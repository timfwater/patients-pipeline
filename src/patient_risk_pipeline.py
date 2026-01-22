# FILE: src/patient_risk_pipeline.py
import os
import re
import json
import time
import logging
import random
import argparse
from datetime import datetime, timedelta, timezone
from io import TextIOWrapper

import boto3
import pandas as pd
import requests
from openai import (
    APIError,
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
)

from src.rag_tfidf import (
    build_index_from_env,
    retrieve_kb,
    format_rag_context,
)

from src.config import (
    OPENAI_MODEL,
    GLOBAL_THROTTLE,
    LLM_DISABLED,
    CSV_CHUNK_ROWS,
    OUTPUT_TMP,
    USE_S3FS,
    USE_LANGCHAIN,
    OPENAI_TEMPERATURE,
    OPENAI_MAX_TOKENS,
    OPENAI_TIMEOUT_SEC,
    logger,
)

from src.llm import (
    _risk_rating_via_langchain,
    get_chat_response,
    query_combined_prompt,
    RISK_PROMPT,
)

# =========================
# RAG
# =========================
RAG_INDEX = None
try:
    RAG_INDEX = build_index_from_env()
except Exception as e:
    # If RAG is disabled, build_index_from_env() returns None.
    # If enabled but misconfigured, this warning tells you why.
    logger.warning("RAG unavailable: %s", e)
    RAG_INDEX = None

# --- RAG startup diagnostics (helps you confirm ON/OFF immediately) ---
logger.info("🧠 RAG_INDEX loaded: %s", "YES" if RAG_INDEX is not None else "NO")
logger.info("🧠 RAG_ENABLED=%s", os.getenv("RAG_ENABLED", "unset"))
logger.info("🧠 RAG_KB_PATH=%s", os.getenv("RAG_KB_PATH", "unset"))
logger.info("🧠 RAG_TOP_K=%s", os.getenv("RAG_TOP_K", "unset"))
logger.info("🧠 RAG_MAX_CHARS=%s", os.getenv("RAG_MAX_CHARS", "unset"))

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
    m = re.search(r"\brisk\s*score\s*:\s*([0-9]+(?:\.[0-9]+)?)\b", text, flags=re.IGNORECASE)
    if not m:
        return None
    try:
        val = float(m.group(1))
        if 0.0 <= val <= 100.0:
            return val / 100.0
    except Exception:
        return None
    return None

def _parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    if not s3_uri.startswith("s3://"):
        raise ValueError(f"Expected s3://... URI, got: {s3_uri}")
    bucket_key = s3_uri.replace("s3://", "", 1)
    bucket, key = bucket_key.split("/", 1)
    return bucket, key

