# FILE: src/patient_risk_pipeline.py

import os
import re
import io
import json
import time
import logging
import random
from io import StringIO
from datetime import datetime, timedelta

import ast
import boto3
import pandas as pd
import requests
import openai

# =========================
# Config knobs (env-override)
# =========================
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
GLOBAL_THROTTLE = float(os.getenv("OPENAI_THROTTLE_SEC", "0") or 0)

# =========================
# OpenAI API key resolution
# =========================
def _get_openai_key_from_secrets(secret_name: str, region_name: str) -> str:
    client = boto3.client("secretsmanager", region_name=region_name)
    resp = client.get_secret_value(SecretId=secret_name)
    return resp["SecretString"]

def get_openai_key() -> str:
    """
    Prefer ECS-injected env var (OPENAI_API_KEY). If absent, fall back to
    AWS Secrets Manager using OPENAI_API_KEY_SECRET_NAME or OPENAI_SECRET_NAME.
    """
    key = os.getenv("OPENAI_API_KEY")
    if key:
        return key

    secret_name = (
        os.getenv("OPENAI_API_KEY_SECRET_NAME")
        or os.getenv("OPENAI_SECRET_NAME")
        or "openai/api-key"
    )
    region = os.getenv("AWS_REGION", "us-east-1")
    try:
        return _get_openai_key_from_secrets(secret_name, region)
    except Exception as e:
        raise RuntimeError(
            f"❌ Failed to retrieve OpenAI API key from Secrets Manager "
            f"(secret='{secret_name}', region='{region}'): {e}"
        )

openai.api_key = get_openai_key()

# =========
# Logging
# =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger()

logger.info("✅ SCRIPT IS RUNNING")
logger.info(f"📡 ECS log stream: {os.getenv('LOG_STREAM', 'unknown')}")
logger.info(f"🆔 ECS task ID: {os.getenv('TASK_ID')}")

# ========
# Prompts
# ========
RISK_PROMPT = (
    "Please assume the role of a primary care physician. Based on the following patient summary text, "
    "provide a single risk rating between 1 and 100 for the patient's need for follow-up care within the next year, "
    "with 1 being nearly no risk and 100 being the greatest risk.\n\n"
    "Respond in the following format:\n\n"
    "Risk Score: <numeric_value>\n"
    "<Brief explanation or justification here (optional)>\n\n"
    "Here is the patient summary:\n\n"
)

COMBINED_PROMPT = """
You are a primary care physician reviewing the following patient note:

{note}

Please answer the following questions based on the note above.
Respond ONLY with your answers — no explanations, no repetition of questions.

Format your response exactly like this:

Follow-up 1 month: <Yes/No>  
Follow-up 6 months: <Yes/No>  
Oncology recommended: <Yes/No>  
Cardiology recommended: <Yes/No>  

Top Medical Concerns:
1. <Most important medical concern>
2. <Second most important medical concern>
3. <Third most important medical concern>
4. <Fourth most important medical concern>
5. <Fifth most important medical concern>
"""

# ==================
# Utility Functions
# ==================
def get_ecs_metadata_task_id():
    try:
        metadata_uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
        if not metadata_uri:
            return None
        resp = requests.get(f"{metadata_uri}/task", timeout=2)
        if resp.ok:
            data = resp.json()
            task_arn = data.get("TaskARN", "")
            task_id = task_arn.split("/")[-1]
            containers = data.get("Containers", [])
            if containers:
                log_opts = containers[0].get("LogOptions", {})
                log_stream = log_opts.get("awslogs-stream")
                os.environ["LOG_STREAM"] = log_stream or "unknown"
            return task_id
    except Exception as e:
        logger.warning(f"Could not retrieve ECS metadata: {e}")
    return None

# Set task ID in environment (used in audit logging)
ecs_task_id = get_ecs_metadata_task_id()
os.environ["TASK_ID"] = ecs_task_id or "unknown"

