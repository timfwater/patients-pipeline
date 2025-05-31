import json
from pathlib import Path

# Always resolve relative to this script's location
BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent

template_path = BASE_DIR / "task-def-template.json"
output_path = BASE_DIR / "final-task-def.json"
env_file_path = ROOT_DIR / "ec2_deployment" / "env.list"

# Load template
with open(template_path) as f:
    task_def = json.load(f)

# Replace placeholders
task_def["executionRoleArn"] = "arn:aws:iam::665277163763:role/ecsTaskExecutionRole"
task_def["taskRoleArn"] = "arn:aws:iam::665277163763:role/ecsTaskRole"
task_def["containerDefinitions"][0]["image"] = "665277163763.dkr.ecr.us-east-1.amazonaws.com/patient-pipeline:latest"

# Inject env variables
env_vars = []
with open(env_file_path) as f:
    for line in f:
        if line.strip() and not line.startswith("#"):
            key, value = line.strip().split("=", 1)
            env_vars.append({"name": key, "value": value})

task_def["containerDefinitions"][0]["environment"] = env_vars

# Write final version
with open(output_path, "w") as f:
    json.dump(task_def, f, indent=2)

print(f"✅ Final task definition written to: {output_path}")
