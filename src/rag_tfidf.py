# FILE: src/rag_tfidf.py
import os
import io
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

import pandas as pd

# scikit-learn
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

logger = logging.getLogger(__name__)

@dataclass
class RagIndex:
    kb_df: pd.DataFrame
    vectorizer: TfidfVectorizer
    kb_matrix: object  # scipy sparse matrix


def _read_csv_any(path: str) -> pd.DataFrame:
    """
    Supports:
      - local paths
      - s3://bucket/key (if boto3 available + creds configured)
    """
    if path.startswith("s3://"):
        import boto3
        s3 = boto3.client("s3")
        bucket, key = path.replace("s3://", "", 1).split("/", 1)
        obj = s3.get_object(Bucket=bucket, Key=key)
        body = obj["Body"].read()
        return pd.read_csv(io.BytesIO(body))
    return pd.read_csv(path)


def build_tfidf_index(kb_df: pd.DataFrame) -> RagIndex:
    # Expect columns like your SageMaker notebook: title, text
    if "title" not in kb_df.columns or "text" not in kb_df.columns:
        raise ValueError("KB must contain columns: 'title' and 'text'")

    corpus = (kb_df["title"].fillna("") + "\n" + kb_df["text"].fillna("")).tolist()
    vectorizer = TfidfVectorizer(
        stop_words="english",
        ngram_range=(1, 2),
        max_features=50000,
    )
    kb_matrix = vectorizer.fit_transform(corpus)
    return RagIndex(kb_df=kb_df, vectorizer=vectorizer, kb_matrix=kb_matrix)


def retrieve_kb(note: str, idx: RagIndex, top_k: int = 4) -> pd.DataFrame:
    q = idx.vectorizer.transform([str(note)])
    sims = cosine_similarity(q, idx.kb_matrix).ravel()
    top_idx = sims.argsort()[::-1][:top_k]
    out = idx.kb_df.iloc[top_idx].copy()
    out["similarity"] = sims[top_idx]
    return out


def format_rag_context(snips: pd.DataFrame, max_chars: int = 2500) -> str:
    if snips is None or snips.empty:
        return ""

    chunks = []
    for _, r in snips.iterrows():
        title = str(r.get("title", "")).strip()
        text = str(r.get("text", "")).strip()
        sim = r.get("similarity", None)
        sim_txt = f"{float(sim):.3f}" if sim is not None else ""
        block = f"- [{sim_txt}] {title}\n{text}".strip()
        chunks.append(block)

    joined = "\n\n".join(chunks).strip()
    if len(joined) > max_chars:
        joined = joined[:max_chars].rstrip() + "…"
    return "RETRIEVED CONTEXT (may be relevant):\n" + joined


def build_index_from_env() -> Optional[RagIndex]:
    enabled = os.getenv("RAG_ENABLED", "false").lower() == "true"
    if not enabled:
        return None

    kb_path = os.getenv("RAG_KB_PATH", "").strip()
    if not kb_path:
        raise ValueError("RAG_ENABLED=true but RAG_KB_PATH is empty")

    logger.info(f"📚 RAG: loading KB from {kb_path}")
    kb_df = _read_csv_any(kb_path)

    logger.info(f"🧠 RAG: building TF-IDF index over {len(kb_df)} KB rows")
    return build_tfidf_index(kb_df)
