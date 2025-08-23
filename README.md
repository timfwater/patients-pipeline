# 🧠 Patient Risk Assessment Pipeline

## 📌 Overview
This project runs a fully containerized **clinical note analysis pipeline** on AWS ECS Fargate.

- **Input:** Patient notes from S3 (`s3://<bucket>/Input/`)
- **Processing (ECS Fargate):**
  - Reads notes from S3
  - Retrieves the OpenAI API key from AWS Secrets Manager
  - Calls OpenAI API to assess patient risk and recommend follow-ups
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
    patient_risk_pipeline.py

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
pytest.ini               # Pytest settings (quiet mode)  
test/                    # Lightweight local tests  
```

## 🚀 Quick Start

The easiest way to deploy is with the one-shot runner:

```bash
cp config.env.example config.env
# Edit config.env with your AWS_ACCOUNT_ID, region, S3 paths, SES emails, etc.

cd fargate_deployment/scripts
./run_all.sh
```

After this completes:

    ✅ Processed CSV is written to your S3 Output path

    ✅ Email notification is sent via SES

    ✅ Audit JSON summary is stored in s3://<AUDIT_BUCKET>/<AUDIT_PREFIX>/

🛠 Useful Scripts

    setup_iam.sh — one-time IAM roles/policies

    build_and_push.sh — build + push Docker image

    deploy_to_fargate.sh — register & run the ECS task

    run_local.sh — run the pipeline locally with DRY_RUN_EMAIL=true and MAX_NOTES=5

    fetch_artifacts.sh — download latest output CSV + audit JSON locally

    seed_sample_input.sh — upload a toy CSV so you can demo the pipeline quickly

    teardown_all.sh — remove IAM roles/policies when cleaning up

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