#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/config.env"

# Normalize names (accept either legacy or new)
ECS_CLUSTER_NAME="${ECS_CLUSTER_NAME:-${CLUSTER_NAME:-patient-pipeline-cluster}}"
ECR_REPO_NAME="${ECR_REPO_NAME:-patient-pipeline}"
EXEC_ROLE_NAME="${EXECUTION_ROLE_NAME:-PatientPipelineECSExecutionRole}"
TASK_ROLE_NAME="${TASK_ROLE_NAME:-PatientPipelineECSTaskRole}"
TASK_FAMILY="${TASK_FAMILY:-patient-pipeline-task}"
LOG_GROUP="${LOG_GROUP:-/ecs/patient-pipeline}"

echo "🔨 Tearing down resources in ${AWS_REGION}..."

# 1) ECS cluster
aws ecs delete-cluster --cluster "$ECS_CLUSTER_NAME" --region "$AWS_REGION" >/dev/null 2>&1 || true
echo "✅ Requested ECS cluster delete: $ECS_CLUSTER_NAME"

# 2) ECR repo (force deletes images)
aws ecr delete-repository --repository-name "$ECR_REPO_NAME" --force --region "$AWS_REGION" >/dev/null 2>&1 || true
echo "✅ Deleted ECR repository (if existed): $ECR_REPO_NAME"

# 3) Deregister all task definitions in the family
TD_ARNS=$(aws ecs list-task-definitions --family-prefix "$TASK_FAMILY" --region "$AWS_REGION" --query 'taskDefinitionArns[]' --output text 2>/dev/null || true)
for arn in $TD_ARNS; do
  aws ecs deregister-task-definition --task-definition "$arn" --region "$AWS_REGION" >/dev/null 2>&1 || true
done
if [[ -n "${TD_ARNS:-}" ]]; then
  echo "✅ Deregistered task defs for family: $TASK_FAMILY"
else
  echo "ℹ️  No task defs to deregister for: $TASK_FAMILY"
fi

# 4) CloudWatch Logs (optional)
aws logs delete-log-group --log-group-name "$LOG_GROUP" --region "$AWS_REGION" >/dev/null 2>&1 || true
echo "✅ Deleted log group (if existed): $LOG_GROUP"

# 5) IAM roles: detach managed + inline policies, then delete roles
delete_role_safe() {
  local role="$1"

  # Detach managed policies
  local attached
  attached=$(aws iam list-attached-role-policies --role-name "$role" --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null || true)
  for arn in $attached; do
    aws iam detach-role-policy --role-name "$role" --policy-arn "$arn" >/dev/null 2>&1 || true
  done

  # Delete inline policies
  local inlines
  inlines=$(aws iam list-role-policies --role-name "$role" --query 'PolicyNames[]' --output text 2>/dev/null || true)
  for name in $inlines; do
    aws iam delete-role-policy --role-name "$role" --policy-name "$name" >/dev/null 2>&1 || true
  done

  # Delete role
  aws iam delete-role --role-name "$role" >/dev/null 2>&1 || true
  echo "✅ Deleted IAM role (if existed): $role"
}

delete_role_safe "$EXEC_ROLE_NAME"
delete_role_safe "$TASK_ROLE_NAME"

echo "🎉 Teardown complete."
