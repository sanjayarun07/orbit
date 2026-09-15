"""Embedder interface. OpenAI text-embedding-3-small when a key is present
(dimension configurable to match the schema), otherwise a deterministic
hashing embedder so the service -- and its tests -- work offline. Swap in a
self-hosted Qwen embedder here without touching the store."""

from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Protocol

from app.settings import settings

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    dim: int
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Bag-of-words feature hashing with bigrams; cosine-comparable, no
    network, no model. Good enough to exercise the pipeline and as a last
    resort; not a substitute for a real model in production."""

    name = "hashing"

    def __init__(self, dim: int = 256):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vector = [0.0] * self.dim
            tokens = re.findall(r"[a-z0-9$#]+", (text or "").lower())
            grams = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
            for gram in grams:
                digest = hashlib.blake2b(gram.encode(), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % self.dim
                sign = 1.0 if digest[4] % 2 == 0 else -1.0
                vector[index] += sign
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            out.append([v / norm for v in vector])
        return out


class OpenAIEmbedder:
    name = "openai"

    def __init__(self, model: str, dim: int):
        from openai import OpenAI

        self._client = OpenAI(api_key=settings.openai_api_key)
        self.model = model
        self.dim = dim

    # OpenAI caps one embeddings request at 300k tokens and 2,048 inputs; a docs
    # site's llms-full.txt can be a single 470k-token document, so batch.
    BATCH = 128

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self.BATCH):
            batch = [t[:8000] for t in texts[start:start + self.BATCH]]
            response = self._client.embeddings.create(model=self.model, input=batch, dimensions=self.dim)
            out.extend(item.embedding for item in response.data)
        return out


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        if settings.openai_api_key and settings.knowledge_embedding_provider == "openai":
            try:
                _embedder = OpenAIEmbedder(settings.knowledge_embedding_model, settings.knowledge_embedding_dim)
            except Exception:
                logger.warning("knowledge: OpenAI embedder unavailable; using hashing embedder", exc_info=True)
                _embedder = HashingEmbedder(settings.knowledge_embedding_dim)
        else:
            _embedder = HashingEmbedder(settings.knowledge_embedding_dim)
    return _embedder


def set_embedder(embedder: Embedder | None) -> None:
    global _embedder
    _embedder = embedder


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
