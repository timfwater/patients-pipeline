# FILE: src/patient_risk_pipeline.py
import os
import re
import io
import json
import time
import logging
import random
from io import StringIO
from datetime import datetime, timedelta, timezone
import argparse

import boto3
import pandas as pd
import requests
import openai

# =========================
# Config knobs (env-override)
# =========================
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
GLOBAL_THROTTLE = float(os.getenv("OPENAI_THROTTLE_SEC", "0") or 0)
LLM_DISABLED = os.getenv("LLM_DISABLED", "false").lower() == "true"  # for fast smoke tests
CSV_CHUNK_ROWS = int(os.getenv("CSV_CHUNK_ROWS", "5000"))  # streaming chunk size
OUTPUT_TMP = os.getenv("OUTPUT_TMP", "/tmp/output.csv")    # local rolling output

# =========
# Logging
# =========
def _configure_logging():
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    fmt_mode = os.getenv("LOG_FORMAT", "text").lower()  # "text" | "json"
    logger = logging.getLogger("patient_pipeline")
    logger.setLevel(level)
    handler = logging.StreamHandler()
    if fmt_mode == "json":
        class JsonFormatter(logging.Formatter):
            def format(self, record):
                base = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "level": record.levelname,
                    "event": record.getMessage(),
                    "run_id": os.getenv("RUN_ID", "unknown"),
                    "task_id": os.getenv("TASK_ID", "unknown"),
                    "log_stream": os.getenv("LOG_STREAM", "unknown"),
                }
                if record.exc_info:
                    base["exc_info"] = self.formatException(record.exc_info)
                return json.dumps(base)
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.handlers = [handler]
    logger.propagate = False
    return logger

logger = _configure_logging()

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

# Initialize OpenAI (unless disabled)
if not LLM_DISABLED:
    try:
        openai.api_key = get_openai_key()
    except Exception as e:
        logger.error("OpenAI key resolution failed: %s", e)
        # allow pipeline to continue only if LLM_DISABLED=true
        raise

logger.info("✅ SCRIPT IS RUNNING")
logger.info(f"📡 ECS log stream: {os.getenv('LOG_STREAM', 'unknown')}")
logger.info(f"🆔 ECS task ID: {os.getenv('TASK_ID', 'unknown')}")

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
os.environ["TASK_ID"] = ecs_task_id or os.getenv("TASK_ID", "unknown")
os.environ["RUN_ID"] = os.getenv("RUN_ID", datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))

def extract_risk_score(text):
    """
    Extract a numeric risk score from variants like:
      "Risk Score: 87"
      " Risk   Score :  42 "
      "Risk Score: 55.0"
    Returns a float in [0.0, 1.0] (value / 100).
    """
    if not isinstance(text, str):
        return None
    m = re.search(r'\brisk\s*score\s*:\s*([0-9]+(?:\.[0-9]+)?)\b', text, flags=re.IGNORECASE)
    if not m:
        return None
    try:
        val = float(m.group(1))
        if 0.0 <= val <= 100.0:
            return val / 100.0
    except Exception:
        return None
    return None

