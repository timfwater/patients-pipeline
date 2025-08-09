🧠 Patient Risk Assessment Pipeline
📌 Overview

This project runs a fully containerized clinical note analysis pipeline on AWS ECS Fargate.
It ingests patient notes from S3, uses an OpenAI model to assess risk, writes annotated results back to S3, and sends high-risk alerts via SES email — all without managing any servers.
🏗 Architecture

Input:

    Patient notes stored in an S3 bucket (s3://<bucket>/Input/)

Processing (ECS Fargate):

    Reads notes from S3

    Retrieves the OpenAI API key from AWS Secrets Manager

    Calls OpenAI API to assess patient risk and recommend follow-ups

    Writes annotated CSV to S3 (Output/)

    Sends an SES email summary listing high-risk patients

Security:

    Execution Role — grants ECS infrastructure access to pull images from ECR, write logs, and retrieve secrets

    Task Role — scoped to allow only necessary S3 read/write, SES email sending, and optional secret retrieval

📂 Repo Structure

src/                    # Application logic
    patient_risk_pipeline.py

fargate_deployment/     # Deployment automation
    scripts/            # Shell & Python deployment helpers
    policies/           # IAM trust & access policies
    templates/          # ECS task definition templates

config.env.example      # Sample environment configuration
requirements.txt        # Python dependencies
Dockerfile              # Container build file
README.md               # This file

🚀 Quick Start (Recommended)

The easiest way to deploy is to run everything in one command using the run_all.sh script — it will set up IAM roles, build & push the Docker image, and deploy the ECS Fargate task.

cp config.env.example config.env
# Fill in AWS_ACCOUNT_ID, region, S3 paths, SES emails, etc.

./fargate_deployment/scripts/run_all.sh

That’s it — after this completes:

    Processed CSV appears in S3 Output/

    Email notification sent via SES

🛠 Advanced (Manual Steps)

If you want finer control, you can run the deployment in steps:

    Set up IAM roles & policies (one-time):

./fargate_deployment/scripts/setup_iam.sh

Build & push Docker image:

./fargate_deployment/scripts/build_and_push.sh

Deploy to Fargate:

    ./fargate_deployment/scripts/deploy_to_fargate.sh

🔒 Security Notes

    No secrets are hardcoded — OpenAI API key is stored in AWS Secrets Manager.

    IAM roles follow the least privilege principle.

    .gitignore is configured to prevent committing sensitive files.