# FILE: src/rag_tfidf.py
"""
RAG backends:
  - TF-IDF (sklearn)
  - Embeddings (HF Transformers mean-pooling)

This module is intentionally "drop-in" for your pipeline:
  - build_index_from_env() returns None or an index object with .retrieve()
  - retrieve_kb(query, index, top_k)
  - format_rag_context(df, max_chars, ...)

Key additions vs earlier versions:
  ✅ Explicit "proof" logs for RAG_MODE + embed config + embedding matrix shape
  ✅ Defensive logging + better error clarity
  ✅ Device resolution printed (auto/cuda/cpu)
"""

import os
import logging
from io import StringIO
from typing import Optional, Tuple, List

import numpy as np
import pandas as pd

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


# Match your pipeline logger name for consistent CloudWatch formatting
logger = logging.getLogger("patient_pipeline")


# =========================
# Helpers
# =========================

def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "t", "yes", "y", "on")


def _parse_s3_uri(uri: str) -> Tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"Expected s3://... URI, got: {uri}")
    bucket_key = uri.replace("s3://", "", 1)
    bucket, key = bucket_key.split("/", 1)
    return bucket, key


def _load_kb_csv(path: str) -> pd.DataFrame:
    """
    Load KB CSV from local path or s3://bucket/key.

    Notes:
      - Reads whole KB into memory (fine for typical KB sizes).
      - Uses AWS_REGION if set.
    """
    if path.startswith("s3://"):
        import boto3

        region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
        s3 = boto3.client("s3", region_name=region)
        bucket, key = _parse_s3_uri(path)
        obj = s3.get_object(Bucket=bucket, Key=key)
        raw = obj["Body"].read().decode("utf-8", errors="replace")
        return pd.read_csv(StringIO(raw))

    return pd.read_csv(path)


def _safe_text_series(s: pd.Series) -> pd.Series:
    # Ensure everything is a clean string; avoid NaNs
    return s.fillna("").astype(str)


# =========================
# Index objects
# =========================

class TfidfIndex:
    backend = "tfidf"

    def __init__(self, df: pd.DataFrame, title_col: str, text_col: str):
        self.df = df.reset_index(drop=True)
        self.title_col = title_col
        self.text_col = text_col

        texts = _safe_text_series(self.df[text_col]).tolist()
        self.vectorizer = TfidfVectorizer(stop_words="english")
        self.matrix = self.vectorizer.fit_transform(texts)

        self.kb_size = len(self.df)
        self.model_name = "sklearn_tfidf"

        # Proof log
        logger.info(
            "🧠 TFIDF_INDEX_READY kb_rows=%d matrix_shape=%s",
            self.kb_size,
            getattr(self.matrix, "shape", None),
        )

    def retrieve(self, query: str, top_k: int) -> pd.DataFrame:
        q = str(query)
        q_vec = self.vectorizer.transform([q])
        sims = cosine_similarity(q_vec, self.matrix)[0]  # shape: (kb_size,)
        idx = np.argsort(sims)[::-1][:top_k]

        out = self.df.iloc[idx].copy()
        out["similarity"] = sims[idx]
        out["kb_id"] = idx
        return out


class EmbeddingIndex:
    """
    Embedding backend using HuggingFace Transformers with simple mean pooling.

    Env knobs:
      RAG_EMBED_MODEL_ID
      RAG_EMBED_DEVICE = auto|cpu|cuda
      RAG_EMBED_MAX_LENGTH
      RAG_EMBED_BATCH_SIZE
      RAG_EMBED_NORMALIZE = true|false
    """
    backend = "embeddings"

    def __init__(self, df: pd.DataFrame, title_col: str, text_col: str):
        import torch
        from transformers import AutoTokenizer, AutoModel

        self.df = df.reset_index(drop=True)
        self.title_col = title_col
        self.text_col = text_col

        # Config
        self.model_id = os.getenv("RAG_EMBED_MODEL_ID", "sentence-transformers/all-MiniLM-L6-v2")
        device_pref = (os.getenv("RAG_EMBED_DEVICE", "auto") or "auto").strip().lower()
        self.max_len = int(os.getenv("RAG_EMBED_MAX_LENGTH", "256") or "256")
        self.batch_size = int(os.getenv("RAG_EMBED_BATCH_SIZE", "32") or "32")
        self.normalize = _truthy(os.getenv("RAG_EMBED_NORMALIZE", "true"))

        if device_pref == "cpu":
            self.device = "cpu"
        elif device_pref == "cuda":
            self.device = "cuda"
        else:
            # auto
            self.device = "cuda" if torch.cuda.is_available() else "cpu"

        # ✅ Proof log (this is what your grep was missing)
        logger.info(
            "🧠 RAG_CONFIG mode=embeddings enabled=true embed_model=%s embed_device=%s batch=%d maxlen=%d normalize=%s",
            self.model_id, self.device, self.batch_size, self.max_len, self.normalize
        )

        # Load model/tokenizer ONCE (critical for performance)
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_id)
        self.model = AutoModel.from_pretrained(self.model_id).to(self.device)
        self.model.eval()

        # Build KB embeddings ONCE
        texts = _safe_text_series(self.df[text_col]).tolist()
        self.matrix = self._encode_texts(texts)  # shape: (kb_size, dim)

        self.kb_size = len(self.df)
        self.model_name = self.model_id
        self.embedding_dim = int(self.matrix.shape[1]) if self.matrix.size else 0

        # ✅ Proof log: embedding matrix shape + dim
        logger.info(
            "🧠 RAG_EMBEDDINGS_READY kb_rows=%d embedding_dim=%d matrix_shape=%s dtype=%s device=%s",
            self.kb_size,
            self.embedding_dim,
            getattr(self.matrix, "shape", None),
            getattr(self.matrix, "dtype", None),
            self.device,
        )

    def _encode_texts(self, texts: List[str]) -> np.ndarray:
        import torch

        if not texts:
            return np.zeros((0, 0), dtype=np.float32)

        vecs: List[np.ndarray] = []
        with torch.no_grad():
            for i in range(0, len(texts), self.batch_size):
                batch = texts[i:i + self.batch_size]
                enc = self.tokenizer(
                    batch,
                    padding=True,
                    truncation=True,
                    max_length=self.max_len,
                    return_tensors="pt",
                ).to(self.device)

                out = self.model(**enc)

                # Mean pooling over tokens (simple, fast)
                emb = out.last_hidden_state.mean(dim=1)

                if self.normalize:
                    emb = torch.nn.functional.normalize(emb, p=2, dim=1)

                vecs.append(emb.detach().cpu().numpy())

        mat = np.vstack(vecs) if vecs else np.zeros((0, 0), dtype=np.float32)
        # force float32 (smaller + consistent)
        return mat.astype(np.float32, copy=False)

    def retrieve(self, query: str, top_k: int) -> pd.DataFrame:
        import torch

        q = str(query)

        with torch.no_grad():
            enc = self.tokenizer(
                [q],
                padding=True,
                truncation=True,
                max_length=self.max_len,
                return_tensors="pt",
            ).to(self.device)

            out = self.model(**enc)
            q_emb = out.last_hidden_state.mean(dim=1)

            if self.normalize:
                q_emb = torch.nn.functional.normalize(q_emb, p=2, dim=1)

            q_emb = q_emb.detach().cpu().numpy()[0]  # shape: (dim,)

        if self.matrix.size == 0:
            # No KB rows
            return self.df.head(0).copy()

        # Because we normalized, dot product == cosine similarity
        sims = np.dot(self.matrix, q_emb)  # shape: (kb_size,)
        idx = np.argsort(sims)[::-1][:top_k]

        out_df = self.df.iloc[idx].copy()
        out_df["similarity"] = sims[idx]
        out_df["kb_id"] = idx
        return out_df