# --- Robust OpenAI caller with backoff + jitter + optional throttle ---
def get_chat_response(inquiry_note, model=OPENAI_MODEL, retries=8, base_delay=1.5, max_delay=20):
    """
    Calls OpenAI ChatCompletion with exponential backoff and jitter.
    Respects GLOBAL_THROTTLE (seconds) between calls if set via env OPENAI_THROTTLE_SEC.
    If LLM_DISABLED=true, returns a deterministic stub.
    """
    if LLM_DISABLED:
        if inquiry_note.strip().startswith("Please assume the role"):
            return {"message": {"content": "Risk Score: 72\nLikely follow-up needed."}}
        else:
            return {"message": {"content": (
                "Follow-up 1 month: Yes\n"
                "Follow-up 6 months: Yes\n"
                "Oncology recommended: No\n"
                "Cardiology recommended: Yes\n\n"
                "Top Medical Concerns:\n"
                "1. Hypertension\n2. A1c elevation\n3. Chest pain\n4. Medication adherence\n5. BMI"
            )}}
    last_err = None
    for attempt in range(retries):
        try:
            if GLOBAL_THROTTLE > 0:
                time.sleep(GLOBAL_THROTTLE)
            resp = openai.ChatCompletion.create(
                model=model,
                messages=[{"role": "user", "content": inquiry_note}],
                timeout=60,
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

def safe_split(line, label):
    try:
        lhs, rhs = line.split(":", 1)
        if re.sub(r"\s+", "", lhs).lower().startswith(re.sub(r"\s+", "", label).lower()):
            return rhs.strip()
    except Exception:
        pass
    return None

def parse_response_and_concerns(text):
    try:
        lines = [l.strip() for l in str(text).strip().splitlines() if l.strip() != ""]
        f1mo = f6mo = onc = card = None
        concerns_text = ""
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

def log_audit_summary(s3_client, bucket, key, summary, retries=3):
    payload = json.dumps(summary, indent=2).encode("utf-8")
    delay = 1.0
    for attempt in range(retries):
        try:
            s3_client.put_object(Bucket=bucket, Key=key, Body=payload)
            return
        except Exception as e:
            if attempt == retries - 1:
                raise
            logger.warning(f"S3 audit put failed (attempt {attempt+1}): {e}; retrying in {delay:.1f}s")
            time.sleep(delay)
            delay *= 2

def s3_put_text(s3_client, bucket, key, text, retries=3):
    body = text if isinstance(text, (bytes, bytearray)) else text.encode("utf-8")
    delay = 1.0
    for attempt in range(retries):
        try:
            s3_client.put_object(Bucket=bucket, Key=key, Body=body)
            return
        except Exception as e:
            if attempt == retries - 1:
                raise
            logger.warning(f"S3 put failed (attempt {attempt+1}): {e}; retrying in {delay:.1f}s")
            time.sleep(delay)
            delay *= 2

# =========
# Main (streaming, chunked)
# =========
def run_pipeline(
    input_s3: str,
    output_s3: str,
    email_to: str,
    email_from: str,
    email_subject: str = "High-Risk Patient Report",
    threshold: float = 0.95,
    start_date_str: str | None = None,
    end_date_str: str | None = None,
    physician_ids_raw: str | None = None,
    dry_run_email: bool = False,
    max_notes: int = 0,
    aws_region: str = "us-east-1",
):
    start_time = time.time()
    logger.info("📌 Starting run_pipeline() with validated args/env...")

    # --- Dates (default last 7 days) ---
    try:
        if start_date_str and end_date_str:
            start_date = pd.to_datetime(start_date_str)
            end_date = pd.to_datetime(end_date_str)
        else:
            today = datetime.now(timezone.utc).date()
            start_date = pd.to_datetime(today - timedelta(days=7))
            end_date = pd.to_datetime(today)
            logger.info(f"🗓️ No dates provided. Using default range: {start_date.date()} to {end_date.date()}")
    except Exception:
        today = datetime.now(timezone.utc).date()
        start_date = pd.to_datetime(today - timedelta(days=7))
        end_date = pd.to_datetime(today)

    input_bucket, input_key = input_s3.replace("s3://", "").split("/", 1)
    output_bucket, output_key = output_s3.replace("s3://", "").split("/", 1)

    s3 = boto3.client('s3', region_name=aws_region)

    # Physician filter
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

    # Ensure output file is clean
    try:
        if os.path.exists(OUTPUT_TMP):
            os.remove(OUTPUT_TMP)
    except Exception:
        pass

    total_rows = 0
    total_high_risk = 0
    remaining_to_score = max_notes if max_notes > 0 else float("inf")
    wrote_header = False

    logger.info(f"🛶 Streaming CSV from S3 in chunks of ~{CSV_CHUNK_ROWS} rows...")
    # Requires s3fs installed
    read_opts = dict(storage_options={"client_kwargs": {"region_name": aws_region}}, chunksize=CSV_CHUNK_ROWS)

    try:
        chunk_iter = pd.read_csv(input_s3, **read_opts)
    except TypeError:
        # Older pandas: chunksize is separate param
        chunk_iter = pd.read_csv(input_s3, storage_options={"client_kwargs": {"region_name": aws_region}}, iterator=True, chunksize=CSV_CHUNK_ROWS)

    for chunk_idx, df in enumerate(chunk_iter, start=1):
        total_rows += len(df)

        # Validate columns on first chunk
        if chunk_idx == 1:
            required_cols = ["idx", "visit_date", "full_note", "physician_id"]
            missing_cols = [c for c in required_cols if c not in df.columns]
            if missing_cols:
                logger.error(f"Missing required columns: {missing_cols}")
                # Write pass-through with empty annotation columns
                df_final = df.copy()
                for c in ["risk_rating","risk_score","combined_response","follow_up_1mo","follow_up_6mo","oncology_rec","cardiology_rec","top_concerns"]:
                    if c not in df_final.columns: df_final[c] = None
                df_final.to_csv(OUTPUT_TMP, index=False, mode="w")
                s3.upload_file(OUTPUT_TMP, output_bucket, output_key)
                summary = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "physician_id": physician_ids_raw,
                    "date_start": start_date_str,
                    "date_end": end_date_str,
                    "total_notes": total_rows,
                    "high_risk_count": 0,
                    "email_sent": False,
                    "output_path": f"s3://{output_bucket}/{output_key}",
                    "run_duration_sec": round(time.time() - start_time, 2),
                    "ecs_task_id": os.getenv("TASK_ID"),
                    "ecs_log_stream": os.getenv("LOG_STREAM", "unknown"),
                    "warning": f"Missing required input columns: {missing_cols}",
                }
                audit_bucket = os.getenv("AUDIT_BUCKET", output_bucket)
                audit_prefix = os.getenv("AUDIT_PREFIX", "audit_logs")
                audit_key = f"{audit_prefix}/{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')}_summary.json"
                log_audit_summary(s3, audit_bucket, audit_key, summary)
                logger.info(f"📁 Audit log written to s3://{audit_bucket}/{audit_key}")
                raise SystemExit(2)

        # Normalize types
        df['visit_date'] = pd.to_datetime(df['visit_date'], errors='coerce')

        # Build filtered view for scoring
        mask = (df['visit_date'] >= start_date) & (df['visit_date'] <= end_date)
        if physician_id_filter:
            mask &= df['physician_id'].isin(physician_id_filter)

        df["risk_rating"] = None
        df["risk_score"] = None
        df["combined_response"] = None
        df["follow_up_1mo"] = None
        df["follow_up_6mo"] = None
        df["oncology_rec"] = None
        df["cardiology_rec"] = None
        df["top_concerns"] = None

        # Nothing to score in this chunk? still append passthrough rows
        if not mask.any() or remaining_to_score == 0:
            df.to_csv(OUTPUT_TMP, index=False, mode=("a" if wrote_header else "w"), header=not wrote_header)
            wrote_header = True
            continue

        df_to_score = df.loc[mask].copy()

        # Enforce MAX_NOTES budget across chunks
        if remaining_to_score < len(df_to_score):
            df_to_score = df_to_score.head(remaining_to_score)

        # ---- Risk scoring
        logger.info(f"🧪 Chunk {chunk_idx}: scoring {len(df_to_score)} notes (budget remaining before: {remaining_to_score})")
        df_to_score['risk_rating'] = df_to_score['full_note'].apply(
            lambda note: get_chat_response(RISK_PROMPT + str(note))['message']['content']
        )
        df_to_score['risk_score'] = df_to_score['risk_rating'].apply(extract_risk_score)
        remaining_to_score -= len(df_to_score)

        # ---- Recommendations for high risk rows
        if df_to_score['risk_score'].notna().any():
            high_mask = df_to_score['risk_score'] >= threshold
            n_high = int(high_mask.sum())
            total_high_risk += n_high
            if n_high > 0:
                df_to_score.loc[high_mask, "combined_response"] = df_to_score.loc[high_mask, "full_note"].apply(query_combined_prompt)
                parsed = df_to_score.loc[high_mask, "combined_response"].dropna().apply(parse_response_and_concerns)
                if not parsed.empty:
                    df_to_score.loc[parsed.index, parsed.columns] = parsed

        # ---- Write out this chunk (annotated rows merged back by index)
        df.update(df_to_score)  # aligns on index; only overwrites rows we scored
        df.to_csv(OUTPUT_TMP, index=False, mode=("a" if wrote_header else "w"), header=not wrote_header)
        wrote_header = True

    # Upload the completed CSV once
    s3.upload_file(OUTPUT_TMP, output_bucket, output_key)
    logger.info("✅ Final output written to S3.")

    # Prepare email body from just the high-risk rows we actually evaluated
    email_sent = False
    if total_high_risk > 0:
        # Re-read only high-risk rows from the produced output (streaming) to build the email
        # This is cheap because we're only formatting text, not calling the LLM again.
        df_out_iter = pd.read_csv(OUTPUT_TMP, chunksize=CSV_CHUNK_ROWS)
        sections = []
        for odf in df_out_iter:
            odf['risk_score'] = pd.to_numeric(odf.get('risk_score'), errors='coerce')
            hi = odf[odf['risk_score'] >= threshold]
            for _, row in hi.iterrows():
                concerns_lines = str(row.get('top_concerns', '')).strip().split('\n')
                concerns_formatted = '\n'.join(f"    {line.strip()}" for line in concerns_lines if line.strip())
                section = f"""📋 Patient ID: {row.get('idx')}
    Visit Date: {row.get('visit_date')}
    Risk Score: {row.get('risk_score')}
    Follow-up 1 Month: {row.get('follow_up_1mo', 'N/A')}
    Follow-up 6 Months: {row.get('follow_up_6mo', 'N/A')}
    Oncology Recommended: {row.get('oncology_rec', 'N/A')}
    Cardiology Recommended: {row.get('cardiology_rec', 'N/A')}
    Top Medical Concerns:
{concerns_formatted}
    ----------------------------------------"""
                sections.append(section)

        if sections:
            email_body = f"""
Hello Team,

Please review the following high-risk patient notes and follow-up recommendations
from {start_date.date()} to {end_date.date()}:

{chr(10).join(sections)}

Regards,
Your Clinical Risk Bot
"""
            if dry_run_email:
                logger.info("🧪 DRY_RUN_EMAIL=true — skipping SES send. Email body below:\n" + email_body)
            else:
                try:
                    logger.info("📧 Sending email via SES...")
                    ses = boto3.client("ses", region_name=aws_region)
                    resp = ses.send_email(
                        Source=email_from,
                        Destination={"ToAddresses": [email_to]},
                        Message={
                            "Subject": {"Data": email_subject, "Charset": "UTF-8"},
                            "Body": {"Text": {"Data": email_body, "Charset": "UTF-8"}}
                        }
                    )
                    logger.info("✅ Email sent! Message ID: %s", resp["MessageId"])
                    email_sent = True
                except Exception as e:
                    logger.error(f"❌ Failed to send email: {e}")

    # ---- Audit summary (always)
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "physician_id": physician_ids_raw,
        "date_start": start_date.strftime("%Y-%m-%d"),
        "date_end": end_date.strftime("%Y-%m-%d"),
        "total_notes": int(total_rows),
        "high_risk_count": int(total_high_risk),
        "email_sent": email_sent,
        "output_path": f"s3://{output_bucket}/{output_key}",
        "run_duration_sec": round(time.time() - start_time, 2),
        "ecs_task_id": os.getenv("TASK_ID"),
        "ecs_log_stream": os.getenv("LOG_STREAM", "unknown"),
        "run_id": os.getenv("RUN_ID", "unknown"),
    }

    audit_bucket = os.getenv("AUDIT_BUCKET", output_bucket)
    audit_prefix = os.getenv("AUDIT_PREFIX", "audit_logs")
    audit_key = f"{audit_prefix}/{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')}_summary.json"
    log_audit_summary(s3, audit_bucket, audit_key, summary)
    logger.info(f"📁 Audit log written to s3://{audit_bucket}/{audit_key}")
    logger.info("📊 Run Summary:\n" + json.dumps(summary, indent=2))
    logger.info("✅ Script completed in %.2f seconds", time.time() - start_time)

