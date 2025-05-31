import pandas as pd
from io import StringIO
import re
import logging
import os
import boto3
import openai
from datetime import datetime, timedelta
import ast
import warnings
import time
import json
import requests

# --- Retrieve OpenAI API key from AWS Secrets Manager ---
def get_openai_key_from_secrets(secret_name="openai/api-key", region_name="us-east-1"):
    try:
        client = boto3.client("secretsmanager", region_name=region_name)
        response = client.get_secret_value(SecretId=secret_name)
        return response["SecretString"]
    except Exception as e:
        raise RuntimeError(f"❌ Failed to retrieve OpenAI API key from Secrets Manager: {e}")

openai.api_key = get_openai_key_from_secrets()

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

print("✅ SCRIPT IS RUNNING")

# --- Prompts ---
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

# --- Utility Functions ---

def get_ecs_metadata_task_id():
    try:
        metadata_uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
        if not metadata_uri:
            return None
        resp = requests.get(f"{metadata_uri}/task")
        if resp.ok:
            data = resp.json()
            task_arn = data.get("TaskARN", "")
            task_id = task_arn.split("/")[-1]
            # Optional: fetch log stream name
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

def get_chat_response(inquiry_note, model="gpt-3.5-turbo", retries=3, delay=5):
    for attempt in range(retries):
        try:
            response = openai.ChatCompletion.create(
                model=model,
                messages=[{"role": "user", "content": inquiry_note}]
            )
            return {"message": {"content": response.choices[0].message.content}}
        except Exception as e:
            logger.warning(f"Attempt {attempt+1} failed: {e}")
            time.sleep(delay)
    logger.error("All retries failed for OpenAI API.")
    return {"message": {"content": ""}}

def query_combined_prompt(note, template=COMBINED_PROMPT):
    prompt = template.format(note=note)
    response = get_chat_response(prompt)['message']['content']
    return response

def safe_split(line):
    return line.split(':', 1)[1].strip() if ':' in line else None

def parse_response_and_concerns(text):
    try:
        parts = text.strip().split('\n')
        f1mo = safe_split(parts[0])
        f6mo = safe_split(parts[1])
        onc = safe_split(parts[2])
        card = safe_split(parts[3])
        concerns_start = next(i for i, line in enumerate(parts) if "Top Medical Concerns" in line)
        concerns_lines = parts[concerns_start+1:]
        concerns_text = '\n'.join(concerns_lines).strip()
        return pd.Series([f1mo, f6mo, onc, card, concerns_text],
                         index=['follow_up_1mo', 'follow_up_6mo', 'oncology_rec', 'cardiology_rec', 'top_concerns'])
    except Exception as e:
        logger.warning(f"Failed to parse response: {e}")
        return pd.Series([None]*5, index=['follow_up_1mo', 'follow_up_6mo', 'oncology_rec', 'cardiology_rec', 'top_concerns'])

def log_audit_summary(s3_client, bucket, key, summary):
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(summary, indent=2).encode("utf-8")
    )

# --- Main ---
def main():
    start_time = time.time()
    logger.info("📌 Starting main() and reading env vars...")

    input_s3 = os.environ["INPUT_S3"]
    output_s3 = os.environ["OUTPUT_S3"]
    email_to = os.environ["EMAIL_TO"]
    email_from = os.environ["EMAIL_FROM"]
    threshold = float(os.environ.get("THRESHOLD", 0.95))
    logger.info(f"📊 Using risk threshold: {threshold}")
    physician_ids_raw = os.environ.get("PHYSICIAN_ID_LIST", "").strip()

    # --- Dynamic default date range (past 7 days) ---
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
    try:
        s3.download_file(input_bucket, input_key, local_input)
        logger.info("✅ Download succeeded.")
    except Exception as e:
        logger.error(f"❌ S3 download failed: {e}")
        raise

    logger.info("📥 Loading input CSV...")
    df = pd.read_csv(local_input)
    logger.info(f"Loaded {len(df)} rows.")

    required_cols = ["idx", "visit_date", "full_note", "physician_id"]
    missing = [col for col in required_cols if col not in df.columns]
    if missing:
        logger.error(f"Missing required columns: {missing}")
        return

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
        return

    logger.info(f"Filtered {len(df_filtered)} rows. Running risk assessments...")
    df_filtered['risk_rating'] = df_filtered['full_note'].apply(lambda note: get_chat_response(RISK_PROMPT + note)['message']['content'])
    df_filtered['risk_score'] = df_filtered['risk_rating'].apply(extract_risk_score)

    if df_filtered['risk_score'].dropna().empty:
        logger.warning("No valid risk scores.")
        return

    logger.info("⚕️ Generating care recommendations...")
    high_risk_df = df_filtered.nlargest(int((1 - threshold) * len(df_filtered)), 'risk_score')
    df_filtered.loc[high_risk_df.index, 'combined_response'] = high_risk_df['full_note'].apply(query_combined_prompt)

    logger.info("🧠 Parsing care recommendation responses...")
    parsed = df_filtered['combined_response'].dropna().apply(parse_response_and_concerns)
    df_filtered[parsed.columns] = parsed

    high_risk_df = df_filtered.loc[high_risk_df.index].copy()

    columns_to_merge = [
        "idx", "visit_date", "risk_rating", "risk_score", "combined_response",
        "follow_up_1mo", "follow_up_6mo", "oncology_rec", "cardiology_rec", "top_concerns"
    ]
    df_final = df_original.merge(df_filtered[columns_to_merge], on=["idx", "visit_date"], how="left")

    csv_buffer = StringIO()
    df_final.to_csv(csv_buffer, index=False)
    s3.put_object(Bucket=output_bucket, Key=output_key, Body=csv_buffer.getvalue())
    logger.info("✅ Final output written to S3.")

    df_email = high_risk_df.copy()
    logger.info(f"📨 Preparing email for {len(df_email)} high-risk patients...")

    if df_email.empty:
        logger.info("No high-risk patients detected — skipping email.")
        email_sent = False
    else:
        email_sent = True

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

    if email_sent:
        try:
            logger.info("📧 Sending email via SES...")
            ses = boto3.client("ses", region_name="us-east-1")
            response = ses.send_email(
                Source=email_from,
                Destination={"ToAddresses": [email_to]},
                Message={
                    "Subject": {"Data": "High-Risk Patient Report", "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": email_body, "Charset": "UTF-8"}}
                }
            )
            logger.info("✅ Email sent! Message ID: %s", response["MessageId"])
        except Exception as e:
            logger.error(f"❌ Failed to send email: {e}")

    summary = {
        "timestamp": datetime.utcnow().isoformat(),
        "physician_id": os.getenv("PHYSICIAN_ID_LIST"),
        "date_start": os.getenv("START_DATE"),
        "date_end": os.getenv("END_DATE"),
        "total_notes": len(df),
        "high_risk_count": len(high_risk_df),
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

