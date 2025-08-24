# FILE: tests/test_parsing.py
from src.patient_risk_pipeline import parse_response_and_concerns

def test_parsing_happy_path():
    text = """Follow-up 1 month: Yes
Follow-up 6 months: No
Oncology recommended: No
Cardiology recommended: Yes

Top Medical Concerns:
1. Chest pain
2. Hypertension
3. Diabetes
"""
    s = parse_response_and_concerns(text)
    assert s["follow_up_1mo"] == "Yes"
    assert s["follow_up_6mo"] == "No"
    assert s["oncology_rec"] == "No"
    assert s["cardiology_rec"] == "Yes"
    assert "Chest pain" in s["top_concerns"]
    # top_concerns should be a single string (comma or newline separated)
    assert isinstance(s["top_concerns"], str) and len(s["top_concerns"]) > 0

def test_parsing_is_robust_to_spacing_casing_and_colons():
    text = """ follow-up 1 Month  : yes
Follow-up   6 months:    YES
Oncology Recommended:   no
cardiology   recommended:  No

TOP  medical   concerns:
1. Atrial fibrillation
2. COPD
"""
    s = parse_response_and_concerns(text)
    # Case and spacing differences should still parse
    assert s["follow_up_1mo"].lower() == "yes"
    assert s["follow_up_6mo"].lower() == "yes"
    assert s["oncology_rec"].lower() == "no"
    assert s["cardiology_rec"].lower() == "no"
    assert "Atrial fibrillation" in s["top_concerns"]

def test_parsing_handles_missing_sections_gracefully():
    text = """Follow-up 1 month: No

Top Medical Concerns:
1. Syncope
"""
    s = parse_response_and_concerns(text)
    # present fields parsed
    assert s["follow_up_1mo"].lower() == "no"
    # missing fields should be present with safe defaults (empty string or "Unknown")
    # Adjust these expectations to match your actual function’s default behavior.
    assert "follow_up_6mo" in s
    assert "oncology_rec" in s
    assert "cardiology_rec" in s
    assert "top_concerns" in s and "Syncope" in s["top_concerns"]
