#!/usr/bin/env python3
"""
Deploy (or reuse) a SageMaker real-time endpoint for a Hugging Face model.

CLI-driven workflow:
  1) Run this script to create the endpoint if it doesn't exist.
  2) Export SAGEMAKER_ENDPOINT_NAME + LLM_PROVIDER=sagemaker and run your pipeline (local/Fargate).

Notes:
- Uses SageMaker Hugging Face Inference Toolkit (HF_MODEL_ID + HF_TASK) zero-code deployment.
  See Hugging Face docs: deployment + request format uses {"inputs": "...", "parameters": {...}}.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

import boto3

# SageMaker SDK is required for deploy
try:
    import sagemaker
    from sagemaker.huggingface import HuggingFaceModel
except ImportError as e:
    raise ImportError(
        "Missing dependency: sagemaker. Install with: pip install sagemaker"
    ) from e


def _boto_client(service: str, region: str):
    return boto3.client(service, region_name=region)


def endpoint_exists(sm, endpoint_name: str) -> bool:
    try:
        sm.describe_endpoint(EndpointName=endpoint_name)
        return True
    except sm.exceptions.ClientError as e:
        msg = str(e)
        if "Could not find endpoint" in msg or "ValidationException" in msg:
            return False
        raise


def wait_in_service(sm, endpoint_name: str, timeout_sec: int = 1800, poll_sec: int = 15) -> None:
    start = time.time()
    while True:
        desc = sm.describe_endpoint(EndpointName=endpoint_name)
        status = desc.get("EndpointStatus")
        if status == "InService":
            return
        if status in ("Failed", "OutOfService"):
            reason = desc.get("FailureReason", "unknown")
            raise RuntimeError(f"Endpoint entered status={status}. reason={reason}")
        if time.time() - start > timeout_sec:
            raise TimeoutError(f"Timed out waiting for endpoint InService after {timeout_sec}s")
        print(f"[wait] status={status} ...")
        time.sleep(poll_sec)


def smoke_test_invoke(smrt, endpoint_name: str, prompt: str) -> dict:
    """
    Sends {"inputs": prompt} which matches HF Inference Toolkit request format.
    You can also pass generation args via {"parameters": {...}}. :contentReference[oaicite:1]{index=1}
    """
    body = {"inputs": prompt}
    resp = smrt.invoke_endpoint(
        EndpointName=endpoint_name,
        ContentType="application/json",
        Accept="application/json",
        Body=json.dumps(body).encode("utf-8"),
    )
    raw = resp["Body"].read()
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw.decode("utf-8", errors="replace")}


def main() -> int:
    p = argparse.ArgumentParser(description="Deploy or reuse a SageMaker HF LLM endpoint (CLI).")

    # Required-ish
    p.add_argument("--endpoint-name", required=True, help="SageMaker endpoint name to create/reuse.")
    p.add_argument("--region", default=os.getenv("AWS_REGION", "us-east-1"), help="AWS region.")
    p.add_argument("--role-arn", default=os.getenv("SAGEMAKER_ROLE_ARN", ""), help="IAM role ARN for SageMaker model.")

    # Model selection (smoke-test default)
    p.add_argument("--hf-model-id", default="google/flan-t5-base", help="HF model id from Hugging Face Hub.")
    p.add_argument(
        "--hf-task",
        default="text2text-generation",
        help="HF pipeline task for inference toolkit (e.g., text-generation, text2text-generation).",
    )

    # Container versions (keep stable; adjust later if needed)
    p.add_argument("--transformers-version", default="4.26", help="Transformers DLC version.")
    p.add_argument("--pytorch-version", default="1.13", help="PyTorch DLC version.")
    p.add_argument("--py-version", default="py39", help="Python version for DLC.")

    # Instance
    p.add_argument("--instance-type", default="ml.m5.xlarge", help="Instance type for real-time endpoint.")
    p.add_argument("--initial-instance-count", type=int, default=1, help="Number of instances.")

    # Behavior flags
    p.add_argument(
        "--update",
        action="store_true",
        help="If set, deploy/update endpoint even if it already exists (can take time).",
    )
    p.add_argument(
        "--smoke-test",
        action="store_true",
        help="If set, invoke endpoint after deploy/reuse to confirm response.",
    )

    args = p.parse_args()

    if not args.role_arn:
        # Works in SageMaker-managed environments where get_execution_role is available
        try:
            role_arn = sagemaker.get_execution_role()
        except Exception:
            raise ValueError(
                "No role ARN provided. Pass --role-arn or set env SAGEMAKER_ROLE_ARN."
            )
    else:
        role_arn = args.role_arn

    region = args.region
    endpoint_name = args.endpoint_name

    sm = _boto_client("sagemaker", region)
    smrt = _boto_client("sagemaker-runtime", region)

    exists = endpoint_exists(sm, endpoint_name)
    if exists and not args.update:
        print(f"[ok] Endpoint already exists (reusing): {endpoint_name}")
        wait_in_service(sm, endpoint_name)
    else:
        if exists and args.update:
            print(f"[update] Endpoint exists; redeploying to same name: {endpoint_name}")
        else:
            print(f"[deploy] Creating endpoint: {endpoint_name}")

        sess = sagemaker.Session(boto_session=boto3.Session(region_name=region))

        env = {
            "HF_MODEL_ID": args.hf_model_id,
            "HF_TASK": args.hf_task,
        }

        hf_model = HuggingFaceModel(
            env=env,
            role=role_arn,
            transformers_version=args.transformers_version,
            pytorch_version=args.pytorch_version,
            py_version=args.py_version,
            sagemaker_session=sess,
        )

        # deploy() will create model + endpoint config + endpoint
        hf_model.deploy(
            initial_instance_count=args.initial_instance_count,
            instance_type=args.instance_type,
            endpoint_name=endpoint_name,
        )

        wait_in_service(sm, endpoint_name)

    if args.smoke_test:
        prompt = "Return JSON with keys risk_score, concerns, recommendations for: patient has dizziness and elevated A1c."
        out = smoke_test_invoke(smrt, endpoint_name, prompt)
        print("[smoke-test] response:")
        print(json.dumps(out, indent=2)[:4000])

    print("\n[export]")
    print(f"export LLM_PROVIDER=sagemaker")
    print(f"export SAGEMAKER_ENDPOINT_NAME={endpoint_name}")
    print(f"export SAGEMAKER_REGION={region}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
