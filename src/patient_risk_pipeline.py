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
    # Allow flexible whitespace, case-insensitive, and optional decimals
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
        # Deterministic stub for tests: produce mid-risk and a simple combined answer
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

# --- Chunked streaming from S3 to keep memory flat ---
def iter_filtered_chunks_from_s3(
    s3_client,
    bucket: str,
    key: str,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    physician_id_filter: list[int] | None,
    max_rows: int,
    chunksize: int = 5000,
):
    """
    Stream and filter the CSV from S3 in chunks to keep memory flat.
    Yields filtered DataFrames until max_rows (if >0) is reached.
    """
    obj = s3_client.get_object(Bucket=bucket, Key=key)
    body = io.TextIOWrapper(obj["Body"], encoding="utf-8")

    usecols = ["idx", "visit_date", "full_note", "physician_id"]
    dtypes = {"idx": "int64", "physician_id": "int64"}
    taken = 0

    for chunk in pd.read_csv(
        body,
        usecols=usecols,
        dtype=dtypes,
        parse_dates=["visit_date"],
        chunksize=chunksize,
    ):
        # Filter by date + physician
        mask = (chunk["visit_date"] >= start_date) & (chunk["visit_date"] <= end_date)
        if physician_id_filter:
            mask &= chunk["physician_id"].isin(physician_id_filter)
        filt = chunk.loc[mask]

        # Respect MAX_NOTES cap
        if max_rows and max_rows > 0 and not filt.empty:
            remaining = max_rows - taken
            if remaining <= 0:
                break
            filt = filt.head(remaining)

        if not filt.empty:
            yield filt
            taken += len(filt)

        if max_rows and taken >= max_rows:
            break