def extract_risk_score(text):
    if not isinstance(text, str):
        return None
    match = re.search(r'Risk Score:\s*(\d{1,3})', text)
    if match:
        score = int(match.group(1))
        if 0 <= score <= 100:
            return score / 100.0
    return None

# --- Robust OpenAI caller with backoff + jitter + optional throttle ---
def get_chat_response(inquiry_note, model=OPENAI_MODEL, retries=8, base_delay=1.5, max_delay=20):
    """
    Calls OpenAI ChatCompletion with exponential backoff and jitter.
    Respects GLOBAL_THROTTLE (seconds) between calls if set via env OPENAI_THROTTLE_SEC.
    """
    last_err = None
    for attempt in range(retries):
        try:
            if GLOBAL_THROTTLE > 0:
                time.sleep(GLOBAL_THROTTLE)
            resp = openai.ChatCompletion.create(
                model=model,
                messages=[{"role": "user", "content": inquiry_note}],
                timeout=60,  # seconds
            )
            return {"message": {"content": resp.choices[0].message.content}}
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            transient = any(s in msg for s in [
                "rate limit", "server is overloaded", "overloaded", "503", "timeout",
                "temporarily unavailable", "connection", "bad gateway", "gateway timeout", "service unavailable"
            ])
            if not transient and attempt >= 1:
                logger.warning(f"Non-transient OpenAI error on attempt {attempt+1}: {e}")
                break
            sleep_s = min(max_delay, base_delay * (2 ** attempt)) * (0.5 + random.random())
            logger.warning(f"Attempt {attempt+1} failed: {e}. Backing off {sleep_s:.1f}s...")
            time.sleep(sleep_s)
    logger.error(f"All retries failed for OpenAI API. Last error: {last_err}")
    return {"message": {"content": ""}}

def query_combined_prompt(note, template=COMBINED_PROMPT):
    prompt = template.format(note=note)
    response = get_chat_response(prompt)['message']['content']
    return response

# --- Improved parsing for the combined prompt output ---
def safe_split(line, label):
    """
    Accepts e.g.:
      "Follow-up 1 month: yes"
      "follow_up 1 Month :  Yes"
    and returns the RHS if the LHS roughly matches the label.
    """
    try:
        lhs, rhs = line.split(":", 1)
        if re.sub(r"\s+", "", lhs).lower().startswith(re.sub(r"\s+", "", label).lower()):
            return rhs.strip()
    except Exception:
        pass
    return None

def parse_response_and_concerns(text):
    """
    Expected shape (your format), but robust to case/spacing:
      Follow-up 1 month: <Yes/No>
      Follow-up 6 months: <Yes/No>
      Oncology recommended: <Yes/No>
      Cardiology recommended: <Yes/No>
      (blank line ok)
      Top Medical Concerns:
      1. ...
      2. ...
      ...
    """
    try:
        lines = [l.strip() for l in str(text).strip().splitlines() if l.strip() != ""]
        f1mo = f6mo = onc = card = None
        concerns_text = ""

        # find concerns header index (case-insensitive)
        concerns_idx = None
        for i, l in enumerate(lines):
            if re.sub(r"\s+", "", l).lower().startswith("topmedicalconcerns"):
                concerns_idx = i
                break

        header_slice = lines[:concerns_idx] if concerns_idx is not None else lines[:4]
        for l in header_slice:
            v = safe_split(l, "Follow-up 1 month")
            if v is not None: f1mo = v; continue
            v = safe_split(l, "Follow-up 6 months")
            if v is not None: f6mo = v; continue
            v = safe_split(l, "Oncology recommended")
            if v is not None: onc = v; continue
            v = safe_split(l, "Cardiology recommended")
            if v is not None: card = v; continue

        if concerns_idx is not None:
            concerns_lines = lines[concerns_idx+1:]
            concerns_text = "\n".join(cl.strip() for cl in concerns_lines)

        return pd.Series(
            [f1mo, f6mo, onc, card, concerns_text],
            index=["follow_up_1mo", "follow_up_6mo", "oncology_rec", "cardiology_rec", "top_concerns"],
        )
    except Exception as e:
        logger.warning(f"Failed to parse response (len={len(str(text)) if text is not None else 0}): {e}")
        return pd.Series([None]*5, index=['follow_up_1mo','follow_up_6mo','oncology_rec','cardiology_rec','top_concerns'])

