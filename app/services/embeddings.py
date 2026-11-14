"""Turning text into vectors.

Two interchangeable backends:

* HashingEmbedder: the default. Pure numpy, no downloads, no API key, fully deterministic.
  It uses the "hashing trick": each word (and word pair) is hashed into one of DIM buckets,
  so the vocabulary never needs to be stored or re-fitted when articles change.
  It matches on shared words (after stop-word removal and light stemming), so it behaves
  like smart keyword search. Good enough for a demo; not semantic.
* SentenceTransformerEmbedder: real semantic embeddings (understands "money back" ~ "refund").
  Optional, needs `pip install sentence-transformers` and a one-time model download.
"""
import hashlib
import math
import re
from collections import Counter
from functools import lru_cache
from typing import Protocol

import numpy as np

from app.core.config import settings

STOPWORDS = frozenset(
    """a about after again all am an and any are as at be because been before being but by can
    could did do does doing for from get got had has have having he her here him his how i if in
    into is it its just me more most my no nor not of off on once only or other our out over own
    same she should so some such than that the their them then there these they this those through
    to too under until up very was we were what when where which while who why will with would
    you your please help need want hi hello thanks thank""".split()
)

_TOKEN = re.compile(r"[a-z0-9]+")


def _stem(token: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if token.endswith(suffix) and not token.endswith("ss") and len(token) - len(suffix) >= 3:
            token = token[: -len(suffix)]
            break
    if token.endswith("e") and len(token) > 3:
        token = token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    """Lowercase, drop stop words, stem. Shared with the mock LLM for sentence matching."""
    return [_stem(t) for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS and len(t) > 1]


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> np.ndarray:
        """Return an (n, dim) float32 array of L2-normalised vectors."""


class HashingEmbedder:
    DIM = 2048
    name = "hashing-v1"

    def _bucket(self, feature: str) -> tuple[int, float]:
        digest = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
        return digest % self.DIM, 1.0 if (digest >> 63) & 1 else -1.0

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.DIM), dtype=np.float32)
        for row, text in enumerate(texts):
            tokens = tokenize(text)
            features = Counter(tokens)
            features.update(f"{a}_{b}" for a, b in zip(tokens, tokens[1:]))  # word pairs
            for feature, count in features.items():
                idx, sign = self._bucket(feature)
                out[row, idx] += sign * (1.0 + math.log(count))
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return out / norms


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "EMBEDDING_BACKEND=sentence-transformers needs: pip install sentence-transformers"
            ) from exc
        self._model = SentenceTransformer(model_name)
        self.name = f"st:{model_name}"

    def embed(self, texts: list[str]) -> np.ndarray:  # pragma: no cover
        vectors = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return np.asarray(vectors, dtype=np.float32)


@lru_cache
def get_embedder() -> Embedder:
    if settings.embedding_backend == "sentence-transformers":
        return SentenceTransformerEmbedder(settings.embedding_model)
    return HashingEmbedder()
