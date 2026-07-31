# FILE: src/deidentify.py
"""
Lightweight de-identification / re-identification layer for clinical notes.

Purpose:
    Before any note text is sent to an external LLM API (OpenAI, etc.), strip
    out identifying information (names, dates, phone numbers, MRNs, etc.) and
    replace each with a placeholder token. After the LLM responds, swap the
    placeholders back to their original values so the final output/audit log
    still contains real data.

Design notes:
    - The mapping between placeholder and real value is generated fresh for
      each note and is never persisted to disk or logged. It only exists in
      memory for the duration of a single note's processing.
    - This uses Microsoft's Presidio library (analyzer + anonymizer), which
      is open source and does NOT call any external API — everything runs
      locally in the container, so no PII leaves your infrastructure during
      the scrubbing step itself.
    - Entity types included below cover the common HIPAA identifiers most
      likely to appear in a free-text clinical note. Adjust ENTITIES_TO_SCRUB
      if you want to be more/less aggressive.
"""

from __future__ import annotations
import re
import logging
from typing import Tuple, Dict

from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

logger = logging.getLogger("patient_pipeline")

# Presidio's internal loggers are very chatty (list every recognizer it
# loads, etc.) — quiet them down so CloudWatch logs stay readable.
logging.getLogger("presidio-analyzer").setLevel(logging.WARNING)
logging.getLogger("tldextract").setLevel(logging.ERROR)

# Entities Presidio's default recognizers can catch that are relevant to
# clinical notes. PERSON/DATE_TIME/PHONE_NUMBER/EMAIL are the highest-value
# ones for this use case; the others are cheap to include and rarely hurt.
ENTITIES_TO_SCRUB = [
    "PERSON",
    "DATE_TIME",
    "PHONE_NUMBER",
    "EMAIL_ADDRESS",
    "LOCATION",
    "US_SSN",
    "MEDICAL_LICENSE",
]

# Simple extra pattern for MRNs / patient IDs, since these are often
# institution-specific formats Presidio's default recognizers won't catch
# out of the box (e.g. "MRN: 00482913" or "Patient ID 84921").
_MRN_PATTERN = re.compile(r"\b(?:MRN|Patient\s*ID)\s*[:#]?\s*(\d{4,10})\b", re.IGNORECASE)

_analyzer = None
_anonymizer = None


def _get_engines():
    """Lazily initialize Presidio engines (they're somewhat expensive to
    construct, so we build them once per process, not once per note)."""
    global _analyzer, _anonymizer
    if _analyzer is None:
        _analyzer = AnalyzerEngine()
    if _anonymizer is None:
        _anonymizer = AnonymizerEngine()
    return _analyzer, _anonymizer


def deidentify(note: str) -> Tuple[str, Dict[str, str]]:
    """
    Scrub identifying info from a note before sending it to an external LLM.

    Returns:
        scrubbed_text: the note with identifiers replaced by placeholders
                        like [PERSON_1], [DATE_TIME_1], [MRN_1]
        mapping: dict of placeholder -> original value, used to reverse
                 the process after the LLM responds. Keep this in memory
                 only; do not persist or log it.
    """
    if not isinstance(note, str) or not note.strip():
        return note, {}

    analyzer, anonymizer = _get_engines()

    # Handle MRN/patient ID pattern manually first (Presidio has no default
    # recognizer for arbitrary institutional ID formats).
    mapping: Dict[str, str] = {}
    mrn_counter = 0

    def _mrn_sub(match: re.Match) -> str:
        nonlocal mrn_counter
        mrn_counter += 1
        placeholder = f"[MRN_{mrn_counter}]"
        mapping[placeholder] = match.group(0)
        return placeholder

    note_pre = _MRN_PATTERN.sub(_mrn_sub, note)

    # Run Presidio's analyzer to find standard PII entities.
    results = analyzer.analyze(text=note_pre, entities=ENTITIES_TO_SCRUB, language="en")

    # Build per-entity-type counters so placeholders read like [PERSON_1],
    # [PERSON_2], [DATE_TIME_1], etc. rather than just generic tokens.
    counters: Dict[str, int] = {}
    operators = {}
    # We need deterministic, distinguishable placeholders per match, so we
    # anonymize manually rather than using Presidio's default "replace" op,
    # which would collapse all entities of a type into one static string.
    scrubbed = note_pre
    # Process matches in reverse order by start index so string replacement
    # doesn't shift the offsets of earlier matches.
    for result in sorted(results, key=lambda r: r.start, reverse=True):
        entity_type = result.entity_type
        original_value = note_pre[result.start:result.end]
        counters[entity_type] = counters.get(entity_type, 0) + 1
        placeholder = f"[{entity_type}_{counters[entity_type]}]"
        mapping[placeholder] = original_value
        scrubbed = scrubbed[:result.start] + placeholder + scrubbed[result.end:]

    return scrubbed, mapping


def reidentify(text: str, mapping: Dict[str, str]) -> str:
    """
    Reverse de-identification on LLM output text, in case the model echoes
    back any placeholder tokens (it sometimes does, e.g. in a summary).
    """
    if not isinstance(text, str) or not mapping:
        return text
    restored = text
    for placeholder, original_value in mapping.items():
        restored = restored.replace(placeholder, original_value)
    return restored


if __name__ == "__main__":
    # Quick manual smoke test - run: python3 deidentify.py
    logging.basicConfig(level=logging.INFO)
    sample_note = (
        "Patient John Smith (MRN: 00482913) was seen on 03/14/2026 by Dr. Alvarez. "
        "Contact number 555-234-9981. Patient reports chest pain and shortness of breath. "
        "Recommend follow-up with cardiology within 1 month. "
        "Email on file: john.smith84@example.com."
    )
    scrubbed, mapping = deidentify(sample_note)
    print("ORIGINAL:\n", sample_note)
    print("\nSCRUBBED:\n", scrubbed)
    print("\nMAPPING:\n", mapping)

    fake_llm_response = "Risk Score: 78\nPatient John Smith should follow up with cardiology."
    restored = reidentify(fake_llm_response, mapping)
    print("\nRESTORED RESPONSE:\n", restored)
