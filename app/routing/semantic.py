"""Bounded nearest-neighbour speech classification with explicit abstention.

The embedding score is cosine similarity, not a calibrated probability.
Neither this module nor its result has access to a signer or transaction API.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import math
import threading
import time

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from app.settings import settings
from .examples import EXAMPLES, EXAMPLE_VERSION


class SpeechUnderstanding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speech_act: Literal["quote", "advice", "research", "explain", "portfolio", "abstain"]
    domain: Literal["crypto", "equity", "wallet", "general"]
    explicit_action: bool = Field(strict=True)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)


@dataclass(frozen=True)
class SemanticMatch:
    understanding: SpeechUnderstanding | None
    method: str
    score: float = 0.0
    margin: float = 0.0
    reason: str = ""


def normalized(text: str) -> str:
    return " ".join(text.lower().split())


def unit(vector: list[float]) -> tuple[float, ...]:
    if not vector or not all(math.isfinite(x) for x in vector):
        raise ValueError("Invalid embedding vector")
    norm = math.sqrt(sum(x*x for x in vector))
    if norm == 0:
        raise ValueError("Empty embedding vector")
    return tuple(x / norm for x in vector)


def rank(query, vectors, examples=EXAMPLES, threshold=.78, margin=.08) -> SemanticMatch:
    if len(vectors) != len(examples) or not vectors:
        raise ValueError("Example/vector count mismatch")
    scores: dict[tuple[str, str], float] = {}
    for vector, (act, domain, _) in zip(vectors, examples):
        if len(query) != len(vector):
            raise ValueError("Embedding dimension mismatch")
        score = sum(a*b for a, b in zip(query, vector))
        scores[(act, domain)] = max(scores.get((act, domain), -1.0), score)
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    (act, domain), top = ranked[0]
    gap = top - ranked[1][1] if len(ranked) > 1 else 0.0
    if top < threshold:
        return SemanticMatch(None, "embedding", top, gap, "low_similarity")
    if gap < margin:
        return SemanticMatch(None, "embedding", top, gap, "low_margin")
    return SemanticMatch(
        SpeechUnderstanding(speech_act=act, domain=domain, explicit_action=act == "quote", confidence=min(1.0, max(0.0, top))),
        "embedding", top, gap, "nearest_label",
    )


class EmbeddingRouter:
    def __init__(self, model: str):
        self.model = model
        self._vectors = None
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, SemanticMatch] = OrderedDict()
        self._retry_at = 0.0

    def classify(self, request: str) -> SemanticMatch:
        text = normalized(request)
        for act, domain, example in EXAMPLES:
            if text == normalized(example):
                return SemanticMatch(SpeechUnderstanding(
                    speech_act=act, domain=domain, explicit_action=act == "quote", confidence=1.0,
                ), "example", 1.0, 1.0, "exact_labelled_example")
        if not settings.intent_embedding_enabled or not settings.openai_api_key:
            return SemanticMatch(None, "embedding", reason="disabled_or_unconfigured")
        if len(request) > 8000:
            return SemanticMatch(None, "embedding", reason="input_limit")
        key = hashlib.sha256(repr((EXAMPLE_VERSION, self.model, settings.intent_embedding_threshold, settings.intent_embedding_margin, text)).encode()).hexdigest()
        # Single-flight initialization and cache misses. A bounded lock wait
        # prevents a slow embedding service from exhausting the chat workers.
        if not self._lock.acquire(timeout=settings.intent_embedding_timeout_seconds):
            return SemanticMatch(None, "embedding", reason="queue_timeout")
        try:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            if time.monotonic() < self._retry_at:
                return SemanticMatch(None, "embedding", reason="circuit_open")
            inputs = [row[2] for row in EXAMPLES] + [text] if self._vectors is None else [text]
            try:
                with OpenAI(api_key=settings.openai_api_key, timeout=settings.intent_embedding_timeout_seconds, max_retries=0) as client:
                    response = client.embeddings.create(model=self.model, input=inputs, encoding_format="float")
                rows = sorted(response.data, key=lambda row: row.index)
                if [row.index for row in rows] != list(range(len(inputs))):
                    raise ValueError("Invalid embedding response indices")
                vectors = [unit(row.embedding) for row in rows]
                if self._vectors is None:
                    self._vectors = vectors[:-1]
                result = rank(vectors[-1], self._vectors, threshold=settings.intent_embedding_threshold, margin=settings.intent_embedding_margin)
            except Exception:
                self._retry_at = time.monotonic() + 30
                return SemanticMatch(None, "embedding", reason="provider_unavailable")
            self._cache[key] = result
            while len(self._cache) > max(1, settings.intent_embedding_cache_entries):
                self._cache.popitem(last=False)
            return result
        finally:
            self._lock.release()


@lru_cache(maxsize=2)
def embedding_router(model: str) -> EmbeddingRouter:
    return EmbeddingRouter(model)
