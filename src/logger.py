"""Centralized logging setup for patient-pipeline.

This scaffold mirrors the logging behavior from the original
`src.patient_risk_pipeline` so other modules can import a shared
`logger` without changing runtime behavior yet.
"""
from __future__ import annotations

import os
import json
import logging
from datetime import datetime, timezone


def _configure_logging():
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    fmt_mode = os.getenv("LOG_FORMAT", "text").lower()  # "text" | "json"
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

__all__ = ["logger", "_configure_logging"]
