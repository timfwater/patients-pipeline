# 🧠 Patient Risk Assessment Pipeline

## 📌 Overview
This project runs a fully containerized **clinical note analysis pipeline** on AWS ECS Fargate.

- **Input:** Patient notes from S3 (`s3://<bucket>/Input/`)
- **Processing (ECS Fargate):**
  - Reads notes from S3
  - Retrieves the OpenAI API key from AWS Secrets Manager (or env fallback)
  - Calls OpenAI API to assess patient risk and generate follow-up recommendations
  - Writes annotated CSV back to S3 (`Output/`)
  - Sends an SES email summary listing high-risk patients
- **Security:**
  - **Execution Role** — allows ECS to pull images, write logs, and retrieve secrets  
  - **Task Role** — scoped for S3 read/write, SES email sending, and optional secret fetches

---

## 🏗 Architecture


S3 (Input CSVs) ──► ECS Fargate Task ──► S3 (Output CSVs)  
│  
├──► OpenAI (risk scoring + recommendations)  
└──► SES (summary email)

---

## 📂 Repo Structure

```text
src/                    # Application logic
    patient_risk_pipeline.py    # main pipeline (CLI + ECS entrypoint)

fargate_deployment/     # Deployment automation
    scripts/            # Shell deployment helpers
        build_and_push.sh      # Build + push Docker image
        deploy_to_fargate.sh   # Register & run task definition
        run_all.sh             # Full one-shot deployment
        run_local.sh           # Local dry-run (no SES, throttleable)
        fetch_artifacts.sh     # Pull latest output & audit logs from S3
        seed_sample_input.sh   # Uploads a toy CSV input to S3
        setup_iam.sh           # Creates/attaches IAM roles & policies
        teardown_all.sh        # Clean up roles/resources
        validate_config.sh     # Sanity check config.env
    policies/           # IAM trust & access JSON policies
    templates/          # ECS task definition templates

config.env.example       # Sample environment configuration  
requirements.txt         # Python + pytest dependencies  
Dockerfile               # Container build file  
pytest.ini               # Pytest settings  
test/                    # Unit tests (risk extraction, parsing, schema merge)

```

## 🚀 Quick Start

The easiest way to deploy is with the one-shot runner:

```bash
# 1. Configure
cp config.env.example config.env
# edit config.env with your AWS_ACCOUNT_ID, region, S3 paths, SES emails, etc.

# 2. Deploy end-to-end
cd fargate_deployment/scripts
./run_all.sh

```

After this completes:

    ✅ Processed CSV is written to your S3 Output path

    ✅ Email notification is sent via SES

    ✅ Audit JSON summary is stored in s3://<AUDIT_BUCKET>/<AUDIT_PREFIX>/


🛠 Useful Scripts
- `setup_iam.sh` — one-time IAM role/policy bootstrap  
- `build_and_push.sh` — build & push Docker image  
- `deploy_to_fargate.sh` — register & run ECS task (with Secrets Manager injection)  
- `run_local.sh` — local smoke test (supports LLM_DISABLED=true, DRY_RUN_EMAIL=true, MAX_NOTES=5)  
- `fetch_artifacts.sh` — download latest output CSV + audit JSON locally  
- `seed_sample_input.sh` — upload toy CSV so you can demo pipeline quickly  
- `teardown_all.sh` — remove IAM roles/policies when cleaning up  


## ⚙️ Runtime Configuration (env vars)

| Variable        | Purpose                                           | Example |
|-----------------|---------------------------------------------------|---------|
| `INPUT_S3`      | Input CSV path in S3                              | `s3://my-bucket/Input/notes.csv` |
| `OUTPUT_S3`     | Output CSV path in S3                             | `s3://my-bucket/Output/results.csv` |
| `EMAIL_FROM`    | SES-verified sender email                         | `alerts@mydomain.com` |
| `EMAIL_TO`      | Recipient email                                   | `team@mydomain.com` |
| `THRESHOLD`     | Risk score cutoff (0–1.0)                         | `0.95` |
| `LLM_DISABLED`  | Skip OpenAI API, return stub responses (fast test) | `true` |
| `DRY_RUN_EMAIL` | Don’t send SES email, just log body               | `true` |
| `MAX_NOTES`     | Cap number of notes processed (for testing)       | `5` |
| `RUN_ID`        | Unique run identifier (auto-set if not provided)  | `2025-08-24T12:00:00Z` |
| `LOG_FORMAT`    | `json` (default) or `text`                        | `text` |
| `LOG_LEVEL`     | Logging verbosity                                 | `DEBUG` |



🧪 Testing

This repo includes lightweight unit tests (no AWS calls):

pip install -r requirements.txt
pytest

Covers:

    Risk score extraction

    Parsing of model responses

    Schema merge logic

🔒 Security Notes

    No secrets are committed — OpenAI API key lives in AWS Secrets Manager.

    IAM roles follow least privilege principle.

    .gitignore ensures sensitive files (like config.env) are not committed.

    SES sandbox requires verifying both EMAIL_FROM and EMAIL_TO addresses.

🎉 Demo Workflow

# One-time IAM setup
./fargate_deployment/scripts/setup_iam.sh

# Upload toy input CSV
./fargate_deployment/scripts/seed_sample_input.sh

# Run full pipeline
./fargate_deployment/scripts/run_all.sh

# Fetch outputs locally
./fargate_deployment/scripts/fetch_artifacts.sh

Artifacts will be in /tmp/patient-pipeline-artifacts/ and your SES inbox.