# =========
# Main (now with CLI)
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
    logger.info(f"🛶 Streaming & filtering from S3 in chunks: s3://{input_bucket}/{input_key}")

    # Physician filter parsing
    physician_id_filter = None
    if physician_ids_raw:
        try:
            physician_id_filter = [
                int(x.strip()) for x in physician_ids_raw.split(",") if x.strip().isdigit()
            ]
            if not physician_id_filter:
                logger.warning("PHYSICIAN_ID_LIST provided but no valid IDs parsed.")
                physician_id_filter = None
        except Exception as e:
            logger.error(f"Failed to parse PHYSICIAN_ID_LIST: {e}")
            physician_id_filter = None

    # Build filtered working set with tiny memory footprint
    filtered_parts = []
    for part in iter_filtered_chunks_from_s3(
        s3_client=s3,
        bucket=input_bucket,
        key=input_key,
        start_date=start_date,
        end_date=end_date,
        physician_id_filter=physician_id_filter,
        max_rows=max_notes,  # 0 means 'no cap'
        chunksize=5000,
    ):
        filtered_parts.append(part)

    if not filtered_parts:
        logger.warning("No records matched physician/date filters.")
        # Write schema-consistent (but empty) CSV with expected columns.
        columns = [
            "idx", "visit_date", "full_note", "physician_id",
            "risk_rating", "risk_score", "combined_response",
            "follow_up_1mo", "follow_up_6mo", "oncology_rec", "cardiology_rec", "top_concerns"
        ]
        empty_df = pd.DataFrame(columns=columns)
        csv_buffer = StringIO()
        empty_df.to_csv(csv_buffer, index=False)
        s3_put_text(s3, output_bucket, output_key, csv_buffer.getvalue())
        logger.info("✅ Final (empty) output written to S3 due to no matches.")

        summary = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "physician_id": physician_ids_raw,
            "date_start": start_date_str,
            "date_end": end_date_str,
            "total_notes": 0,
            "high_risk_count": 0,
            "email_sent": False,
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
        return

    df_filtered = pd.concat(filtered_parts, ignore_index=True)
    logger.info(f"Filtered {len(df_filtered)} rows (chunked build).")

    # Optional cap (already applied in iterator, but keep for safety)
    if max_notes > 0 and len(df_filtered) > max_notes:
        logger.info(f"✂️  MAX_NOTES={max_notes} active; truncating from {len(df_filtered)} rows.")
        df_filtered = df_filtered.head(max_notes).copy()

    logger.info("Running risk assessments...")
    # Apply risk prompt sequentially; GLOBAL_THROTTLE + backoff tame 429/503 bursts.
    df_filtered['risk_rating'] = df_filtered['full_note'].apply(
        lambda note: get_chat_response(RISK_PROMPT + str(note))['message']['content']
    )
    df_filtered['risk_score'] = df_filtered['risk_rating'].apply(extract_risk_score)

    if df_filtered['risk_score'].dropna().empty:
        logger.warning("No valid risk scores.")
        # Output only filtered structure (no matches scored)
        out_df = df_filtered.copy()
        for c in ["combined_response","follow_up_1mo","follow_up_6mo","oncology_rec","cardiology_rec","top_concerns"]:
            if c not in out_df.columns: out_df[c] = None
        csv_buffer = StringIO()
        out_df.to_csv(csv_buffer, index=False)
        s3_put_text(s3, output_bucket, output_key, csv_buffer.getvalue())
        logger.info("✅ Final output (no scores) written to S3.")

        summary = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "physician_id": physician_ids_raw,
            "date_start": start_date_str,
            "date_end": end_date_str,
            "total_notes": int(len(df_filtered)),
            "high_risk_count": 0,
            "email_sent": False,
            "output_path": f"s3://{output_bucket}/{output_key}",
            "run_duration_sec": round(time.time() - start_time, 2),
            "ecs_task_id": os.getenv("TASK_ID"),
            "ecs_log_stream": os.getenv("LOG_STREAM", "unknown")
        }
        audit_bucket = os.getenv("AUDIT_BUCKET", output_bucket)
        audit_prefix = os.getenv("AUDIT_PREFIX", "audit_logs")
        audit_key = f"{audit_prefix}/{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')}_summary.json"
        log_audit_summary(s3, audit_bucket, audit_key, summary)
        logger.info(f"📁 Audit log written to s3://{audit_bucket}/{audit_key}")
        return

    # ---- Threshold-based selection for second prompt ----
    logger.info("⚕️ Selecting high-risk rows using threshold...")
    df_filtered["risk_score"] = pd.to_numeric(df_filtered["risk_score"], errors="coerce")
    high_mask = df_filtered["risk_score"] >= threshold
    high_risk_idx = df_filtered.index[high_mask]

    if len(high_risk_idx) == 0:
        logger.info("No rows meet the risk threshold for recommendations.")
        df_filtered["combined_response"] = None
    else:
        logger.info("Generating combined recommendations for %d rows...", len(high_risk_idx))
        df_filtered.loc[high_risk_idx, "combined_response"] = (
            df_filtered.loc[high_risk_idx, "full_note"].apply(query_combined_prompt)
        )

    logger.info("🧠 Parsing care recommendation responses...")
    parsed = df_filtered["combined_response"].dropna().apply(parse_response_and_concerns)
    if not parsed.empty:
        df_filtered[parsed.columns] = parsed

    high_risk_df = df_filtered.loc[high_risk_idx].copy()

    # ---- Write output (only filtered set to avoid OOM) ----
    columns_out = [
        "idx", "visit_date", "full_note", "physician_id",
        "risk_rating", "risk_score", "combined_response",
        "follow_up_1mo", "follow_up_6mo", "oncology_rec", "cardiology_rec", "top_concerns"
    ]
    out_df = df_filtered.reindex(columns=columns_out)
    csv_buffer = StringIO()
    out_df.to_csv(csv_buffer, index=False)
    s3_put_text(s3, output_bucket, output_key, csv_buffer.getvalue())
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
        if dry_run_email:
            logger.info("🧪 DRY_RUN_EMAIL=true — skipping SES send. Email body below:\n" + email_body)
            email_sent = False
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

    # ---- Audit summary (always) ----
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "physician_id": physician_ids_raw,
        "date_start": start_date.strftime("%Y-%m-%d"),
        "date_end": end_date.strftime("%Y-%m-%d"),
        "total_notes": int(len(df_filtered)),
        "high_risk_count": int(len(df_email)),
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