# ---------- CLI wrapper ----------
def main():
    parser = argparse.ArgumentParser(description="Patient Risk Pipeline")
    parser.add_argument("--input", dest="input_s3", default=os.getenv("INPUT_S3"), help="s3://bucket/key.csv (or set INPUT_S3)")
    parser.add_argument("--output", dest="output_s3", default=os.getenv("OUTPUT_S3"), help="s3://bucket/key.csv (or set OUTPUT_S3)")
    parser.add_argument("--email-to", dest="email_to", default=os.getenv("EMAIL_TO"), help="Recipient email (or set EMAIL_TO)")
    parser.add_argument("--email-from", dest="email_from", default=os.getenv("EMAIL_FROM"), help="Sender email (verified in SES; or set EMAIL_FROM)")
    parser.add_argument("--subject", dest="email_subject", default=os.getenv("EMAIL_SUBJECT", "High-Risk Patient Report"))
    parser.add_argument("--threshold", type=float, default=float(os.getenv("THRESHOLD", "0.95")))
    parser.add_argument("--start-date", dest="start_date", default=os.getenv("START_DATE"))
    parser.add_argument("--end-date", dest="end_date", default=os.getenv("END_DATE"))
    parser.add_argument("--physician-ids", dest="physician_ids", default=os.getenv("PHYSICIAN_ID_LIST"))
    parser.add_argument("--dry-run-email", action="store_true", default=(os.getenv("DRY_RUN_EMAIL", "false").lower() == "true"))
    parser.add_argument("--max-notes", type=int, default=int(os.getenv("MAX_NOTES", "0") or 0))
    parser.add_argument("--region", dest="aws_region", default=os.getenv("AWS_REGION", "us-east-1"))

    args = parser.parse_args()

    # Fail-fast requireds (env or args)
    if not args.input_s3 or not args.output_s3 or not args.email_to or not args.email_from:
        missing = []
        if not args.input_s3: missing.append("INPUT_S3/--input")
        if not args.output_s3: missing.append("OUTPUT_S3/--output")
        if not args.email_to: missing.append("EMAIL_TO/--email-to")
        if not args.email_from: missing.append("EMAIL_FROM/--email-from")
        raise RuntimeError(f"Missing required args/env: {missing}")

    run_pipeline(
        input_s3=args.input_s3,
        output_s3=args.output_s3,
        email_to=args.email_to,
        email_from=args.email_from,
        email_subject=args.email_subject,
        threshold=args.threshold,
        start_date_str=args.start_date,
        end_date_str=args.end_date,
        physician_ids_raw=args.physician_ids,
        dry_run_email=args.dry_run_email,
        max_notes=args.max_notes,
        aws_region=args.aws_region,
    )

if __name__ == "__main__":
    main()
