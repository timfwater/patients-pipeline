# FILE: src/llm_bedrock.py
"""
Bedrock (Claude) calling function, written as a drop-in alternative to the
existing OpenAI-based get_chat_response() in patient_risk_pipeline.py.

Design goals:
    - Same call signature and same return shape as get_chat_response(), i.e.
      returns {"message": {"content": "..."}}, so callers (query_combined_prompt,
      the risk-scoring .apply() call, etc.) don't need to change at all.
    - Uses Bedrock's Converse API rather than the raw Anthropic-specific
      invoke_model format. Converse normalizes request/response shape across
      model providers, so if you ever swap Claude for another Bedrock model,
      this function likely doesn't need to change.
    - No API key handling at all -- auth is via the ECS task's IAM role
      (needs bedrock:InvokeModel permission; see setup_iam.sh note below).
    - Same retry/backoff philosophy as the existing OpenAI function, since
      Bedrock can also throttle under load.
"""

import os
import time
import random
import logging

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger("patient_pipeline")

BEDROCK_REGION = os.getenv("BEDROCK_REGION", os.getenv("AWS_REGION", "us-east-1"))
BEDROCK_MODEL_ID = os.getenv(
    "BEDROCK_MODEL_ID",
    "anthropic.claude-3-5-sonnet-20240620-v1:0",
)

_bedrock_client = None


def _get_client():
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
    return _bedrock_client


def get_claude_response(inquiry_note, model=BEDROCK_MODEL_ID, retries=8, base_delay=1.5, max_delay=20):
    """
    Calls Claude on Bedrock via the Converse API, with exponential backoff
    and jitter, mirroring the existing get_chat_response() for OpenAI.

    Returns: {"message": {"content": "<response text>"}}
    """
    client = _get_client()
    last_err = None

    for attempt in range(retries):
        try:
            response = client.converse(
                modelId=model,
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": inquiry_note}],
                    }
                ],
                inferenceConfig={
                    "maxTokens": int(os.getenv("OPENAI_MAX_TOKENS", "800")),
                    "temperature": float(os.getenv("OPENAI_TEMPERATURE", "0")),
                },
            )
            content = response["output"]["message"]["content"][0]["text"]
            return {"message": {"content": content}}

        except ClientError as e:
            last_err = e
            error_code = e.response.get("Error", {}).get("Code", "")
            transient = error_code in (
                "ThrottlingException",
                "ServiceUnavailableException",
                "ModelTimeoutException",
                "InternalServerException",
            )
            if not transient and attempt >= 1:
                logger.warning(f"Non-transient Bedrock error on attempt {attempt+1}: {e}")
                break
            sleep_s = min(max_delay, base_delay * (2 ** attempt)) * (0.5 + random.random())
            logger.warning(f"Bedrock attempt {attempt+1} failed: {e}. Backing off {sleep_s:.1f}s...")
            time.sleep(sleep_s)

        except Exception as e:
            last_err = e
            logger.warning(f"Unexpected error calling Bedrock on attempt {attempt+1}: {e}")
            sleep_s = min(max_delay, base_delay * (2 ** attempt)) * (0.5 + random.random())
            time.sleep(sleep_s)

    logger.error(f"All retries failed for Bedrock API. Last error: {last_err}")
    return {"message": {"content": ""}}