# =========================
# Public API (unchanged)
# =========================

def build_index_from_env() -> Optional[object]:
    """
    Returns:
      - EmbeddingIndex or TfidfIndex when RAG_ENABLED=true
      - None when RAG_ENABLED is falsey

    Env:
      RAG_ENABLED=true|false
      RAG_KB_PATH=s3://... or /path/to.csv
      RAG_TITLE_COL=title (default)
      RAG_TEXT_COL=text (default)
      RAG_MODE=tfidf|embeddings (default tfidf)
    """
    rag_enabled = _truthy(os.getenv("RAG_ENABLED", "false"))
    if not rag_enabled:
        logger.info("🧠 RAG_ENABLED=false — skipping index build.")
        return None

    kb_path = os.getenv("RAG_KB_PATH")
    if not kb_path:
        raise ValueError("RAG_KB_PATH not set")

    title_col = os.getenv("RAG_TITLE_COL", "title")
    text_col = os.getenv("RAG_TEXT_COL", "text")
    mode = (os.getenv("RAG_MODE", "tfidf") or "tfidf").strip().lower()

    df = _load_kb_csv(kb_path)

    if title_col not in df.columns or text_col not in df.columns:
        raise ValueError(f"KB must contain columns: {title_col}, {text_col}. Found: {list(df.columns)}")

    # ✅ Proof log: mode + KB stats
    logger.info(
        "🧠 Building RAG index | mode=%s | kb_rows=%d | title_col=%s | text_col=%s | kb_path=%s",
        mode, len(df), title_col, text_col, kb_path
    )

    if mode == "embeddings":
        index = EmbeddingIndex(df, title_col, text_col)
        logger.info(
            "🧠 RAG backend initialized: embeddings | model=%s | dim=%s | kb_rows=%d",
            getattr(index, "model_name", "unknown"),
            getattr(index, "embedding_dim", "unknown"),
            getattr(index, "kb_size", len(df)),
        )
        return index

    index = TfidfIndex(df, title_col, text_col)
    logger.info("🧠 RAG backend initialized: tfidf | kb_rows=%d", getattr(index, "kb_size", len(df)))
    return index


def retrieve_kb(query: str, index, top_k: int = 4) -> pd.DataFrame:
    return index.retrieve(query, top_k)


def format_rag_context(
    df: pd.DataFrame,
    max_chars: int = 2500,
    title_col: Optional[str] = None,
    text_col: Optional[str] = None,
) -> str:
    """
    Formats retrieved KB rows into a single prompt block, respecting max_chars.

    If title_col/text_col not provided, defaults to env RAG_TITLE_COL/RAG_TEXT_COL.
    """
    if df is None or getattr(df, "empty", True):
        return ""

    if title_col is None:
        title_col = os.getenv("RAG_TITLE_COL", "title")
    if text_col is None:
        text_col = os.getenv("RAG_TEXT_COL", "text")

    blocks = []
    total = 0

    for _, row in df.iterrows():
        title = str(row.get(title_col, "")).strip()
        text = str(row.get(text_col, "")).strip()
        if not title and not text:
            continue

        block = f"[{title}]\n{text}".strip()

        if total + len(block) > max_chars:
            break

        blocks.append(block)
        total += len(block)

    if not blocks:
        return ""

    return "RELEVANT CONTEXT:\n\n" + "\n\n---\n\n".join(blocks)
