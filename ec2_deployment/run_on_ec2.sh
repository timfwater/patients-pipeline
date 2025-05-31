#!/bin/bash
# 👨‍💻 Manual runner for EC2
# Run this *after* the instance is up

KEY_PATH="$HOME/Desktop/patient-pipeline/may_25_kp.pem"
INSTANCE_IP="REPLACE_ME"  # Use `ec2 describe-instances` or your console to find this
REPO_DIR="patient-pipeline"

ssh -i "$KEY_PATH" ec2-user@"$INSTANCE_IP" << EOF
  cd $REPO_DIR
  git pull
  docker build -t patient-pipeline .
  docker run --env-file ec2_deployment/env.list patient-pipeline
EOF
