#!/bin/bash

# -------- SETTINGS --------
KEY_NAME="may_25_kp"
KEY_PATH="$HOME/Desktop/patient-pipeline/may_25_kp.pem"
INSTANCE_TYPE="t2.micro"
AMI_ID="ami-0c02fb55956c7d316"  # Amazon Linux 2 in us-east-1
SECURITY_GROUP_ID="sg-09be1dde75a78c79a"
IAM_INSTANCE_PROFILE_NAME="LLM_EC2_InstanceProfile"
INSTANCE_NAME="patient-pipeline-instance"
PROJECT_DIR="$HOME/Desktop/patient-pipeline/ec2_deployment"
DOCKER_IMAGE_NAME="patient-pipeline"
ENV_FILE="env.list"

# -------- GET YOUR PUBLIC IP --------
USER_IP=$(curl -s -4 ifconfig.me)
echo "Your public IP is: $USER_IP"

# -------- LAUNCH EC2 INSTANCE --------
echo "Launching EC2 instance..."
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id $AMI_ID \
  --count 1 \
  --instance-type $INSTANCE_TYPE \
  --key-name $KEY_NAME \
  --security-group-ids $SECURITY_GROUP_ID \
  --iam-instance-profile Name=$IAM_INSTANCE_PROFILE_NAME \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$INSTANCE_NAME}]" \
  --query 'Instances[0].InstanceId' \
  --output text)

echo "Instance launched: $INSTANCE_ID"
echo "Waiting for instance to be running..."
aws ec2 wait instance-running --instance-ids $INSTANCE_ID

PUBLIC_IP=$(aws ec2 describe-instances \
  --instance-ids $INSTANCE_ID \
  --query "Reservations[0].Instances[0].PublicIpAddress" \
  --output text)

echo "Instance is ready!"
echo "Public IP: $PUBLIC_IP"
echo "Waiting for instance to finish booting..."
sleep 20

# -------- UPLOAD PROJECT FILES --------
echo "Uploading code to EC2..."
scp -i $KEY_PATH -o StrictHostKeyChecking=no -r "$PROJECT_DIR" ec2-user@$PUBLIC_IP:/home/ec2-user/
scp -i $KEY_PATH -o StrictHostKeyChecking=no "$PROJECT_DIR/$ENV_FILE" ec2-user@$PUBLIC_IP:/home/ec2-user/

# -------- PROMPT FOR OVERRIDES --------
read -p "Enter THRESHOLD override (default 0.8): " THRESHOLD_OVERRIDE
read -p "Enter START_DATE override (YYYY-MM-DD, default 2024-05-01): " START_DATE_OVERRIDE
read -p "Enter END_DATE override (YYYY-MM-DD, default 2024-05-01): " END_DATE_OVERRIDE
read -p "Enter PHYSICIAN_ID_LIST (e.g. 101 or 101,102,103 — leave blank for all): " PHYSICIAN_ID_OVERRIDE

# Apply defaults if user input is empty
THRESHOLD_OVERRIDE=${THRESHOLD_OVERRIDE:-0.8}
START_DATE_OVERRIDE=${START_DATE_OVERRIDE:-2024-05-01}
END_DATE_OVERRIDE=${END_DATE_OVERRIDE:-2024-05-01}
PHYSICIAN_ID_OVERRIDE=${PHYSICIAN_ID_OVERRIDE:-""}

# -------- SSH AND RUN DOCKER --------
echo "Connecting and running Docker container..."
ssh -i $KEY_PATH -o StrictHostKeyChecking=no ec2-user@$PUBLIC_IP <<EOF
  sudo yum update -y
  sudo yum install docker -y
  sudo systemctl start docker
  cd /home/ec2-user/$(basename "$PROJECT_DIR")
  sudo docker build -t $DOCKER_IMAGE_NAME .
	sudo docker run \
  	--env-file /home/ec2-user/$ENV_FILE \
  	--env THRESHOLD=$THRESHOLD_OVERRIDE \
  	--env START_DATE=$START_DATE_OVERRIDE \
  	--env END_DATE=$END_DATE_OVERRIDE \
  	--env PHYSICIAN_ID_LIST="$PHYSICIAN_ID_OVERRIDE" \
  	$DOCKER_IMAGE_NAME
EOF

echo "✅ Done. Check your email and S3 output bucket."