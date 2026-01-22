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

from src.pipeline_core import run_pipeline

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
