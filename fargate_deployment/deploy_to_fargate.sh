#!/bin/bash

set -euo pipefail
cd "$(dirname "$0")"  # move into fargate_deployment/

REGION="us-east-1"
CLUSTER_NAME="patient-pipeline-cluster"
SUBNET_ID="subnet-9b61e5ba"  # Replace with your actual subnet
SECURITY_GROUP_ID="sg-09be1dde75a78c79a"  # Replace with your actual SG

# Step 1: Build and push Docker image
echo "🐳 Building and pushing Docker image..."
cd ..
docker build -t patient-pipeline .
docker tag patient-pipeline:latest 665277163763.dkr.ecr.us-east-1.amazonaws.com/patient-pipeline:latest
docker push 665277163763.dkr.ecr.us-east-1.amazonaws.com/patient-pipeline:latest
cd fargate_deployment/

# Step 2: Generate final task definition
echo "🛠️ Generating task definition..."
python generate_task_def.py

# Step 3: Register task definition
echo "📄 Registering task definition..."
TASK_DEF_ARN=$(aws ecs register-task-definition \
  --cli-input-json file://final-task-def.json \
  --region $REGION \
  --query 'taskDefinition.taskDefinitionArn' \
  --output text)

REVISION=$(echo "$TASK_DEF_ARN" | awk -F':' '{print $NF}')
echo "✅ Registered revision: $REVISION"

# Step 4: Launch Fargate task
echo "🚀 Launching task..."
aws ecs run-task \
  --cluster $CLUSTER_NAME \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET_ID],securityGroups=[$SECURITY_GROUP_ID],assignPublicIp=ENABLED}" \
  --task-definition "patient-pipeline-task:$REVISION" \
  --region $REGION

echo "🎉 Task launched successfully!"