def _read_csv_s3_in_chunks(s3_client, s3_uri: str, chunksize: int, aws_region: str):
    """
    Chunked CSV reader that works WITHOUT s3fs. Streams via boto3.
    Falls back to pandas+s3fs if USE_S3FS=true.
    """
    if USE_S3FS:
        read_opts = dict(storage_options={"client_kwargs": {"region_name": aws_region}}, chunksize=chunksize)
        try:
            return pd.read_csv(s3_uri, **read_opts)
        except TypeError:
            return pd.read_csv(
                s3_uri,
                storage_options={"client_kwargs": {"region_name": aws_region}},
                iterator=True,
                chunksize=chunksize,
            )

    bucket, key = _parse_s3_uri(s3_uri)
    obj = s3_client.get_object(Bucket=bucket, Key=key)
    text_stream = TextIOWrapper(obj["Body"], encoding="utf-8")
    return pd.read_csv(text_stream, chunksize=chunksize)

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
            if v is not None:
                f1mo = v
                continue
            v = safe_split(l, "Follow-up 6 months")
            if v is not None:
                f6mo = v
                continue
            v = safe_split(l, "Oncology recommended")
            if v is not None:
                onc = v
                continue
            v = safe_split(l, "Cardiology recommended")
            if v is not None:
                card = v
                continue

        if concerns_idx is not None:
            concerns_lines = lines[concerns_idx + 1:]
            concerns_text = "\n".join(cl.strip() for cl in concerns_lines)

        return pd.Series(
            [f1mo, f6mo, onc, card, concerns_text],
            index=["follow_up_1mo", "follow_up_6mo", "oncology_rec", "cardiology_rec", "top_concerns"],
        )
    except Exception as e:
        logger.warning(f"Failed to parse response (len={len(str(text)) if text is not None else 0}): {e}")
        return pd.Series(
            [None] * 5,
            index=["follow_up_1mo", "follow_up_6mo", "oncology_rec", "cardiology_rec", "top_concerns"],
        )

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
            logger.info("🗓️ No dates provided. Using default range: %s to %s", start_date.date(), end_date.date())
    except Exception:
        today = datetime.now(timezone.utc).date()
        start_date = pd.to_datetime(today - timedelta(days=7))
        end_date = pd.to_datetime(today)

    output_bucket, output_key = _parse_s3_uri(output_s3)
    s3 = boto3.client("s3", region_name=aws_region)

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

    logger.info("🛶 Streaming CSV from S3 in chunks of ~%d rows... (USE_S3FS=%s)", CSV_CHUNK_ROWS, USE_S3FS)

    chunk_iter = _read_csv_s3_in_chunks(s3, input_s3, chunksize=CSV_CHUNK_ROWS, aws_region=aws_region)

    for chunk_idx, df in enumerate(chunk_iter, start=1):
        total_rows += len(df)

        # Validate columns on first chunk
        if chunk_idx == 1:
            required_cols = ["idx", "visit_date", "full_note", "physician_id"]
            missing_cols = [c for c in required_cols if c not in df.columns]
            if missing_cols:
                logger.error("Missing required columns: %s", missing_cols)

                # Write pass-through with empty annotation columns (INCLUDING rag_context)
                df_final = df.copy()
                for c in [
                    "risk_rating", "risk_score",
                    "combined_response", "rag_context",
                    "follow_up_1mo", "follow_up_6mo",
                    "oncology_rec", "cardiology_rec",
                    "top_concerns",
                    "lc_rationale",
                ]:
                    if c not in df_final.columns:
                        df_final[c] = None

                df_final.to_csv(OUTPUT_TMP, index=False, mode="w")
                s3.upload_file(OUTPUT_TMP, output_bucket, output_key)

                summary = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "physician_id": physician_ids_raw,
                    "date_start": start_date_str,
                    "date_end": end_date_str,
                    "total_notes": int(total_rows),
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
                logger.info("📁 Audit log written to s3://%s/%s", audit_bucket, audit_key)
                raise SystemExit(2)

        # Normalize types
        df["visit_date"] = pd.to_datetime(df["visit_date"], errors="coerce")

        # Build filtered view for scoring
        mask = (df["visit_date"] >= start_date) & (df["visit_date"] <= end_date)
        if physician_id_filter:
            mask &= df["physician_id"].isin(physician_id_filter)

        # Ensure output columns exist (including RAG audit column)
        for col in [
            "risk_rating",
            "risk_score",
            "combined_response",
            "rag_context",
            "follow_up_1mo",
            "follow_up_6mo",
            "oncology_rec",
            "cardiology_rec",
            "top_concerns",
            "lc_rationale",
        ]:
            if col not in df.columns:
                df[col] = None

        # Nothing to score in this chunk? still append passthrough rows
        if not mask.any() or remaining_to_score == 0:
            df.to_csv(OUTPUT_TMP, index=False, mode=("a" if wrote_header else "w"), header=not wrote_header)
            wrote_header = True
            continue

        df_to_score = df.loc[mask].copy()

        # Enforce MAX_NOTES budget across chunks
        if remaining_to_score < len(df_to_score):
            df_to_score = df_to_score.head(int(remaining_to_score))

        logger.info(
            "🧪 Chunk %d: scoring %d notes (budget remaining before: %s)",
            chunk_idx, len(df_to_score), remaining_to_score
        )

        if USE_LANGCHAIN:
            scored = df_to_score["full_note"].apply(lambda note: pd.Series(
                _risk_rating_via_langchain(str(note)),
                index=["risk_rating", "lc_rationale"]
            ))
            df_to_score.loc[scored.index, ["risk_rating", "lc_rationale"]] = scored
        else:
            df_to_score["risk_rating"] = df_to_score["full_note"].apply(
                lambda note: get_chat_response(RISK_PROMPT + str(note))["message"]["content"]
            )

        df_to_score["risk_score"] = df_to_score["risk_rating"].apply(extract_risk_score)
        remaining_to_score -= len(df_to_score)

        # ---- Recommendations for high risk rows
        if df_to_score["risk_score"].notna().any():
            high_mask = df_to_score["risk_score"] >= threshold
            n_high = int(high_mask.sum())
            total_high_risk += n_high

            if n_high > 0:
                df_to_score.loc[high_mask, ["combined_response", "rag_context"]] = (
                    df_to_score.loc[high_mask, "full_note"]
                    .apply(lambda n: pd.Series(
                        query_combined_prompt(n),
                        index=["combined_response", "rag_context"]
                    ))
                )

                parsed = (
                    df_to_score.loc[high_mask, "combined_response"]
                    .dropna()
                    .apply(parse_response_and_concerns)
                )
                if not parsed.empty:
                    df_to_score.loc[parsed.index, parsed.columns] = parsed

        # ---- Merge annotated rows back into original chunk
        df.loc[df_to_score.index, df_to_score.columns] = df_to_score

        # ---- Write out this chunk
        df.to_csv(OUTPUT_TMP, index=False, mode=("a" if wrote_header else "w"), header=not wrote_header)
        wrote_header = True

    # Upload the completed CSV once (AFTER all chunks)
    s3.upload_file(OUTPUT_TMP, output_bucket, output_key)
    logger.info("✅ Final output written to S3: s3://%s/%s", output_bucket, output_key)

    # Prepare email body from just the high-risk rows we actually evaluated
    email_sent = False
    if total_high_risk > 0:
        df_out_iter = pd.read_csv(OUTPUT_TMP, chunksize=CSV_CHUNK_ROWS)
        sections = []

        for odf in df_out_iter:
            odf["risk_score"] = pd.to_numeric(odf.get("risk_score"), errors="coerce")
            hi = odf[odf["risk_score"] >= threshold]

            for _, row in hi.iterrows():
                concerns_lines = str(row.get("top_concerns", "")).strip().split("\n")
                concerns_formatted = "\n".join(f"    {line.strip()}" for line in concerns_lines if line.strip())

                risk_val = row.get("risk_score")
                risk_pct = None
                try:
                    risk_pct = f"{float(risk_val) * 100:.0f}" if risk_val is not None and str(risk_val) != "nan" else None
                except Exception:
                    risk_pct = None

                section = f"""📋 Patient ID: {row.get('idx')}
    Visit Date: {row.get('visit_date')}
    Risk Score: {risk_pct if risk_pct is not None else row.get('risk_score')}
    Follow-up 1 Month: {row.get('follow_up_1mo', 'N/A')}
    Follow-up 6 Months: {row.get('follow_up_6mo', 'N/A')}
    Oncology Recommended: {row.get('oncology_rec', 'N/A')}
    Cardiology Recommended: {row.get('cardiology_rec', 'N/A')}
    Top Medical Concerns:
{concerns_formatted}
    ----------------------------------------"""
                sections.append(section)

        if sections:
            patient_summaries = "\n".join(sections).strip()

            # ==========================================================
            # ✅ INSERTED UPDATE: guardrail truncation for LangChain email
            # ==========================================================
            max_email_chars = int(os.getenv("EMAIL_LLM_MAX_CHARS", "12000"))
            if len(patient_summaries) > max_email_chars:
                logger.warning(
                    "📎 patient_summaries is %d chars; truncating to %d for email drafting.",
                    len(patient_summaries), max_email_chars
                )
                patient_summaries = patient_summaries[:max_email_chars] + "\n\n[TRUNCATED]"
            # ==========================================================
            # ✅ END INSERTED UPDATE
            # ==========================================================

            # Option A: Use LangChain to draft a clinician-friendly email from deterministic sections.
            # This gives you a clear 2nd LangChain-driven LLM call (portfolio signal) while keeping
            # the "facts" deterministic (reduces hallucination risk).
            if USE_LANGCHAIN:
                try:
                    from src.llm_chain import draft_clinician_email_with_langchain  # type: ignore

                    clinic_context = os.getenv("CLINIC_CONTEXT", "").strip()

                    drafted = draft_clinician_email_with_langchain(
                        patient_summaries=patient_summaries,
                        clinic_context=clinic_context,
                        model=OPENAI_MODEL,
                        temperature=OPENAI_TEMPERATURE,
                        max_tokens=OPENAI_MAX_TOKENS,
                        timeout_sec=OPENAI_TIMEOUT_SEC,
                    )

                    # If you didn't explicitly set EMAIL_SUBJECT, you can let the model provide one
                    if not os.getenv("EMAIL_SUBJECT"):
                        try:
                            email_subject = str(drafted.subject).strip() or email_subject
                        except Exception:
                            pass

                    try:
                        email_body = str(drafted.body).strip()
                    except Exception:
                        email_body = ""

                    if not email_body:
                        raise RuntimeError("LangChain returned empty email body")

                    logger.info("🧩 LangChain drafted clinician email. Subject: %s", email_subject)

                except Exception as e:
                    logger.warning(
                        "LangChain email drafting failed (%s). Falling back to deterministic email.",
                        e,
                    )
                    email_body = f"""
Hello Team,

Please review the following high-risk patient notes and follow-up recommendations
from {start_date.date()} to {end_date.date()}:

{patient_summaries}

Regards,
Your Clinical Risk Bot
""".strip()

            else:
                # Deterministic email (no extra LLM call)
                email_body = f"""
Hello Team,

Please review the following high-risk patient notes and follow-up recommendations
from {start_date.date()} to {end_date.date()}:

{patient_summaries}

Regards,
Your Clinical Risk Bot
""".strip()

            if dry_run_email:
                logger.info("🧪 DRY_RUN_EMAIL=true — skipping SES send. Email body below:\n%s", email_body)
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
        "use_langchain": USE_LANGCHAIN,
    }

    audit_bucket = os.getenv("AUDIT_BUCKET", output_bucket)
    audit_prefix = os.getenv("AUDIT_PREFIX", "audit_logs")
    audit_key = f"{audit_prefix}/{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')}_summary.json"
    log_audit_summary(s3, audit_bucket, audit_key, summary)
    logger.info("📁 Audit log written to s3://%s/%s", audit_bucket, audit_key)
    logger.info("📊 Run Summary:\n%s", json.dumps(summary, indent=2))
    logger.info("✅ Script completed in %.2f seconds", time.time() - start_time)

