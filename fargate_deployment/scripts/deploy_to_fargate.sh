#!/usr/bin/env bash
# 🧪 deploy_to_fargate.sh — version w/ dry-run & smoke-test logic

set -euo pipefail

# ── Handle optional dry-run mode ───────────────────────────────────────────────

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
  echo "⚠️ DRY RUN mode: commands will be echoed, not executed"
  set -x  # show expanded commands
fi

# ── Configuration (override via deploy.env or CI env) ──────────────────────────

REGION="${AWS_REGION:-us‑east‑1}"
CLUSTER_NAME="${ECS_CLUSTER_NAME:-patient‑pipeline‑cluster}"
# Allow comma-separated list of subnets & SGs for HA deployment
SUBNET_IDS="${FARGATE_SUBNET_IDS:-subnet‑9b61e5ba}"
SECURITY_GROUP_IDS="${FARGATE_SECURITY_GROUP_IDS:-sg‑09be1dde75a78c79a}"
ECR_REPO="${ECR_REPO_URI:-665277163763.dkr.ecr.us‑east‑1.amazonaws.com/patient‑pipeline}"
TASK_FAMILY="${TASK_FAMILY:-patient‑pipeline‑task}"

cd "$(dirname "$0")"  # ensure working from fargate_deployment/

# ── Step 1: Build & push image tags (latest + git SHA) ─────────────────────────

(
  cd ..
  echo "🐳 Building Docker image..."
  $DRY_RUN || docker build -t patient‑pipeline .

  # Determine version tag
  GIT_SHA=$(git rev‑parse --short=7 HEAD 2>/dev/null || echo "latest")
  IMAGE_SHA="$ECR_REPO:$GIT_SHA"
  IMAGE_LATEST="$ECR_REPO:latest"

  echo "🔖 Tagging image as: $GIT_SHA & latest"
  $DRY_RUN || {
    docker tag patient‑pipeline "$IMAGE_SHA"
    docker tag patient‑pipeline "$IMAGE_LATEST"
  }

  echo "🔐 Logging in to ECR"
  $DRY_RUN || aws ecr get-login-password --region "$REGION" \
     | docker login --username AWS --password‑stdin "$(echo $ECR_REPO | cut -d/ -f1)"

  echo "📥 Pushing images to ECR"
  $DRY_RUN || {
    docker push "$IMAGE_SHA"
    docker push "$IMAGE_LATEST"
  }
)

# ── Step 2: Write task definition using SHA tag ─────────────────────────────────

echo "🛠️ Generating task definition"
$DRY_RUN || IMAGE_URI="$IMAGE_SHA" python generate_task_def.py --image "$IMAGE_SHA"

# ── Step 3: Register task definition in ECS ────────────────────────────────────

echo "📄 Registering task definition"
TASK_DEF_ARN=$($DRY_RUN || aws ecs register-task-definition \
  --cli-input-json file://final-task-def.json \
  --region "$REGION" \
  --query 'taskDefinition.taskDefinitionArn' \
  --output text)

if [[ -n "${TASK_DEF_ARN:-}" ]]; then
  REVISION="${TASK_DEF_ARN##*:}"
  echo "✅ Registered task definition revision: $REVISION"
else
  echo "⚠️ Failed to register task definition; skipping run"
  exit 1
fi

# ── Step 4: Launch task with VPC config ─────────────────────────────────────────

echo "🚀 Launching Fargate task"
aws_args=$(printf "subnets=%s,securityGroups=%s,assignPublicIp=DISABLED" "$SUBNET_IDS" "$SECURITY_GROUP_IDS")
RUN_OUTPUT=$($DRY_RUN || aws ecs run-task \
  --cluster "$CLUSTER_NAME" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={$aws_args}" \
  --task-definition "$TASK_FAMILY:$REVISION" \
  --region "$REGION" \
  --query 'tasks[0].taskArn' --output text)

if [[ -z "${RUN_OUTPUT:-}" ]]; then
  echo "❌ Failed to launch ECS task"
  exit 1
fi

TASK_ARN=$(basename "$RUN_OUTPUT")
echo "✅ Task launched: $TASK_ARN (revision $REVISION, image $IMAGE_SHA)"

# ── Step 5: Wait for task to stop; fail fast on non-zero exit ─────────────────

if ! $DRY_RUN; then
  echo "⏳ Waiting for task $TASK_ARN to stop ..."
  aws ecs wait tasks-stopped --cluster "$CLUSTER_NAME" --tasks "$TASK_ARN" --region "$REGION"
fi

EXIT_CODE=0
if ! $DRY_RUN; then
  EXIT_CODE=$(aws ecs describe-tasks \
    --cluster "$CLUSTER_NAME" \
    --tasks "$TASK_ARN" \
    --query "tasks[0].containers[0].exitCode" --output text \
  )
  echo "⏹ Container exit code: $EXIT_CODE"
fi

if [[ "$EXIT_CODE" != "0" ]]; then
  echo "❌ Task $TASK_ARN failed (exit code $EXIT_CODE)"
  printf "🌐 View logs: AWS Console → CloudWatch Logs group = %s\n" "${LOG_GROUP:-/aws/ecs/$TASK_FAMILY}"
  exit "$EXIT_CODE"
else
  echo "🎉 Task completed successfully!"
fi

exit 0
