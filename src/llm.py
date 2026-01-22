import os
import json
import random
import time
from openai import OpenAI, APIError, APIConnectionError, APITimeoutError, RateLimitError
import boto3

from src.config import (
    OPENAI_MODEL,
    GLOBAL_THROTTLE,
    LLM_DISABLED,
    OPENAI_TEMPERATURE,
    OPENAI_MAX_TOKENS,
    OPENAI_TIMEOUT_SEC,
    logger,
    USE_LANGCHAIN,
)

def _extract_openai_key_from_secret_string(secret_string: str) -> str:
    if not secret_string:
        return secret_string
    s = secret_string.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return s
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            for k in [
                "OPENAI_API_KEY",
                "openai_api_key",
                "api_key",
                "OPENAI_KEY",
                "openaiKey",
                "key",
            ]:
                v = obj.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    except Exception:
        pass
    return s

def _get_openai_key_from_secrets(secret_name: str, region_name: str) -> str:
    client = boto3.client("secretsmanager", region_name=region_name)
    resp = client.get_secret_value(SecretId=secret_name)
    return _extract_openai_key_from_secret_string(resp.get("SecretString", "") or "")

def get_openai_key() -> str:
    key = os.getenv("OPENAI_API_KEY")
    if key and key.strip():
        return key.strip()
    secret_name = (
        os.getenv("OPENAI_API_KEY_SECRET_NAME")
        or os.getenv("OPENAI_SECRET_NAME")
        or "openai/api-key"
    )
    region = os.getenv("AWS_REGION", "us-east-1")
    try:
        return _get_openai_key_from_secrets(secret_name, region)
    except Exception as e:
        raise RuntimeError(
            f"❌ Failed to retrieve OpenAI API key from Secrets Manager "
            f"(secret='{secret_name}', region='{region}'): {e}"
        )

OPENAI_CLIENT: OpenAI | None = None
if not LLM_DISABLED:
    OPENAI_CLIENT = OpenAI(api_key=get_openai_key())

def get_chat_response(
    inquiry_note,
    model=OPENAI_MODEL,
    retries=8,
    base_delay=1.5,
    max_delay=20,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout_sec: int | None = None,
):
    if temperature is None:
        temperature = OPENAI_TEMPERATURE
    if max_tokens is None:
        max_tokens = OPENAI_MAX_TOKENS
    if timeout_sec is None:
        timeout_sec = OPENAI_TIMEOUT_SEC

    if LLM_DISABLED:
        if inquiry_note.strip().startswith("Please assume the role"):
            return {"message": {"content": "Risk Score: 72\nLikely follow-up needed."}}
        return {"message": {"content": (
            "Follow-up 1 month: Yes\n"
            "Follow-up 6 months: Yes\n"
            "Oncology recommended: No\n"
            "Cardiology recommended: Yes\n\n"
            "Top Medical Concerns:\n"
            "1. Hypertension\n2. A1c elevation\n3. Chest pain\n4. Medication adherence\n5. BMI"
        )}}

    if OPENAI_CLIENT is None:
        raise RuntimeError("OPENAI_CLIENT not initialized (check LLM_DISABLED and OPENAI_API_KEY injection).")

    last_err = None
    for attempt in range(retries):
        try:
            if GLOBAL_THROTTLE > 0:
                time.sleep(GLOBAL_THROTTLE)

            resp = OPENAI_CLIENT.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": str(inquiry_note)}],
                temperature=float(temperature),
                max_tokens=int(max_tokens),
                timeout=float(timeout_sec),
            )

            content = ""
            try:
                content = resp.choices[0].message.content or ""
            except Exception:
                content = ""

            return {"message": {"content": content}}

        except (RateLimitError, APITimeoutError, APIConnectionError, APIError) as e:
            last_err = e
            sleep_s = min(max_delay, base_delay * (2 ** attempt)) * (0.5 + random.random())
            logger.warning("OpenAI transient error on attempt %d: %s. Backing off %.1fs...", attempt + 1, e, sleep_s)
            time.sleep(sleep_s)

        except Exception as e:
            last_err = e
            msg = str(e).lower()
            transient = any(s in msg for s in [
                "rate limit", "server is overloaded", "overloaded", "503", "timeout",
                "temporarily unavailable", "connection", "bad gateway", "gateway timeout", "service unavailable"
            ])
            if not transient and attempt >= 1:
                logger.warning("Non-transient OpenAI error on attempt %d: %s", attempt + 1, e)
                break

            sleep_s = min(max_delay, base_delay * (2 ** attempt)) * (0.5 + random.random())
            logger.warning("Attempt %d failed: %s. Backing off %.1fs...", attempt + 1, e, sleep_s)
            time.sleep(sleep_s)

    logger.error("All retries failed for OpenAI API. Last error: %s", last_err)
    return {"message": {"content": ""}}

def _risk_rating_via_langchain(note_text: str) -> tuple[str, str | None]:
    if LLM_DISABLED:
        return "Risk Score: 72\nLikely follow-up needed.", "Likely follow-up needed."

    try:
        from src.llm_chain import assess_note_with_langchain  # type: ignore
    except Exception as e:
        logger.warning("USE_LANGCHAIN=true but src.llm_chain import failed (%s). Falling back to OpenAI path.", e)
        return get_chat_response("Please assume the role of a primary care physician. Based on the following patient summary text, provide a single risk rating between 1 and 100 for the patient's need for follow-up care within the next year, with 1 being nearly no risk and 100 being the greatest risk.\n\nRespond in the following format:\n\nRisk Score: <numeric_value>\n<Brief explanation or justification here (optional)>\n\nHere is the patient summary:\n\n" + str(note_text))["message"]["content"], None

    try:
        try:
            assessment = assess_note_with_langchain(
                note_text=str(note_text),
                rag_context="",
                model=OPENAI_MODEL,
                temperature=OPENAI_TEMPERATURE,
                max_tokens=OPENAI_MAX_TOKENS,
                timeout_sec=OPENAI_TIMEOUT_SEC,
            )
        except TypeError:
            assessment = assess_note_with_langchain(note_text=str(note_text), rag_context="")

        score_0_1 = float(assessment.risk_score)
        score_1_100 = max(1, min(100, int(round(score_0_1 * 100))))

        rationale = ""
        try:
            if getattr(assessment, "rationale_bullets", None):
                rationale = "\n".join(f"- {b}" for b in assessment.rationale_bullets if str(b).strip())
        except Exception:
            rationale = ""

        risk_text = f"Risk Score: {score_1_100}"
        if rationale:
            risk_text += f"\n{rationale}"

        return risk_text, (rationale if rationale else None)
    except Exception as e:
        logger.warning("LangChain assessment failed (%s). Falling back to OpenAI path.", e)
        return get_chat_response("Please assume the role of a primary care physician. Based on the following patient summary text, provide a single risk rating between 1 and 100 for the patient's need for follow-up care within the next year, with 1 being nearly no risk and 100 being the greatest risk.\n\nRespond in the following format:\n\nRisk Score: <numeric_value>\n<Brief explanation or justification here (optional)>\n\nHere is the patient summary:\n\n" + str(note_text))["message"]["content"], None