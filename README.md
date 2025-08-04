# 🧠 Agentic AI Risk Pipeline

This project implements a **fully serverless risk scoring pipeline**—processing incoming patient clinical notes, generating care recommendations via OpenAI GPT, and emailing clinicians—all running on **AWS Fargate + ECS** with automated deployment.

---

## ✅ Key Features

- Token‑based LLM risk scoring with OpenAI
- Automated care recommendation generation
- Alert and email summary via Amazon SES
- Storage of reports and audits in Amazon S3
- Scheduled Fargate-based ECS task (no EC2 hosts required)

> ⚠️ EC2 legacy code has been removed. If you see references like `run_project`, `ec2_deployment/`, or `Dockerfile` configured for EC2 logic, they’re obsolete.

---

## 📦 Code & Configuration Files

| File | Purpose |
|------|---------|
| `Dockerfile` + `requirements.txt` | Builds the container image for deployment (OpenAI SDK, boto3, pandas, etc.) |
| `patients_pipeline.py` | Contains the orchestration logic: S3 download, LLM scoring, SES email, output and audit to S3 |
| `env.list.example` | Template for runtime vars (e.g. S3 bucket, risk threshold, physician IDs, email addresses) |
| `fargate_deployment/task‑def‑template.json` | Base task definition: CPU/memory, logging, empty placeholder for image & environment |
| `fargate_deployment/generate_task_def.py` | Populates the template with actual image URI, IAM roles, and env vars → produces `task‑def.json` |
| `fargate_deployment/ecs‑trust‑policy.json` | IAM trust policy for ECS tasks to assume the container role |
| `fargate_deployment/s3‑access‑policy.json` | IAM policy permissions (S3 & SES) for your container's runtime role |
| `fargate_deployment/deploy_to_fargate.sh` | Orchestrates:
1. Role creation (if needed),
2. Task definition generation,
3. Registers new revision to ECS,
4. Optionally launches the task manually (e.g. for dev/test runs) |

---

## 🎯 How the Fargate Deployment Flow Works

1. **Build the Docker image** (your local machine or CI):
   ```bash
   docker build --pull -t patient-pipeline:latest .
   docker push <your‑ecr‑repo>/patient‑pipeline:latest
