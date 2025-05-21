import pandas as pd
import re
import logging
import os
import boto3
import openai
from datetime import datetime

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
def extract_risk_score(text):
    if not isinstance(text, str):
        return None
    match = re.search(r'Risk Score:\s*(\d{1,3})', text)
    if match:
        score = int(match.group(1))
        if 0 <= score <= 100:
            return score / 100.0
    return None

def get_chat_response(inquiry_note, model="gpt-3.5-turbo"):
    client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": inquiry_note}]
    )
    return {"message": {"content": response.choices[0].message.content}}

def query_combined_prompt(note, template=COMBINED_PROMPT):
    prompt = template.format(note=note)
    response = get_chat_response(prompt)['message']['content']
    return response

def parse_response_and_concerns(text):
    try:
        parts = text.strip().split('\n')
        f1mo = parts[0].split(':')[1].strip()
        f6mo = parts[1].split(':')[1].strip()
        onc = parts[2].split(':')[1].strip()
        card = parts[3].split(':')[1].strip()
        concerns_start = next(i for i, line in enumerate(parts) if "Top Medical Concerns" in line)
        concerns_lines = parts[concerns_start+1:]
        concerns_text = '\n'.join(concerns_lines).strip()
        return pd.Series([f1mo, f6mo, onc, card, concerns_text],
                         index=['follow_up_1mo', 'follow_up_6mo', 'oncology_rec', 'cardiology_rec', 'top_concerns'])
    except Exception as e:
        logger.warning(f"Failed to parse response: {e}")
        return pd.Series([None]*5, index=['follow_up_1mo', 'follow_up_6mo', 'oncology_rec', 'cardiology_rec', 'top_concerns'])

# --- Main ---
def main():
    input_s3 = os.environ["INPUT_S3"]
    output_s3 = os.environ["OUTPUT_S3"]
    email_to = os.environ["EMAIL_TO"]
    email_from = os.environ["EMAIL_FROM"]
    threshold = float(os.environ.get("THRESHOLD", 0.8))
    start_date = pd.to_datetime(os.environ.get("START_DATE", "2024-05-01"))
    end_date = pd.to_datetime(os.environ.get("END_DATE", "2024-05-01"))

    input_bucket, input_key = input_s3.replace("s3://", "").split("/", 1)
    output_bucket, output_key = output_s3.replace("s3://", "").split("/", 1)

    local_input = '/tmp/input.csv'
    local_output = '/tmp/output.csv'

    s3 = boto3.client('s3')
    logger.info("Downloading input file from S3...")
    s3.download_file(input_bucket, input_key, local_input)

    logger.info("Loading input CSV...")
    df = pd.read_csv(local_input)
    df_original = df.copy()

    logger.info("Running risk assessments...")
    df['risk_rating'] = df['full_note'].apply(lambda note: get_chat_response(RISK_PROMPT + note)['message']['content'])
    df['risk_score'] = df['risk_rating'].apply(extract_risk_score)

    if df['risk_score'].dropna().empty:
        logger.warning("No valid risk scores found. Exiting.")
        return

    logger.info("Generating care recommendations where risk > threshold...")
    high_risk_df = df.nlargest(int((1 - threshold) * len(df)), 'risk_score')
    df.loc[high_risk_df.index, 'combined_response'] = high_risk_df['full_note'].apply(query_combined_prompt)

    parsed = df['combined_response'].dropna().apply(parse_response_and_concerns)
    df[parsed.columns] = parsed

    logger.info("Merging model output onto original input DataFrame...")
    columns_to_keep = [
        "idx", "visit_date", "risk_rating", "risk_score", "combined_response",
        "follow_up_1mo", "follow_up_6mo", "oncology_rec", "cardiology_rec", "top_concerns"
    ]
    columns_to_merge = [col for col in columns_to_keep if col not in df_original.columns or col in ["idx", "visit_date"]]

    df_final = df_original.merge(
        df[columns_to_merge],
        on=["idx", "visit_date"],
        how="left"
    )

    logger.info("Saving merged output to local file...")
    df_final.to_csv(local_output, index=False)

    logger.info("Uploading merged output to S3...")
    s3.upload_file(local_output, output_bucket, output_key)

    logger.info("Filtering by date range and preparing email summary...")
    df['visit_date'] = pd.to_datetime(df['visit_date'], errors='coerce')
    mask_time = (df['visit_date'] >= start_date) & (df['visit_date'] <= end_date)

    df_email = df[
        mask_time &
        df['risk_score'].notna() &
        (df['risk_score'] > threshold)
    ]

    if df_email.empty:
        logger.info("No high-risk patients in selected date range.")
        return

    sections = []
    for idx, row in df_email.iterrows():
        concerns_raw = row.get('top_concerns', '')
        concerns_lines = str(concerns_raw or '').strip().split('\n')
        concerns_formatted = '\n'.join(f"    {line.strip()}" for line in concerns_lines if line.strip())

        note = f"""📋 Patient ID: {row['idx'] if pd.notna(row['idx']) else 'N/A'}
    Visit Date: {row.get('visit_date', 'N/A')}
    Risk Score: {row.get('risk_score', 'N/A')}
    Follow-up 1 Month: {row.get('follow_up_1mo', 'N/A')}
    Follow-up 6 Months: {row.get('follow_up_6mo', 'N/A')}
    Oncology Recommended: {row.get('oncology_rec', 'N/A')}
    Cardiology Recommended: {row.get('cardiology_rec', 'N/A')}
    Top Medical Concerns:
{concerns_formatted}
    ----------------------------------------"""
        sections.append(note)

    email_body = f"""
Hello Team,

Please review the following high-risk patient notes and follow-up recommendations
from {start_date.date()} to {end_date.date()}:

{chr(10).join(sections)}

Regards,
Your Clinical Risk Bot
"""

    try:
        logger.info("Sending email via SES...")
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
        logger.error("❌ Failed to send email: %s", e)

if __name__ == "__main__":
    main()