# ---------- CLI wrapper ----------
def main():
    parser = argparse.ArgumentParser(description="Patient Risk Pipeline")
    parser.add_argument("--input", dest="input_s3", default=os.getenv("INPUT_S3"),
                        help="s3://bucket/key.csv (or set INPUT_S3)")
    parser.add_argument("--output", dest="output_s3", default=os.getenv("OUTPUT_S3"),
                        help="s3://bucket/key.csv (or set OUTPUT_S3)")
    parser.add_argument("--email-to", dest="email_to", default=os.getenv("EMAIL_TO"),
                        help="Recipient email (or set EMAIL_TO)")
    parser.add_argument("--email-from", dest="email_from", default=os.getenv("EMAIL_FROM"),
                        help="Sender email (verified in SES; or set EMAIL_FROM)")
    parser.add_argument("--subject", dest="email_subject", default=os.getenv("EMAIL_SUBJECT", "High-Risk Patient Report"))
    parser.add_argument("--threshold", type=float, default=float(os.getenv("THRESHOLD", "0.95")))
    parser.add_argument("--start-date", dest="start_date", default=os.getenv("START_DATE"))
    parser.add_argument("--end-date", dest="end_date", default=os.getenv("END_DATE"))
    parser.add_argument("--physician-ids", dest="physician_ids", default=os.getenv("PHYSICIAN_ID_LIST"))
    parser.add_argument("--dry-run-email", action="store_true",
                        default=(os.getenv("DRY_RUN_EMAIL", "false").lower() == "true"))
    parser.add_argument("--max-notes", type=int, default=int(os.getenv("MAX_NOTES", "0") or 0))
    parser.add_argument("--region", dest="aws_region", default=os.getenv("AWS_REGION", "us-east-1"))

    args = parser.parse_args()

    # Fail-fast requireds (env or args)
    missing = []
    if not args.input_s3:
        missing.append("INPUT_S3/--input")
    if not args.output_s3:
        missing.append("OUTPUT_S3/--output")
    if not args.email_to:
        missing.append("EMAIL_TO/--email-to")
    if not args.email_from:
        missing.append("EMAIL_FROM/--email-from")
    if missing:
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
