# FILE: src/llm_chain.py
from __future__ import annotations

import os
from typing import List, Optional

from pydantic import BaseModel, Field, confloat

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from langchain.output_parsers import OutputFixingParser


# -----------------------------
# 1) Structured output schema
# -----------------------------
class RiskAssessment(BaseModel):
    risk_score: confloat(ge=0, le=1) = Field(
        ...,
        description="Risk score in [0,1]. Keep in sync with pipeline (pipeline multiplies by 100).",
    )
    risk_level: str = Field(
        ...,
        description="One of: low, medium, high (or your preferred taxonomy).",
    )
    rationale_bullets: List[str] = Field(
        default_factory=list,
        description="Top 2-4 concise bullets justifying the risk score.",
    )
    follow_up_recommendations: List[str] = Field(
        default_factory=list,
        description="Actionable recommendations; may be empty for low risk.",
    )
    red_flags: List[str] = Field(
        default_factory=list,
        description="Specific concerning findings/symptoms/med adherence issues.",
    )
    # Optional “portfolio alignment” fields (safe to keep even if blank)
    supporting_note_quotes: List[str] = Field(
        default_factory=list,
        description="Short direct quotes (<= 20 words each) supporting assessment.",
    )
    kb_citations: List[str] = Field(
        default_factory=list,
        description="KB IDs/filenames used (no full KB text).",
    )


# -----------------------------
# 2) Model + parsing utilities
# -----------------------------
def _get_llm(
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout_sec: int | None = None,
) -> ChatOpenAI:
    """
    Configure the model from env vars with optional overrides.
    This keeps LangChain calls in sync with your direct OpenAI caller.
    """
    if model is None:
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if temperature is None:
        temperature = float(os.getenv("OPENAI_TEMPERATURE", "0") or 0)
    if max_tokens is None:
        max_tokens = int(os.getenv("OPENAI_MAX_TOKENS", "800") or 800)
    if timeout_sec is None:
        timeout_sec = int(os.getenv("OPENAI_TIMEOUT_SEC", "60") or 60)

    # ChatOpenAI reads OPENAI_API_KEY from env by default.
    return ChatOpenAI(
        model=str(model),
        temperature=float(temperature),
        max_tokens=int(max_tokens),
        timeout=int(timeout_sec),
    )


def _build_prompt(parser: PydanticOutputParser) -> ChatPromptTemplate:
    """
    Prompt that strongly encourages parser-compliant output.
    """
    system = (
        "You are a clinical risk triage assistant. "
        "You will be given a patient note and optionally retrieved clinical knowledge base context. "
        "Return ONLY data that conforms to the required schema.\n\n"
        "Safety/quality rules:\n"
        "- Use only the note + provided KB context; do not hallucinate.\n"
        "- Keep bullets concise and clinically grounded.\n"
        "- Quotes must be short and copied from the note.\n"
        "- KB citations should be IDs/filenames if provided.\n"
    )

    human = (
        "{format_instructions}\n\n"
        "RAG_CONTEXT (may be empty):\n"
        "{rag_context}\n\n"
        "PATIENT_NOTE:\n"
        "{note_text}\n"
    )

    return ChatPromptTemplate.from_messages(
        [
            ("system", system),
            ("human", human),
        ]
    ).partial(format_instructions=parser.get_format_instructions())


def assess_note_with_langchain(
    note_text: str,
    rag_context: str = "",
    *,
    kb_citations: Optional[List[str]] = None,
    # --- optional overrides to keep runtime knobs in sync with patient_risk_pipeline.py ---
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout_sec: int | None = None,
) -> RiskAssessment:
    """
    Main entrypoint: returns a validated RiskAssessment object.
    If parsing fails, tries automatic repair.

    IMPORTANT: model/temperature/max_tokens/timeout_sec are optional overrides so
    patient_risk_pipeline.py can force the same knobs as direct OpenAI calls.
    """
    llm = _get_llm(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
    )

    base_parser = PydanticOutputParser(pydantic_object=RiskAssessment)
    fixing_parser = OutputFixingParser.from_llm(parser=base_parser, llm=llm)

    prompt = _build_prompt(base_parser)
    chain = prompt | llm

    msg = chain.invoke(
        {
            "note_text": note_text,
            "rag_context": rag_context or "",
        }
    )

    # msg is an AIMessage; parser expects text
    text = getattr(msg, "content", str(msg))

    try:
        obj = base_parser.parse(text)
    except Exception:
        obj = fixing_parser.parse(text)

    # If the caller already knows citations (e.g., from TF-IDF retrieval),
    # inject them here without relying on the model.
    if kb_citations:
        seen = set()
        deduped: List[str] = []
        for c in kb_citations:
            c = (c or "").strip()
            if c and c not in seen:
                seen.add(c)
                deduped.append(c)
        obj.kb_citations = deduped

    return obj


# -----------------------------
# Optional: 2nd chain for email
# -----------------------------
class ClinicianEmail(BaseModel):
    subject: str = Field(..., description="Short subject line")
    body: str = Field(..., description="Email body in plain text with clear sections")


def draft_clinician_email_with_langchain(
    patient_summaries: str,
    *,
    clinic_context: str = "",
    # Optional overrides (same sync pattern)
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout_sec: int | None = None,
) -> ClinicianEmail:
    """
    Optional: Use an LLM to turn already-structured patient summaries into
    a polished email. Only use if you currently do an LLM email step.
    """
    llm = _get_llm(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
    )

    base_parser = PydanticOutputParser(pydantic_object=ClinicianEmail)
    fixing_parser = OutputFixingParser.from_llm(parser=base_parser, llm=llm)

    system = (
        "You are formatting a concise clinician alert email summarizing high-risk patients. "
        "Be brief, scannable, and avoid any invented details."
    )
    human = (
        "{format_instructions}\n\n"
        "CLINIC_CONTEXT (optional):\n"
        "{clinic_context}\n\n"
        "PATIENT_SUMMARIES (already extracted; do not add new facts):\n"
        "{patient_summaries}\n"
    )

    prompt = ChatPromptTemplate.from_messages([("system", system), ("human", human)]).partial(
        format_instructions=base_parser.get_format_instructions()
    )

    msg = (prompt | llm).invoke(
        {
            "clinic_context": clinic_context or "",
            "patient_summaries": patient_summaries,
        }
    )
    text = getattr(msg, "content", str(msg))

    try:
        return base_parser.parse(text)
    except Exception:
        return fixing_parser.parse(text)
