# 🧠 **Agentic AI Patient Risk Pipeline**

📌 **Overview**

This project implements a fully containerized agentic AI workflow for analyzing clinical notes.
It runs on AWS ECS Fargate and uses OpenAI’s GPT models to surface high-risk patients and generate actionable summaries for clinicians.

Input: CSV patient notes in S3 (s3://<bucket>/Input/)

**Processing:**

Retrieve OpenAI API key securely from AWS Secrets Manager

Sequentially apply LLM prompts + regex/NLP parsing + risk scoring

Append results to the input CSV and save to S3 Output/

Send a summary email to clinicians via SES

**Output:**

Annotated CSVs in S3 (s3://<bucket>/Output/)

Audit logs in S3 (s3://<bucket>/Audit/)

SES email listing high-risk patients

⚠️ Clinical disclaimer: This pipeline does not replace clinicians. All flagged cases must be manually reviewed by qualified professionals before action is taken.


🏗 **Architecture**

S3 (Input CSVs) ──► ECS Fargate Task ──► S3 (Output CSVs)
 │
 ├──► OpenAI GPT (risk scoring + recommendations)
 └──► SES (summary email to clinicians)

**Key principles:**

Human-in-the-loop: clinicians review all outputs

Security: Secrets Manager for API keys, least-privilege IAM roles

Traceability: logs + outputs stored in S3


🧾 **Prompt Design (RISEN Framework)**

Prompt engineering followed the RISEN framework (Role, Input, Steps, Expectation, Narrowing):

Role: instruct GPT to act as a primary care physician

Input: provide raw unstructured clinical notes

Steps: risk scoring, explanation, follow-up recommendation prompts

Expectation: consistent numeric risk scores (1–100) + concise justifications

Narrowing: follow-up prompts only triggered for high-risk cases

Subsequent prompts summarize top medical concerns and generate specialty-specific follow-up recommendations.


📂 **Repo Structure**

src/                        # Core pipeline logic
    patient_risk_pipeline.py   # Main entrypoint (CLI + ECS)

fargate_deployment/         # Deployment automation
    scripts/                # Shell helpers
        build_and_push.sh       # Build + push Docker image
        deploy_to_fargate.sh    # Register & run task definition
        run_all.sh              # One-shot deploy + execute
        run_local.sh            # Local dry-run (LLM_DISABLED, DRY_RUN_EMAIL)
        fetch_artifacts.sh      # Download outputs from S3
        seed_sample_input.sh    # Upload sample CSV
        setup_iam.sh            # Create IAM roles & policies
        teardown_all.sh         # Cleanup IAM + resources
        validate_config.sh      # Sanity-check config.env
    templates/                 # ECS task definition JSONs

config.env.example           # Editable runtime configuration
requirements.txt             # Python + test deps
Dockerfile                   # Container build
pytest.ini                   # Pytest config
test/                        # Unit tests


🚀 **Quick Start**

1. Configure environment
cp config.env.example config.env
 Edit with AWS account, region, S3 paths, SES emails, etc.

2. Deploy + run end-to-end
cd fargate_deployment/scripts
./run_all.sh


**Results:**

✅ Output CSV written to your S3 Output/ path

✅ Email notification sent via SES

✅ Audit JSON stored in Audit/ S3 prefix

⏱ **Scheduling**

The pipeline can be automated with EventBridge:

Example: run weekly to scan the last 7 days of notes

Custom: run on demand for specific clinician IDs, time windows, or thresholds

⚙️ **Runtime Configuration**

| Variable        | Purpose                                       | Example                          |
| --------------- | --------------------------------------------- | -------------------------------- |
| `INPUT_S3`      | Input CSV in S3                               | `s3://bucket/Input/notes.csv`    |
| `OUTPUT_S3`     | Output CSV in S3                              | `s3://bucket/Output/results.csv` |
| `EMAIL_FROM`    | SES-verified sender email                     | `alerts@mydomain.com`            |
| `EMAIL_TO`      | Recipient email                               | `team@mydomain.com`              |
| `THRESHOLD`     | Risk cutoff (0–1.0)                           | `0.95`                           |
| `LLM_DISABLED`  | Stub responses, skip OpenAI                   | `true`                           |
| `DRY_RUN_EMAIL` | Log email body but don’t send                 | `true`                           |
| `MAX_NOTES`     | Cap # of notes processed (testing)            | `5`                              |
| `RUN_ID`        | Unique identifier (auto-generated if missing) | `2025-08-24T12:00:00Z`           |
| `LOG_FORMAT`    | `json` (default) or `text`                    | `text`                           |
| `LOG_LEVEL`     | Logging verbosity                             | `DEBUG`                          |


🧪 **Testing**

Run local unit tests (no AWS calls):

pip install -r requirements.txt
pytest


🔒 **Security Notes**

Secrets: API keys stored in AWS Secrets Manager, not committed.

IAM: Execution role (ECR/logs/secrets) vs Task role (S3/SES/Secrets).

SES Sandbox: requires verifying EMAIL_FROM and EMAIL_TO.

.gitignore: prevents leaking config/env secrets.


🎉 **Demo Workflow**

One-time IAM setup
./fargate_deployment/scripts/setup_iam.sh

Run full pipeline
./fargate_deployment/scripts/deploy_to_fargate.sh

Fetch outputs locally
./fargate_deployment/scripts/fetch_artifacts.sh

Artifacts appear in /tmp/patient-pipeline-artifacts/ and your SES inbox.


📊 **Future Directions**

Interactive review portal (web UI)

Fine-tuned clinical LLMs

Multi-modal input (labs, imaging, notes)

© 2025 Timothy Waterman • Hosted project website

**Project Walkthrough:**
https://wbst-bkt.s3.us-east-1.amazonaws.com/patient_index.html