#!/bin/bash
# 🚀 EC2 Launch Script for Patient Pipeline

set -euo pipefail
cd "$(dirname "$0")"

# --- CONFIG ---
KEY_NAME="may_25_kp"
KEY_PATH="$HOME/Desktop/patient-pipeline/ec2_deployment/${KEY_NAME}.pem"
INSTANCE_TYPE="t2.micro"
AMI_ID="ami-0c02fb55956c7d316"  # Amazon Linux 2 (us-east-1)
SECURITY_GROUP_ID="sg-09be1dde75a78c79a"
IAM_INSTANCE_PROFILE="LLM_EC2_InstanceProfile"
INSTANCE_NAME="patient-pipeline-instance"
USER_DATA_SCRIPT="ec2_user_data.sh"
PROJECT_DIR="$HOME/Desktop/patient-pipeline"

# --- Authorize your current IP for SSH (port 22) ---
USER_IP=$(curl -s -4 ifconfig.me)
echo "🔐 Authorizing SSH access from your IP: $USER_IP"
aws ec2 authorize-security-group-ingress \
  --group-id "$SECURITY_GROUP_ID" \
  --protocol tcp \
  --port 22 \
  --cidr "$USER_IP/32" || true

# --- Launch EC2 Instance ---
echo "🌩️ Launching EC2 instance..."
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id "$AMI_ID" \
  --instance-type "$INSTANCE_TYPE" \
  --key-name "$KEY_NAME" \
  --security-group-ids "$SECURITY_GROUP_ID" \
  --iam-instance-profile Name="$IAM_INSTANCE_PROFILE" \
  --user-data file://"$USER_DATA_SCRIPT" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$INSTANCE_NAME}]" \
  --query "Instances[0].InstanceId" \
  --output text)

echo "🆔 Instance ID: $INSTANCE_ID"
echo "⏳ Waiting for instance to be running..."

aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"

PUBLIC_IP=$(aws ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" \
  --query "Reservations[0].Instances[0].PublicIpAddress" \
  --output text)

echo "✅ EC2 Instance is ready!"
echo "🌐 Public IP: $PUBLIC_IP"
echo "🔑 To SSH in, run:"
echo "ssh -i \"$KEY_PATH\" ec2-user@$PUBLIC_IP"
