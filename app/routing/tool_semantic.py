"""Per-capability tool-description embedding fallback for ProviderRouter's
hard regex gate (app/provider_router.py's candidates()).

Not a replacement for ProviderTool.matches -- an OR-alternative consulted
only for capability+chain-eligible tools whose regex gate already returned
False this call. Mirrors app/routing/semantic.py's caching/locking/circuit-
breaker shape (that module is the proven pattern for embedding-based
routing in this codebase; this reuses it rather than inventing a new one),
but the ranking model is different on purpose: semantic.py picks one
winner and abstains on a close call (appropriate for a mutually-exclusive
intent classifier); tool retrieval is not mutually exclusive -- several
gated tools can legitimately all be relevant to one request, so this
returns the *set* of tools clearing a similarity threshold, not a single
argmax winner. Final selection among whatever becomes a candidate is still
ProviderRouter._score()'s job, exactly as it is for regex-matched tools.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import threading
import time

from openai import OpenAI

from app.call_budget import charge_and_check
from app.settings import settings
from .semantic import normalized, unit


@dataclass(frozen=True)
class ToolSemanticMatch:
    qualifying: frozenset[str]
    method: str
    reason: str = ""


class ToolEmbeddingRouter:
    def __init__(self, model: str):
        self.model = model
        self._vectors: dict[str, list[tuple[float, ...]]] = {}  # capability -> description vectors
        self._names: dict[str, tuple[str, ...]] = {}  # capability -> tool names, same order as _vectors
        # Defensive staleness guard: the provider registry is built once via
        # get_provider_router()'s own caching, so a capability's gated-tool
        # composition should never actually change within a process -- but a
        # silent stale-cache mismatch (serving one tool's cached vector for a
        # different tool of the same name) would be a bad failure mode to
        # leave undetected, so it's checked rather than assumed.
        self._signature: dict[str, str] = {}
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, ToolSemanticMatch] = OrderedDict()
        self._retry_at = 0.0

    def qualify(self, request: str, capability: str, gated: tuple[tuple[str, str], ...]) -> ToolSemanticMatch:
        """gated: (tool_name, description) pairs for capability-eligible
        tools whose own matches() already returned False this call. Empty
        is a free no-op -- callers should only invoke this when there's
        something to fall back for; this function doesn't itself know
        anything about ProviderTool or its regex gate.
        """
        if not gated:
            return ToolSemanticMatch(frozenset(), "embedding", "no_gated_tools")
        if not settings.tool_embedding_enabled or not settings.openai_api_key:
            return ToolSemanticMatch(frozenset(), "embedding", "disabled_or_unconfigured")
        text = normalized(request)
        if len(request) > 8000:
            return ToolSemanticMatch(frozenset(), "embedding", "input_limit")

        names = tuple(name for name, _ in gated)
        signature = hashlib.sha256(repr(gated).encode()).hexdigest()
        key = hashlib.sha256(repr((capability, signature, self.model, settings.tool_embedding_threshold, text)).encode()).hexdigest()

        if not self._lock.acquire(timeout=settings.tool_embedding_timeout_seconds):
            return ToolSemanticMatch(frozenset(), "embedding", "queue_timeout")
        try:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            if time.monotonic() < self._retry_at:
                return ToolSemanticMatch(frozenset(), "embedding", "circuit_open")

            cached_vectors = self._vectors.get(capability)
            stale = self._signature.get(capability) != signature
            descriptions = tuple(desc for _, desc in gated)
            inputs = list(descriptions) + [text] if (cached_vectors is None or stale) else [text]

            if not charge_and_check(settings.tool_embedding_cost_usd):
                return ToolSemanticMatch(frozenset(), "embedding", "budget_exhausted")
            try:
                with OpenAI(api_key=settings.openai_api_key, timeout=settings.tool_embedding_timeout_seconds, max_retries=0) as client:
                    response = client.embeddings.create(model=self.model, input=inputs, encoding_format="float")
                rows = sorted(response.data, key=lambda row: row.index)
                if [row.index for row in rows] != list(range(len(inputs))):
                    raise ValueError("Invalid embedding response indices")
                vectors = [unit(row.embedding) for row in rows]
                if cached_vectors is None or stale:
                    cached_vectors = vectors[:-1]
                    self._vectors[capability] = cached_vectors
                    self._names[capability] = names
                    self._signature[capability] = signature
                query_vector = vectors[-1]
                scores = {
                    name: sum(a * b for a, b in zip(query_vector, vector))
                    for name, vector in zip(self._names[capability], cached_vectors)
                }
                qualifying = frozenset(
                    name for name in names if scores.get(name, -1.0) >= settings.tool_embedding_threshold
                )
                result = ToolSemanticMatch(qualifying, "embedding", "qualified" if qualifying else "below_threshold")
            except Exception:
                self._retry_at = time.monotonic() + 30
                return ToolSemanticMatch(frozenset(), "embedding", "provider_unavailable")
            self._cache[key] = result
            while len(self._cache) > max(1, settings.tool_embedding_cache_entries):
                self._cache.popitem(last=False)
            return result
        finally:
            self._lock.release()


@lru_cache(maxsize=8)
def tool_embedding_router(model: str) -> ToolEmbeddingRouter:
    return ToolEmbeddingRouter(model)
