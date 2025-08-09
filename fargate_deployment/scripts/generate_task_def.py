#!/usr/bin/env python3
import os
import sys
import json
import argparse
from pathlib import Path
from typing import List, Tuple

"""
Generate an ECS Fargate task definition from a template.

- Resolves paths relative to this script:
    templates/task-def-template.json  -> templates/final-task-def.json
- Injects image URI (prefer SHA tag).
- Optionally injects environment variables and/or secrets from a key=value file.
- Validates minimal Fargate fields and that exactly one container is present.
"""

SCRIPT_DIR = Path(__file__).resolve().parent
DEPLOY_DIR = SCRIPT_DIR.parent                        # fargate_deployment/
REPO_ROOT = DEPLOY_DIR.parent                         # repo root
TEMPLATES_DIR = DEPLOY_DIR / "templates"

TEMPLATE = TEMPLATES_DIR / "task-def-template.json"
OUTPUT = TEMPLATES_DIR / "final-task-def.json"

FALLBACK_IMAGE = os.getenv("FALLBACK_IMAGE_URI", "placeholder:latest")

def parse_kv_file(path: Path, secret_prefix: str | None) -> Tuple[List[dict], List[dict]]:
    """
    Parse key=value lines. Supports common variants like:
      - KEY=value
      - export KEY=value
      - KEY="quoted value"
    Lines beginning with # are comments. Blank lines ignored.

    If secret_prefix is provided, any KEY starting with it will be emitted as a
    Secrets Manager / SSM "secrets" entry with valueFrom=<val>.
    """
    env, secrets = [], []
    if not path.exists():
        return env, secrets

    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            sys.stderr.write(f"⚠️  Skipping invalid line (no '='): {raw}\n")
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if secret_prefix and key.startswith(secret_prefix):
            secrets.append({"name": key, "valueFrom": val})
        else:
            env.append({"name": key, "value": val})
    return env, secrets

def validate_fargate(td: dict) -> None:
    errs = []
    req = td.get("requiresCompatibilities", [])
    if "FARGATE" not in req:
        errs.append("requiresCompatibilities must include 'FARGATE'")
    if td.get("networkMode") != "awsvpc":
        errs.append("networkMode must be 'awsvpc'")
    if not td.get("cpu"):
        errs.append("cpu (string) must be set at task level")
    if not td.get("memory"):
        errs.append("memory (string) must be set at task level")
    cds = td.get("containerDefinitions", [])
    if not cds:
        errs.append("containerDefinitions must contain at least one container")
    if len(cds) != 1:
        errs.append("this script expects exactly one container in containerDefinitions")
    if errs:
        for e in errs:
            sys.stderr.write(f"❌ {e}\n")
        sys.exit(2)

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate ECS task definition with image, env vars, and secrets."
    )
    parser.add_argument("--image", help="ECR image URI (…amazonaws.com/repo:tag)", default=None)
    parser.add_argument(
        "--env-file",
        metavar="FILE",
        # default to repo-root config.env (your current pattern)
        default=str(REPO_ROOT / "config.env"),
        help="Path to key=value env file (line-separated). Default: ./config.env",
    )
    parser.add_argument(
        "--secret-prefix",
        default=None,
        help="If provided, keys starting with this prefix go to 'secrets' (valueFrom=ARN).",
    )
    args = parser.parse_args()

    image = args.image or os.getenv("IMAGE_URI") or FALLBACK_IMAGE

    if not TEMPLATE.exists():
        sys.stderr.write(f"❌ Template not found: {TEMPLATE}\n")
        sys.exit(1)

    with TEMPLATE.open("r") as f:
        task_def = json.load(f)

    # Validate base assumptions first
    validate_fargate(task_def)

    # Enforce exactly one container for simplicity
    container = task_def["containerDefinitions"][0]

    # Inject roles if provided via environment
    exec_role = os.getenv("TASK_EXECUTION_ROLE")
    task_role = os.getenv("TASK_ROLE")
    if exec_role:
        task_def["executionRoleArn"] = exec_role
    if task_role:
        task_def["taskRoleArn"] = task_role

    # Inject image
    container["image"] = image

    # Inject env/secrets from file
    env_file = Path(args.env_file)
    env_list, secrets_list = parse_kv_file(env_file, args.secret_prefix)

    if env_list:
        container["environment"] = env_list
    if secrets_list:
        container["secrets"] = secrets_list

    # Ensure output dir exists
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT.open("w") as out:
        json.dump(task_def, out, indent=2)

    print(f"✅ Wrote final task def: {OUTPUT}")
    print(f"🔍 Using image: {image}")
    if env_list:
        print(f"🧪 Injected environment vars: {[e['name'] for e in env_list]}")
    if secrets_list:
        print(f"🔐 Injected secrets: {[s['name'] for s in secrets_list]}")

if __name__ == "__main__":
    main()
