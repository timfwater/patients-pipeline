# 🧠 Agentic AI Patient Risk Pipeline

A fully containerized **agentic AI workflow** for analyzing clinical notes and surfacing high-risk patients for clinician review. The pipeline runs on **AWS ECS Fargate** and uses **OpenAI GPT models** plus lightweight parsing and scoring logic to generate concise, actionable summaries.

---

## 📌 Overview

**Input**

- CSV of patient notes in S3  
  `s3://<bucket>/Input/notes.csv`

**Processing**

1. Retrieve OpenAI API key securely from **AWS Secrets Manager**.
2. Load input notes from S3.
3. Apply *RISEN*-style LLM prompts for:
   - Risk scoring (1–100)
   - Explanatory text
   - Follow-up recommendations for high-risk cases
4. Parse/validate outputs and compute a normalized risk score.
5. Append results to the original CSV.
6. Write annotated CSV + audit artifacts back to S3.
7. Send a summary email via **Amazon SES** listing high-risk patients.

**Output**

- Annotated CSV(s) in S3  
  `s3://<bucket>/Output/...`
- Audit logs and JSON artifacts in  
  `s3://<bucket>/Audit/...`
- SES email summarizing the high-risk cohort (for clinician review).

> ⚠️ **Clinical disclaimer:**  
> This pipeline is for **decision support** and **demonstration** only. It does **not** replace clinicians. All flagged cases **must** be manually reviewed by qualified professionals before any action is taken.

---

## 🏗 Architecture

```text
S3 (Input CSVs)
      │
      ▼
ECS Fargate Task ────────────────┐
      │                          │
      ▼                          │
S3 (Output CSVs + Audit)         │
      │                          │
      ├─► OpenAI GPT (risk, concerns, recommendations)
      └─► Amazon SES (summary email to clinicians)
```

**Key principles**

- **Human-in-the-loop:** clinicians review all outputs.
- **Security by design:** Secrets Manager for API keys; least-privilege IAM roles.
- **Traceability:** logs and structured artifacts persisted to S3.

---

## 🧾 Prompt Design (RISEN Framework)

Prompting follows a **RISEN** pattern: **Role, Input, Steps, Expectation, Narrowing**

- **Role:** Instruct GPT to act as a primary care physician reviewing a patient panel.
- **Input:** Raw, unstructured clinical notes + limited structured context if available.
- **Steps:**
  - Assign a numeric risk score (1–100).
  - Explain key concerns driving the score.
  - Suggest follow-up actions and specialties.
- **Expectation:**
  - Stable, machine-parsable numeric risk scores.
  - Concise, clinically plausible explanations.
- **Narrowing:**
  - Follow-up prompts and email inclusion triggered **only** for high-risk cases (above threshold).

Subsequent prompts summarize top medical concerns and generate specialty-level and visit-level recommendations.

---

## 📂 Repo Structure

```text
src/                         # Core pipeline logic
    __init__.py
    patient_risk_pipeline.py # Main entrypoint (CLI + ECS task handler)

fargate_deployment/
    scripts/                 # Deployment + ops automation
        build_and_push.sh        # Build + push Docker image to ECR
        deploy_to_fargate.sh     # Register + run ECS task definition
        fetch_artifacts.sh       # Download S3 outputs to local /tmp
        run_all.sh               # One-shot: build, deploy, run, fetch
        run_local.sh             # Local dry-run (LLM_DISABLED/DRY_RUN_EMAIL)
        setup_iam.sh             # Create IAM roles + policies
        teardown_all.sh          # Tear down IAM + ECS artifacts
        validate_config.sh       # Sanity-check config.env before deploy
    templates/
        task-def-template.json   # Base ECS task definition template

config.env.example            # Fargate/ECS/S3/SES/Secrets config template
config.env                    # (User-specific) runtime configuration
env.list.example              # Example container env var file (optional)

Dockerfile                    # Container build
requirements.txt              # Python deps (pipeline + tests)
pytest.ini                    # Pytest configuration

test/                         # Unit tests for parsing + schema logic
    test_extract_risk.py
    test_parsing.py
    test_schema_merge.py
```

> This repo is designed as a **production-style, one-click deploy** demo of an LLM-powered clinical workflow on AWS.

---

## 🚀 Quick Start

### 1. Prerequisites

- AWS account with programmatic access
- **AWS CLI** configured with appropriate credentials
- **Docker** installed locally
- SES configured in your region (verify `EMAIL_FROM` and `EMAIL_TO` or move out of the SES sandbox)
- An OpenAI API key stored in **AWS Secrets Manager**

### 2. Configure environment

```bash
cp config.env.example config.env
```

Then edit `config.env` with:

- AWS region
- S3 bucket + prefixes for Input/Output/Audit
- ECS cluster / subnets / security group
- Names/ARNs for task roles and execution roles (or let `setup_iam.sh` create them)
- SES sender and recipient emails
- Name of the OpenAI secret in Secrets Manager

If you use an `env.list` file for container environment variables, copy and adapt:

```bash
cp env.list.example env.list   # optional, if your scripts reference it
```

### 3. One-shot deploy + run (happy path)

From the repo root:

```bash
cd fargate_deployment/scripts
./run_all.sh
```

`run_all.sh` will:

1. Validate `config.env`.
2. Build the Docker image and push it to ECR.
3. Render a task definition from `task-def-template.json`.
4. Run the ECS task on Fargate.
5. Fetch artifacts from S3 to your local machine.

**Expected results**

- Annotated output CSV written to your S3 `Output/` prefix  
- Audit JSON/logs written to your S3 `Audit/` prefix  
- Summary email delivered via SES to your configured recipients  
- Local copies of key artifacts under `/tmp/patient-pipeline-artifacts/`

---

## 🧪 Local Testing

Run tests without touching AWS:

```bash
pip install -r requirements.txt
pytest
```

These tests cover:

- Risk extraction/parsing of GPT responses
- Schema merge logic when appending model outputs to the input CSV
- Basic parsing edge cases (unexpected formats, missing fields, etc.)

---

## ⚙️ Runtime Configuration

At runtime, the task uses environment variables (from `config.env`, `env.list`, or the ECS task definition) such as:

| Variable        | Purpose                                       | Example                          |
|----------------|-----------------------------------------------|----------------------------------|
| INPUT_S3       | Input CSV in S3                               | s3://bucket/Input/notes.csv      |
| OUTPUT_S3      | Output CSV in S3                              | s3://bucket/Output/results.csv   |
| EMAIL_FROM     | SES-verified sender email                     | alerts@mydomain.com              |
| EMAIL_TO       | Recipient email(s)                            | team@mydomain.com                |
| THRESHOLD      | Risk cutoff (0–1.0)                           | 0.95                             |
| LLM_DISABLED   | Stub responses, skip OpenAI calls             | true                             |
| DRY_RUN_EMAIL  | Log email body but don’t send                 | true                             |
| MAX_NOTES      | Cap number of notes processed (for testing)   | 5                                |
| RUN_ID         | Unique run identifier (auto-generated if empty)| 2025-08-24T12:00:00Z             |
| LOG_FORMAT     | json (default) or text                        | text                             |
| LOG_LEVEL      | Logging verbosity                             | DEBUG                            |

Refer to `config.env.example` for the complete set used by your deployment scripts.

---

## 🔒 Security Notes

- Secrets  
  - OpenAI API keys live in **AWS Secrets Manager**; they are **never** committed to the repo.
- IAM separation of concerns  
  - Task execution role: pull from ECR, write CloudWatch logs.
  - Task role: access S3, SES, and Secrets Manager at the minimum required scope.
- SES Sandbox  
  - In sandbox, SES requires verifying both EMAIL_FROM and EMAIL_TO.  
  - For production use, request sandbox removal or adjust identities as needed.
- .gitignore  
  - Ensures config.env, env.list, and other sensitive files are not committed.

---

## 🎉 Demo & Workflow Examples

### One-time IAM setup

From `fargate_deployment/scripts`:

```bash
./setup_iam.sh
```

Creates or updates the IAM roles and policies expected by your task definition.

### Run full pipeline on Fargate

If you prefer manual steps instead of `run_all.sh`:

```bash
./build_and_push.sh
./deploy_to_fargate.sh
```

Wait for the task to complete (or watch CloudWatch logs) and then:

```bash
./fetch_artifacts.sh
```

Artifacts will appear under `/tmp/patient-pipeline-artifacts/` and in your configured S3 Output/Audit prefixes, and the SES email will be sent.

### Local dry-run

For debugging pipeline logic without real LLM calls or emails:

```bash
./run_local.sh
```

Configure `LLM_DISABLED=true` and/or `DRY_RUN_EMAIL=true` in your environment so that:

- LLM prompts are skipped or stubbed.
- Email bodies are logged instead of sent.

---

## 📊 Future Directions

- Interactive review portal (web UI) driven by S3 artifacts
- Integration with fine-tuned or domain-specific clinical LLMs
- Multi-modal inputs (labs, imaging, vitals, structured EHR fields)
- Tiered routing (e.g., auto-assign to specialty teams based on concern categories)

---

© 2026 Timothy Waterman • Project walkthrough website:  
https://wbst-bkt.s3.us-east-1.amazonaws.com/patient_index.html