def log_audit_summary(s3_client, bucket, key, summary):
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(summary, indent=2).encode("utf-8")
    )

# =========
# Main
# =========
def main():
    start_time = time.time()
    logger.info("📌 Starting main() and reading env vars...")

    # --- Required envs (fail fast) ---
    required_envs = ["INPUT_S3", "OUTPUT_S3", "EMAIL_TO", "EMAIL_FROM"]
    missing = [e for e in required_envs if not os.environ.get(e)]
    if missing:
        raise RuntimeError(
            f"Missing required env vars: {missing}. "
            f"Expected e.g., INPUT_S3=s3://bucket/path.csv, OUTPUT_S3=s3://bucket/path.csv"
        )

    input_s3 = os.environ["INPUT_S3"]
    output_s3 = os.environ["OUTPUT_S3"]
    email_to = os.environ["EMAIL_TO"]
    email_from = os.environ["EMAIL_FROM"]
    email_subject = os.environ.get("EMAIL_SUBJECT", "High-Risk Patient Report")
    threshold = float(os.environ.get("THRESHOLD", 0.95))
    logger.info(f"📊 Using risk threshold: {threshold}")
    physician_ids_raw = os.environ.get("PHYSICIAN_ID_LIST", "").strip()

    # Demo guardrails (optional)
    dry_run_email = os.getenv("DRY_RUN_EMAIL", "false").lower() == "true"
    max_notes = int(os.getenv("MAX_NOTES", "0") or 0)

    # --- Dynamic default date range (past 7 days if not provided) ---
    try:
        start_date = pd.to_datetime(os.environ.get("START_DATE"))
        end_date = pd.to_datetime(os.environ.get("END_DATE"))
        if pd.isna(start_date) or pd.isna(end_date):
            raise ValueError
    except Exception:
        today = datetime.utcnow().date()
        start_date = pd.to_datetime(today - timedelta(days=7))
        end_date = pd.to_datetime(today)
        logger.info(f"🗓️ No dates provided. Using default range: {start_date.date()} to {end_date.date()}")

    input_bucket, input_key = input_s3.replace("s3://", "").split("/", 1)
    output_bucket, output_key = output_s3.replace("s3://", "").split("/", 1)
    local_input = '/tmp/input.csv'

    s3 = boto3.client('s3', region_name=os.environ.get("AWS_REGION", "us-east-1"))
    logger.info(f"🪣 Downloading from S3: bucket={input_bucket}, key={input_key}")

    # S3 download w/ single retry
    try:
        s3.download_file(input_bucket, input_key, local_input)
        logger.info("✅ Download succeeded.")
    except Exception as e:
        logger.warning(f"Primary S3 download failed: {e}; retrying once...")
        time.sleep(2.0)
        s3.download_file(input_bucket, input_key, local_input)
        logger.info("✅ Download succeeded on retry.")

    logger.info("📥 Loading input CSV...")
    df = pd.read_csv(local_input)
    logger.info(f"Loaded {len(df)} rows.")

    required_cols = ["idx", "visit_date", "full_note", "physician_id"]
    missing = [col for col in required_cols if col not in df.columns]
    if missing:
        logger.error(f"Missing required columns: {missing}")
        # Still write an output file with missing outputs + audit for consistency
        df_final = df.copy()
        for c in ["risk_rating","risk_score","combined_response","follow_up_1mo","follow_up_6mo","oncology_rec","cardiology_rec","top_concerns"]:
            if c not in df_final.columns: df_final[c] = None
        csv_buffer = StringIO()
        df_final.to_csv(csv_buffer, index=False)
        s3.put_object(Bucket=output_bucket, Key=output_key, Body=csv_buffer.getvalue())
        logger.info("✅ Wrote output (missing columns case) to S3.")

        summary = {
            "timestamp": datetime.utcnow().isoformat(),
            "physician_id": os.getenv("PHYSICIAN_ID_LIST"),
            "date_start": os.getenv("START_DATE"),
            "date_end": os.getenv("END_DATE"),
            "total_notes": len(df),
            "high_risk_count": 0,
            "email_sent": False,
            "output_path": f"s3://{output_bucket}/{output_key}",
            "run_duration_sec": round(time.time() - start_time, 2),
            "ecs_task_id": os.getenv("TASK_ID"),
            "ecs_log_stream": os.getenv("LOG_STREAM", "unknown"),
            "warning": f"Missing required input columns: {missing}"
        }
        audit_bucket = os.getenv("AUDIT_BUCKET", output_bucket)
        audit_prefix = os.getenv("AUDIT_PREFIX", "audit_logs")
        audit_key = f"{audit_prefix}/{datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%SZ')}_summary.json"
        log_audit_summary(s3, audit_bucket, audit_key, summary)
        logger.info(f"📁 Audit log written to s3://{audit_bucket}/{audit_key}")
        # Exit non-zero so ECS marks the task as failed (clear signal)
        raise SystemExit(2)

    df['visit_date'] = pd.to_datetime(df['visit_date'], errors='coerce')
    df_original = df.copy()

    physician_id_filter = None
    if physician_ids_raw:
        try:
            physician_id_filter = [int(x.strip()) for x in physician_ids_raw.split(",") if x.strip().isdigit()]
            if not physician_id_filter:
                logger.warning("PHYSICIAN_ID_LIST provided but no valid IDs parsed.")
                physician_id_filter = None
        except Exception as e:
            logger.error(f"Failed to parse PHYSICIAN_ID_LIST: {e}")
            physician_id_filter = None

    mask = (df['visit_date'] >= start_date) & (df['visit_date'] <= end_date)
    if physician_id_filter:
        logger.info(f"Filtering for physician_id in: {physician_id_filter}")
        mask &= df['physician_id'].isin(physician_id_filter)

    df_filtered = df[mask].copy()
    if df_filtered.empty:
        logger.warning("No records matched physician/date filters.")
        # Write empty outputs (consistent schema) + minimal audit, then exit
        df_final = df_original.copy()
        for c in ["risk_rating","risk_score","combined_response","follow_up_1mo","follow_up_6mo","oncology_rec","cardiology_rec","top_concerns"]:
            if c not in df_final.columns: df_final[c] = None

        csv_buffer = StringIO()
        df_final.to_csv(csv_buffer, index=False)
        s3.put_object(Bucket=output_bucket, Key=output_key, Body=csv_buffer.getvalue())
        logger.info("✅ Final (empty) output written to S3 due to no matches.")

        summary = {
            "timestamp": datetime.utcnow().isoformat(),
            "physician_id": os.getenv("PHYSICIAN_ID_LIST"),
            "date_start": os.getenv("START_DATE"),
            "date_end": os.getenv("END_DATE"),
            "total_notes": len(df),
            "high_risk_count": 0,
            "email_sent": False,
            "output_path": f"s3://{output_bucket}/{output_key}",
            "run_duration_sec": round(time.time() - start_time, 2),
            "ecs_task_id": os.getenv("TASK_ID"),
            "ecs_log_stream": os.getenv("LOG_STREAM", "unknown")
        }
        audit_bucket = os.getenv("AUDIT_BUCKET", output_bucket)
        audit_prefix = os.getenv("AUDIT_PREFIX", "audit_logs")
        audit_key = f"{audit_prefix}/{datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%SZ')}_summary.json"
        log_audit_summary(s3, audit_bucket, audit_key, summary)
        logger.info(f"📁 Audit log written to s3://{audit_bucket}/{audit_key}")
        return

    # Optional cap for cheap/fast dry runs before LLM calls
    if max_notes > 0 and len(df_filtered) > max_notes:
        logger.info(f"✂️  MAX_NOTES={max_notes} active; truncating from {len(df_filtered)} rows.")
        df_filtered = df_filtered.head(max_notes).copy()

    logger.info(f"Filtered {len(df_filtered)} rows. Running risk assessments...")
    # Apply risk prompt sequentially; GLOBAL_THROTTLE + backoff tame 429/503 bursts.
    df_filtered['risk_rating'] = df_filtered['full_note'].apply(
        lambda note: get_chat_response(RISK_PROMPT + str(note))['message']['content']
    )
    df_filtered['risk_score'] = df_filtered['risk_rating'].apply(extract_risk_score)

    if df_filtered['risk_score'].dropna().empty:
        logger.warning("No valid risk scores.")
        # still produce an output with columns present
        df_final = df_original.copy()
        for c in ["risk_rating","risk_score","combined_response","follow_up_1mo","follow_up_6mo","oncology_rec","cardiology_rec","top_concerns"]:
            if c not in df_final.columns: df_final[c] = None
        csv_buffer = StringIO()
        df_final.to_csv(csv_buffer, index=False)
        s3.put_object(Bucket=output_bucket, Key=output_key, Body=csv_buffer.getvalue())
        logger.info("✅ Final output (no scores) written to S3.")
        # audit (no email)
        summary = {
            "timestamp": datetime.utcnow().isoformat(),
            "physician_id": os.getenv("PHYSICIAN_ID_LIST"),
            "date_start": os.getenv("START_DATE"),
            "date_end": os.getenv("END_DATE"),
            "total_notes": len(df),
            "high_risk_count": 0,
            "email_sent": False,
            "output_path": f"s3://{output_bucket}/{output_key}",
            "run_duration_sec": round(time.time() - start_time, 2),
            "ecs_task_id": os.getenv("TASK_ID"),
            "ecs_log_stream": os.getenv("LOG_STREAM", "unknown")
        }
        audit_bucket = os.getenv("AUDIT_BUCKET", output_bucket)
        audit_prefix = os.getenv("AUDIT_PREFIX", "audit_logs")
        audit_key = f"{audit_prefix}/{datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%SZ')}_summary.json"
        log_audit_summary(s3, audit_bucket, audit_key, summary)
        logger.info(f"📁 Audit log written to s3://{audit_bucket}/{audit_key}")
        return

    # ---- Threshold-based selection for second prompt (expected behavior) ----
    logger.info("⚕️ Selecting high-risk rows using threshold...")
    df_filtered["risk_score"] = pd.to_numeric(df_filtered["risk_score"], errors="coerce")
    high_mask = df_filtered["risk_score"] >= threshold
    high_risk_idx = df_filtered.index[high_mask]

    if len(high_risk_idx) == 0:
        logger.info("No rows meet the risk threshold for recommendations.")
        df_filtered["combined_response"] = None
    else:
        logger.info("Generating combined recommendations for %d rows...", len(high_risk_idx))
        df_filtered.loc[high_risk_idx, "combined_response"] = df_filtered.loc[high_risk_idx, "full_note"].apply(query_combined_prompt)

    logger.info("🧠 Parsing care recommendation responses...")
    parsed = df_filtered["combined_response"].dropna().apply(parse_response_and_concerns)
    if not parsed.empty:
        df_filtered[parsed.columns] = parsed

    high_risk_df = df_filtered.loc[high_risk_idx].copy()

    # ---- Merge back to original and write output ----
    columns_to_merge = [
        "idx", "visit_date", "risk_rating", "risk_score", "combined_response",
        "follow_up_1mo", "follow_up_6mo", "oncology_rec", "cardiology_rec", "top_concerns"
    ]
    df_final = df_original.merge(df_filtered[columns_to_merge], on=["idx", "visit_date"], how="left")

    csv_buffer = StringIO()
    df_final.to_csv(csv_buffer, index=False)
    s3.put_object(Bucket=output_bucket, Key=output_key, Body=csv_buffer.getvalue())
    logger.info("✅ Final output written to S3.")

    # ---- Email summary (only if there are high-risk rows) ----
    df_email = high_risk_df.copy()
    logger.info(f"📨 Preparing email for {len(df_email)} high-risk patients...")

    email_sent = False
    if not df_email.empty:
        sections = []
        for _, row in df_email.iterrows():
            concerns_lines = str(row.get('top_concerns', '')).strip().split('\n')
            concerns_formatted = '\n'.join(f"    {line.strip()}" for line in concerns_lines if line.strip())
            section = f"""📋 Patient ID: {row['idx']}
    Visit Date: {row['visit_date']}
    Risk Score: {row['risk_score']}
    Follow-up 1 Month: {row.get('follow_up_1mo', 'N/A')}
    Follow-up 6 Months: {row.get('follow_up_6mo', 'N/A')}
    Oncology Recommended: {row.get('oncology_rec', 'N/A')}
    Cardiology Recommended: {row.get('cardiology_rec', 'N/A')}
    Top Medical Concerns:
{concerns_formatted}
    ----------------------------------------"""
            sections.append(section)

        email_body = f"""
Hello Team,

Please review the following high-risk patient notes and follow-up recommendations
from {start_date.date()} to {end_date.date()}:

{chr(10).join(sections)}

Regards,
Your Clinical Risk Bot
"""
        if os.getenv("DRY_RUN_EMAIL", "false").lower() == "true":
            logger.info("🧪 DRY_RUN_EMAIL=true — skipping SES send. Email body below:\n" + email_body)
            email_sent = False
        else:
            try:
                logger.info("📧 Sending email via SES...")
                ses = boto3.client("ses", region_name=os.environ.get("AWS_REGION", "us-east-1"))

                def _send():
                    return ses.send_email(
                        Source=email_from,
                        Destination={"ToAddresses": [email_to]},
                        Message={
                            "Subject": {"Data": email_subject, "Charset": "UTF-8"},
                            "Body": {"Text": {"Data": email_body, "Charset": "UTF-8"}}
                        }
                    )

                try:
                    response = _send()
                except Exception as e1:
                    logger.warning(f"SES send failed once: {e1}; retrying...")
                    time.sleep(2.0)
                    response = _send()

                logger.info("✅ Email sent! Message ID: %s", response["MessageId"])
                email_sent = True
            except Exception as e:
                logger.error(f"❌ Failed to send email: {e}")

    # ---- Audit summary (always) ----
    summary = {
        "timestamp": datetime.utcnow().isoformat(),
        "physician_id": os.getenv("PHYSICIAN_ID_LIST"),
        "date_start": os.getenv("START_DATE"),
        "date_end": os.getenv("END_DATE"),
        "total_notes": len(df),
        "high_risk_count": int(len(df_email)),
        "email_sent": email_sent,
        "output_path": f"s3://{output_bucket}/{output_key}",
        "run_duration_sec": round(time.time() - start_time, 2),
        "ecs_task_id": os.getenv("TASK_ID"),
        "ecs_log_stream": os.getenv("LOG_STREAM", "unknown")
    }

    audit_bucket = os.getenv("AUDIT_BUCKET", output_bucket)
    audit_prefix = os.getenv("AUDIT_PREFIX", "audit_logs")
    audit_key = f"{audit_prefix}/{datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%SZ')}_summary.json"

    log_audit_summary(s3, audit_bucket, audit_key, summary)
    logger.info(f"📁 Audit log written to s3://{audit_bucket}/{audit_key}")
    logger.info("📊 Run Summary:\n" + json.dumps(summary, indent=2))
    logger.info("✅ Script completed in %.2f seconds", time.time() - start_time)


if __name__ == "__main__":
    main()
