import os
import logging
from datetime import datetime, timezone
import json

# =========================
# Config knobs (env-override)
# =========================
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
GLOBAL_THROTTLE = float(os.getenv("OPENAI_THROTTLE_SEC", "0") or 0)
LLM_DISABLED = os.getenv("LLM_DISABLED", "false").lower() == "true"
CSV_CHUNK_ROWS = int(os.getenv("CSV_CHUNK_ROWS", "5000"))
OUTPUT_TMP = os.getenv("OUTPUT_TMP", "/tmp/output.csv")
USE_S3FS = os.getenv("USE_S3FS", "false").lower() == "true"
USE_LANGCHAIN = os.getenv("USE_LANGCHAIN", "false").lower() == "true"
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0") or 0)
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "800") or 800)
OPENAI_TIMEOUT_SEC = int(os.getenv("OPENAI_TIMEOUT_SEC", "60") or 60)

# =========
# Logging
# =========
def _configure_logging():
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    fmt_mode = os.getenv("LOG_FORMAT", "text").lower()
    _logger = logging.getLogger("patient_pipeline")
    _logger.setLevel(level)
    handler = logging.StreamHandler()

    if fmt_mode == "json":
        class JsonFormatter(logging.Formatter):
            def format(self, record):
                base = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "level": record.levelname,
                    "event": record.getMessage(),
                    "run_id": os.getenv("RUN_ID", "unknown"),
                    "task_id": os.getenv("TASK_ID", "unknown"),
                    "log_stream": os.getenv("LOG_STREAM", "unknown"),
                }
                if record.exc_info:
                    base["exc_info"] = self.formatException(record.exc_info)
                return json.dumps(base)
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    _logger.handlers = [handler]
    _logger.propagate = False
    return _logger

logger = _configure_logging()