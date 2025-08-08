#!/usr/bin/env bash
set -euo pipefail

# =========================
# Role and Policy Names
# =========================
EXEC_ROLE_NAME="PatientPipelineECSExecutionRole"
TASK_ROLE_NAME="PatientPipelineECSTaskRole"
S3_POLICY_NAME="PatientPipelineS3Policy"
SECRETS_POLICY_NAME="PatientPipelineSecretsPolicy"

# Policy file paths
POLICY_DIR="fargate_deployment/policies"
TRUST_POLICY_FILE="$POLICY_DIR/ecs-trust-policy.json"
S3_POLICY_FILE="$POLICY_DIR/s3-access-policy.json"
SECRETS_POLICY_FILE="$POLICY_DIR/secrets-access-policy.json"

# =========================
# Create Execution Role
# =========================
echo "🔍 Checking for IAM execution role: $EXEC_ROLE_NAME ..."
if aws iam get-role --role-name "$EXEC_ROLE_NAME" >/dev/null 2>&1; then
  echo "✅ Execution role $EXEC_ROLE_NAME already exists."
else
  echo "🚀 Creating execution role: $EXEC_ROLE_NAME ..."
  aws iam create-role \
    --role-name "$EXEC_ROLE_NAME" \
    --assume-role-policy-document file://$TRUST_POLICY_FILE
  echo "✅ Execution role created."
fi

echo "🔍 Attaching AmazonECSTaskExecutionRolePolicy to execution role ..."
aws iam attach-role-policy \
  --role-name "$EXEC_ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

# =========================
# Create Task Role
# =========================
echo "🔍 Checking for IAM task role: $TASK_ROLE_NAME ..."
if aws iam get-role --role-name "$TASK_ROLE_NAME" >/dev/null 2>&1; then
  echo "✅ Task role $TASK_ROLE_NAME already exists."
else
  echo "🚀 Creating task role: $TASK_ROLE_NAME ..."
  aws iam create-role \
    --role-name "$TASK_ROLE_NAME" \
    --assume-role-policy-document file://$TRUST_POLICY_FILE
  echo "✅ Task role created."
fi

# =========================
# Attach Policies to Task Role
# =========================

# SES access
echo "🔍 Attaching AmazonSESFullAccess to task role ..."
aws iam attach-role-policy \
  --role-name "$TASK_ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/AmazonSESFullAccess

# S3 access
echo "🔍 Attaching custom S3 access policy ..."
aws iam put-role-policy \
  --role-name "$TASK_ROLE_NAME" \
  --policy-name "$S3_POLICY_NAME" \
  --policy-document file://$S3_POLICY_FILE

# Secrets Manager access
echo "🔍 Attaching Secrets Manager policy ..."
aws iam put-role-policy \
  --role-name "$TASK_ROLE_NAME" \
  --policy-name "$SECRETS_POLICY_NAME" \
  --policy-document file://$SECRETS_POLICY_FILE

# =========================
# Output ARNs
# =========================
EXEC_ROLE_ARN=$(aws iam get-role --role-name "$EXEC_ROLE_NAME" --query "Role.Arn" --output text)
TASK_ROLE_ARN=$(aws iam get-role --role-name "$TASK_ROLE_NAME" --query "Role.Arn" --output text)

echo "✅ IAM roles setup complete."
echo "📌 Execution Role ARN: $EXEC_ROLE_ARN"
echo "📌 Task Role ARN: $TASK_ROLE_ARN"
echo "💡 Paste these ARNs into config.env:"
echo "    TASK_EXECUTION_ROLE=$EXEC_ROLE_ARN"
echo "    TASK_ROLE=$TASK_ROLE_ARN"
