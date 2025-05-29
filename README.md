# 🏥 Patient Risk Pipeline

This project runs an NLP-powered analysis of patient notes to:
- Assign follow-up risk scores
- Generate clinical recommendations
- Email a summary of high-risk patients

Supports deployment on both **EC2** and **Fargate** using Docker and AWS services.

---

## 📁 Project Structure

patient-pipeline/
├── Dockerfile # Builds the container
├── patients_pipeline.py # Main script logic
├── requirements.txt # Python dependencies
├── env.list # Environment variables
├── ec2_deployment/ # EC2 launch scripts
│ ├── launch_ec2_pipeline_instance.sh
│ ├── setup_docker.sh
│ └── ssh_connect.sh (optional)
├── fargate_deployment/ # Fargate deployment config
│ ├── fargate-task.json
│ ├── deploy_fargate.sh
└── README.md

---

## 🚀 EC2 Deployment Guide

### 1. Launch EC2 Instance
From your local terminal:

```bash
cd ec2_deployment/
./launch_ec2_pipeline_instance.sh