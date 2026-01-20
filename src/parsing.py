"""Response parsing utilities.

This module centralizes parsing helpers that were previously defined
inside `src.patient_risk_pipeline.py` so they can be reused and unit-tested.
"""
from __future__ import annotations

import re
import pandas as pd
import logging

logger = logging.getLogger("patient_pipeline")


def extract_risk_score(text):
    """
    Extract a numeric risk score from variants like:
      "Risk Score: 87"
    Returns a float in [0.0, 1.0] (value / 100) or None.
    """
    if not isinstance(text, str):
        return None
    m = re.search(r"\brisk\s*score\s*:\s*([0-9]+(?:\.[0-9]+)?)\b", text, flags=re.IGNORECASE)
    if not m:
        return None
    try:
        val = float(m.group(1))
        if 0.0 <= val <= 100.0:
            return val / 100.0
    except Exception:
        return None
    return None


def safe_split(line, label):
    try:
        lhs, rhs = line.split(":", 1)
        if re.sub(r"\s+", "", lhs).lower().startswith(re.sub(r"\s+", "", label).lower()):
            return rhs.strip()
    except Exception:
        pass
    return None


def parse_response_and_concerns(text):
    try:
        lines = [l.strip() for l in str(text).strip().splitlines() if l.strip() != ""]
        f1mo = f6mo = onc = card = None
        concerns_text = ""
        concerns_idx = None

        for i, l in enumerate(lines):
            if re.sub(r"\s+", "", l).lower().startswith("topmedicalconcerns"):
                concerns_idx = i
                break

        header_slice = lines[:concerns_idx] if concerns_idx is not None else lines[:4]

        for l in header_slice:
            v = safe_split(l, "Follow-up 1 month")
            if v is not None:
                f1mo = v
                continue
            v = safe_split(l, "Follow-up 6 months")
            if v is not None:
                f6mo = v
                continue
            v = safe_split(l, "Oncology recommended")
            if v is not None:
                onc = v
                continue
            v = safe_split(l, "Cardiology recommended")
            if v is not None:
                card = v
                continue

        if concerns_idx is not None:
            concerns_lines = lines[concerns_idx + 1:]
            concerns_text = "\n".join(cl.strip() for cl in concerns_lines)

        return pd.Series(
            [f1mo, f6mo, onc, card, concerns_text],
            index=["follow_up_1mo", "follow_up_6mo", "oncology_rec", "cardiology_rec", "top_concerns"],
        )
    except Exception as e:
        logger.warning(f"Failed to parse response (len={len(str(text)) if text is not None else 0}): {e}")
        return pd.Series([None] * 5, index=["follow_up_1mo", "follow_up_6mo", "oncology_rec", "cardiology_rec", "top_concerns"]) 


__all__ = ["extract_risk_score", "safe_split", "parse_response_and_concerns"]
