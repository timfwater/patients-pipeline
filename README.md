# Project Docs

## Architecture (High-Level)
- **Input**: Patient notes in S3 (`s3://.../input/`)
- **Compute**: ECS Fargate runs a Dockerized Python worker that:
  - Reads notes from S3
  - Calls OpenAI (secret from AWS Secrets Manager)
  - Writes annotated CSV back to S3
  - Sends SES email summary for high-risk cases
- **Security**: IAM task role (S3 read/write, SecretsManager GetSecretValue, SES SendEmail)

## Repo Layout
- `src/` — application code (`patient_risk_pipeline.py`)
- `fargate_deployment/` — deployment scripts (`scripts/`), IAM policies, ECS templates
- `docs/` — diagrams, sample outputs, screenshots

## Runbook (TL;DR)
1. Copy `config.env.example` → `config.env` and fill in values.
2. `./fargate_deployment/scripts/setup_iam.sh` (one-time).
3. `python ./fargate_deployment/scripts/generate_task_def.py` to render a task def.
4. `./fargate_deployment/scripts/deploy_to_fargate.sh` to build/push/run.

> See the root README for full instructions.
