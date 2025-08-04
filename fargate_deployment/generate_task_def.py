#!/usr/bin/env python3
import os
import sys
import json
import argparse
from pathlib import Path

"""
Script: generate_task_def.py
Purpose: Load a template, inject image URI (ideally SHA-tagged), environment vars or secrets,
         and write an ECS-compatible task definition JSON for Fargate.
"""

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
TEMPLATE = BASE / "task-def-template.json"
OUTPUT = BASE / "final-task-def.json"
FALLBACK_IMAGE = os.getenv("FALLBACK_IMAGE_URI", "placeholder:latest")

# Read CLI args
parser = argparse.ArgumentParser(
    description="Generate ECS task definition with image, env vars, secrets."
)
parser.add_argument(
    "--image",
    help="ECR image URI (e.g. 012345...amazonaws.com/patient-pipeline:git-sha)",
    default=None
)
parser.add_argument(
    "--env-file",
    metavar="FILE",
    default=str(ROOT / "ec2_deployment" / "env.list"),
    help="Path to key=value env file (line‑separated)"
)
parser.add_argument(
    "--secret-prefix",
    default=None,
    help=(
        "If provided, any key in env_file starting with this prefix "
        "will be treated as an AWS SecretsManager or SSM param ARN"
    )
)
args = parser.parse_args()

IMAGE = args.image or os.getenv("IMAGE_URI") or FALLBACK_IMAGE

# Load template
with open(TEMPLATE, "r") as f:
    task_def = json.load(f)

# Inject roles if needed; use placeholders or env vars
# Optional override via ENV or CLI
TASK_EXEC_ROLE = os.getenv("TASK_EXECUTION_ROLE")
TASK_ROLE = os.getenv("TASK_ROLE")
if TASK_EXEC_ROLE:
    task_def["executionRoleArn"] = TASK_EXEC_ROLE
if TASK_ROLE:
    task_def["taskRoleArn"] = TASK_ROLE

# Required Fargate fields (validate after-load):
#   task_def["requiresCompatibilities"] == ["FARGATE"]
#   "networkMode" == "awsvpc"
#   has "cpu", "memory" on root-level task_def (string)
#   containerDefinitions inner may have resources as needed

# Inject image
task_def["containerDefinitions"][0]["image"] = IMAGE

# Load environment variables
env_list = []
secrets_list = []
with open(args.env_file, "r") as ef:
    for line in ef:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            sys.stderr.write(f"⚠️ Skipping invalid line: {line}\n")
            continue
        key, val = line.split("=", 1)
        if args.secret_prefix and key.startswith(args.secret_prefix):
            # treat val as full ARN for secret or SSM param
            secrets_list.append({"name": key, "valueFrom": val})
        else:
            env_list.append({"name": key, "value": val})

if env_list:
    task_def["containerDefinitions"][0]["environment"] = env_list

if secrets_list:
    # Fargate requires platform version >= 1.4.0 for JSON key support [6]
    task_def["containerDefinitions"][0]["secrets"] = secrets_list

# Write final version
with open(OUTPUT, "w") as out:
    json.dump(task_def, out, indent=2)

print(f"✅ Wrote final task def: {OUTPUT}")
print(f"🔍 Using image: {IMAGE}")
if env_list:
    print(f"🧪 Injecting environment vars: {[e['name'] for e in env_list]}")
if secrets_list:
    print(f"🔐 Injecting secrets: {[s['name'] for s in secrets_list]}")
