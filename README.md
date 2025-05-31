🧠 Agentic AI Risk Pipeline

This project demonstrates an end-to-end, agentic AI system for analyzing clinical notes and flagging high-risk patients. It integrates OpenAI GPT-based inference with AWS (S3, SES, EC2, Fargate) to automate risk scoring, care recommendations, reporting, and email delivery.

✅ Key Features

Scores patient risk using GPT-based prompts

Recommends follow-up or specialist care

Sends a summary email to clinicians

Uploads merged output and audit log to S3

Supports both EC2 and fully serverless Fargate deployments

🚀 Deployment Options

The system supports two deployment modes:

Method

Description

Status

EC2 + Docker

Manual Docker execution on EC2

✅ Production-ready

AWS Fargate

Serverless ECS task (templated)

✅ Fully automated

EC2 Mode: Uses launch_ec2_pipeline_instance.sh to create an EC2, SSH, and run Docker.

Fargate Mode: Uses generate_task_def.py and deploy_to_fargate.sh for seamless CLI deployment.

📁 Input Format

Input S3 file: augmented_input.csv

Required columns:

idx: unique row ID

visit_date: YYYY-MM-DD

full_note: unstructured clinical text

physician_id: int (for optional filtering)

🧐 Model Logic

Prompt

Output columns

RISK_PROMPT

risk_rating, risk_score

COMBINED_PROMPT

followup_1mo, cardiology_flag, ...

Follow-up questions are asked only for top N% risk patients (configurable).

📧 Email Summary

Generated email includes:

Patient ID and risk score

Flags: follow-up (1/6 mo), oncology, cardiology

Top 5 extracted medical concerns

✅ Output Files

output.csv (merged input + model results)

audit_logs/*.json (runtime metadata, filters used, counts, flags)

🔐 Secrets & Env Vars

Stored securely via AWS:

OpenAI Key in Secrets Manager → openai/api-key

Defined via env or ECS task:

Name

Purpose

INPUT_S3

S3 URI to input CSV

OUTPUT_S3

Output path on S3

EMAIL_TO

SES destination address

EMAIL_FROM

SES verified sender

PHYSICIAN_ID_LIST

Optional filtering (comma-separated)

START_DATE / END_DATE

Filter input by visit_date

THRESHOLD

Risk score percentile cutoff (0-1)

AWS_REGION

Deployment region (e.g. us-east-1)

🚧 Fargate CLI Example

aws ecs run-task \
  --cluster patient-pipeline-cluster \
  --launch-type FARGATE \
  --network-configuration ... \
  --task-definition patient-pipeline-task:XX \
  --overrides file://env-overrides.json

Task defs and overrides generated via:

generate_task_def.py

deploy_to_fargate.sh

📆 Project Tree (Key Files)

├── patients_pipeline.py          # Main pipeline script
├── Dockerfile                    # Image config
├── run_project                   # Menu to launch EC2 or Fargate
├── ec2_deployment/               # EC2-specific scripts and env
│   └── launch_ec2_pipeline_instance.sh
├── fargate_deployment/           # Fargate deploy logic
│   ├── task-def-template.json
│   ├── generate_task_def.py
│   └── deploy_to_fargate.sh

📊 Goals & Highlights

✅ Deploy a fully automated note-to-alert pipeline

✅ Apply LLMs in clinical triage scenarios

✅ Use AWS-native services securely and scalably

📧 Contact

Timothy Watermantimfwater [at] gmail [dot] com

