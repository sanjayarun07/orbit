"""Provider-neutral routing with quota, cost, health, cache and fallback policy."""

from __future__ import annotations

from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
import logging
import re
from threading import Condition, Lock
import time
from typing import Callable

from app.metrics import increment
from app.provider_analytics import emit_provider_event
from app.settings import settings


Handler = Callable[[str], str]
logger = logging.getLogger(__name__)
Enabled = Callable[[], bool]
Matches = Callable[[str], bool]
_WORD = re.compile(r"[a-z0-9]+")
_LIVE_QUOTE_REQUEST = re.compile(
    r"\b(?:price|quote|trading\s+(?:at|for)|worth)\b.*\b(?:now|current|currently|live|today)\b"
    r"|\b(?:now|current|currently|live|today)\b.*\b(?:price|quote|trading\s+(?:at|for)|worth)\b",
    re.IGNORECASE,
)
_QUOTE_VALUE = re.compile(
    r"(?:[$€£₹]\s*\d[\d,]*(?:\.\d+)?)"
    r"|(?:\b\d[\d,]*(?:\.\d+)?\s*(?:usd|usdt|usdc|eur|gbp|inr|dollars?|euros?|pounds?|rupees?)\b)"
    r"|(?:\b(?:price|trades?|trading)\s+(?:is|at|for)\s+\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE,
)
_UNUSABLE_OUTPUT = re.compile(
    r"\b(?:i|we)\s+(?:can(?:not|'t)|could(?:not|'t)|am\s+unable|are\s+unable)\s+"
    r"(?:(?:currently|reliably|directly)\s+){0,2}(?:retrieve|access|provide|fetch|get)\b"
    r"|\b(?:market(?:-data)?|financial?|real[- ]time|live|current)\s+(?:data|information|prices?)\s+"
    r"(?:access\s+)?is\s+(?:currently\s+)?unavailable\b"
    r"|\b(?:do\s+not|don't)\s+have\s+access\s+to\s+(?:live|current|real[- ]time)\b",
    re.IGNORECASE,
)


def is_usable_provider_output(request: str, output: object, *, require_live_quote: bool = False) -> bool:
    """Reject soft failures that upstream APIs sometimes return as HTTP 200 text."""
    if not isinstance(output, str) or not output.strip():
        return False
    normalized = output.replace("’", "'").replace("‘", "'")
    refusal = _UNUSABLE_OUTPUT.search(normalized)
    if refusal and refusal.start() < 160:
        return False
    if require_live_quote and _LIVE_QUOTE_REQUEST.search(request) and not _QUOTE_VALUE.search(normalized):
        return False
    return True


@dataclass(frozen=True)
class ProviderTool:
    name: str
    provider: str
    capabilities: tuple[str, ...]
    handler: Handler
    enabled: Enabled = lambda: True
    matches: Matches = lambda _request: True
    keywords: tuple[str, ...] = ()
    chains: tuple[str, ...] = ()
    cost_usd: float = 0.0
    quota_per_minute: int = 60
    cache_ttl_seconds: int = 60
    priority: float = 0.0
    risk: str = "read_only"


@dataclass(frozen=True)
class ProviderResult:
    output: str
    tool: str
    provider: str
    cached: bool = False
    attempted: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()


@dataclass
class _Health:
    calls: int = 0
    successes: int = 0
    consecutive_failures: int = 0
    latency_ewma_ms: float | None = None
    circuit_open_until: float = 0.0

    @property
    def reliability(self) -> float:
        return self.successes / self.calls if self.calls else 1.0


@dataclass
class _CacheEntry:
    expires_at: float
    output: str
    words: frozenset[str] = field(default_factory=frozenset)


class ProviderRouter:
    """Rank interchangeable providers, then execute with transparent fallback."""

    def __init__(self) -> None:
        self._tools: list[ProviderTool] = []
        self._health: defaultdict[str, _Health] = defaultdict(_Health)
        self._quota: defaultdict[str, deque[float]] = defaultdict(deque)
        self._cache: OrderedDict[tuple[str, str], _CacheEntry] = OrderedDict()
        self._inflight: set[tuple[str, str]] = set()
        self._condition = Condition()
        self._lock = Lock()
        self._overrides: dict[str, dict[str, bool | float | int]] = {}

    def register(self, tool: ProviderTool) -> None:
        if any(existing.name == tool.name for existing in self._tools):
            raise ValueError(f"Provider tool already registered: {tool.name}")
        self._tools.append(tool)

    def apply_overrides(self, overrides: dict[str, dict]) -> None:
        known = {tool.name for tool in self._tools}
        allowed = {"enabled", "priority", "quota_per_minute", "cost_usd"}
        with self._lock:
            self._overrides = {
                name: {key: value for key, value in policy.items() if key in allowed}
                for name, policy in overrides.items()
                if name in known and isinstance(policy, dict)
            }

    def configure(self, tool_name: str, policy: dict) -> dict:
        if not any(tool.name == tool_name for tool in self._tools):
            raise KeyError(tool_name)
        allowed = {"enabled", "priority", "quota_per_minute", "cost_usd"}
        with self._lock:
            current = self._overrides.setdefault(tool_name, {})
            current.update({key: value for key, value in policy.items() if key in allowed and value is not None})
            return dict(current)

    def configure_provider(self, provider: str, enabled: bool) -> int:
        names = [tool.name for tool in self._tools if tool.provider == provider]
        if not names:
            raise KeyError(provider)
        with self._lock:
            for name in names:
                self._overrides.setdefault(name, {})["enabled"] = enabled
        return len(names)

    def reset(self, tool_name: str) -> None:
        with self._lock:
            self._overrides.pop(tool_name, None)

    def overrides(self) -> dict[str, dict]:
        with self._lock:
            return {name: dict(policy) for name, policy in self._overrides.items()}

    def _effective(self, tool: ProviderTool, key: str):
        return self._overrides.get(tool.name, {}).get(key, getattr(tool, key))

    def _enabled(self, tool: ProviderTool) -> bool:
        configured = tool.enabled()
        override = self._overrides.get(tool.name, {}).get("enabled")
        return configured and override is not False

    @staticmethod
    def _normalized(request: str) -> str:
        return " ".join(_WORD.findall(request.lower()))

    @staticmethod
    def _words(request: str) -> frozenset[str]:
        return frozenset(_WORD.findall(request.lower()))

    def _quota_remaining(self, tool: ProviderTool, now: float, reserve: bool = False) -> int:
        with self._lock:
            usage = self._quota[tool.name]
            while usage and usage[0] <= now - 60:
                usage.popleft()
            limit = int(self._effective(tool, "quota_per_minute"))
            remaining = max(0, limit - len(usage))
            if reserve and remaining:
                usage.append(now)
            return remaining

    def _score(self, tool: ProviderTool, request: str, capability: str, chains: tuple[str, ...]) -> float:
        now = time.monotonic()
        health = self._health[tool.name]
        if health.circuit_open_until > now or not self._quota_remaining(tool, now):
            return float("-inf")
        words = self._words(request)
        keyword_hits = sum(1 for keyword in tool.keywords if keyword.lower() in request.lower())
        chain_fit = 2.0 if chains and set(chains) & set(tool.chains) else 0.0
        if chains and tool.chains and not set(chains) & set(tool.chains):
            chain_fit = -5.0
        latency_penalty = min((health.latency_ewma_ms or 0) / 2000, 2.0)
        cost_penalty = float(self._effective(tool, "cost_usd")) * settings.provider_cost_weight
        breadth_penalty = max(0, len(words) - 30) * 0.01
        return (
            float(self._effective(tool, "priority"))
            + 8.0
            + keyword_hits * 2.0
            + chain_fit
            + health.reliability * settings.provider_health_weight
            - health.consecutive_failures * 2.0
            - latency_penalty
            - cost_penalty
            - breadth_penalty
        )

    def candidates(
        self,
        request: str,
        capability: str,
        chains: tuple[str, ...] = (),
        provider: str | None = None,
    ) -> list[ProviderTool]:
        candidates = [
            tool
            for tool in self._tools
            if capability in tool.capabilities
            and (provider is None or tool.provider == provider)
            and tool.risk == "read_only"
            and self._enabled(tool)
            and tool.matches(request)
        ]
        scored = [
            (self._score(tool, request, capability, chains), tool)
            for tool in candidates
        ]
        return [
            tool
            for score, tool in sorted(scored, key=lambda item: item[0], reverse=True)
            if score != float("-inf")
        ]

    def _cache_get(self, capability: str, request: str) -> str | None:
        normalized = self._normalized(request)
        key = (capability, normalized)
        now = time.monotonic()
        with self._condition:
            exact = self._cache.get(key)
            if exact and exact.expires_at > now:
                self._cache.move_to_end(key)
                return exact.output
            words = self._words(request)
            if len(words) >= 4:
                for cache_key, entry in reversed(self._cache.items()):
                    if cache_key[0] != capability or entry.expires_at <= now:
                        continue
                    union = words | entry.words
                    similarity = len(words & entry.words) / len(union) if union else 0
                    if similarity >= settings.provider_semantic_cache_threshold:
                        return entry.output
        return None

    def _cache_set(self, capability: str, request: str, output: str, ttl: int) -> None:
        key = (capability, self._normalized(request))
        with self._condition:
            self._cache[key] = _CacheEntry(time.monotonic() + ttl, output, self._words(request))
            self._cache.move_to_end(key)
            while len(self._cache) > settings.provider_cache_max_entries:
                self._cache.popitem(last=False)

    def _record(self, tool: ProviderTool, success: bool, latency_ms: float) -> None:
        with self._lock:
            health = self._health[tool.name]
            health.calls += 1
            health.successes += int(success)
            health.latency_ewma_ms = (
                latency_ms
                if health.latency_ewma_ms is None
                else health.latency_ewma_ms * 0.8 + latency_ms * 0.2
            )
            if success:
                health.consecutive_failures = 0
            else:
                health.consecutive_failures += 1
                if health.consecutive_failures >= settings.provider_circuit_failure_threshold:
                    health.circuit_open_until = time.monotonic() + settings.provider_circuit_cooldown_seconds
        emit_provider_event(tool.name, tool.provider, success, latency_ms)

    def route(
        self,
        request: str,
        capability: str,
        chains: tuple[str, ...] = (),
        *,
        provider: str | None = None,
    ) -> ProviderResult:
        cache_capability = f"{capability}@{provider}" if provider else capability
        cached = self._cache_get(cache_capability, request)
        if cached is not None:
            increment("provider_semantic_cache_hits")
            return ProviderResult(cached, "semantic_cache", "cache", cached=True)

        key = (cache_capability, self._normalized(request))
        with self._condition:
            while key in self._inflight:
                self._condition.wait(timeout=settings.provider_request_timeout_seconds)
                cached = self._cache_get(cache_capability, request)
                if cached is not None:
                    increment("provider_coalesced_calls")
                    return ProviderResult(cached, "semantic_cache", "cache", cached=True)
            self._inflight.add(key)

        attempted: list[str] = []
        failures: list[str] = []
        try:
            for tool in self.candidates(request, capability, chains, provider):
                if not self._quota_remaining(tool, time.monotonic(), reserve=True):
                    increment(f"provider_{tool.provider}_quota_skips")
                    continue
                attempted.append(tool.name)
                started = time.monotonic()
                increment(f"provider_{tool.provider}_calls")
                effective_cost = float(self._effective(tool, "cost_usd"))
                increment("provider_estimated_cost_microusd", round(effective_cost * 1_000_000))
                try:
                    output = tool.handler(request)
                    if not is_usable_provider_output(request, output):
                        raise RuntimeError("provider returned an unusable or ungrounded response")
                    self._record(tool, True, (time.monotonic() - started) * 1000)
                    self._cache_set(cache_capability, request, output, tool.cache_ttl_seconds)
                    return ProviderResult(output, tool.name, tool.provider, False, tuple(attempted), tuple(failures))
                except Exception as exc:
                    self._record(tool, False, (time.monotonic() - started) * 1000)
                    increment(f"provider_{tool.provider}_errors")
                    logger.warning("Provider tool %s failed", tool.name, exc_info=True)
                    reason = (
                        "unusable or ungrounded response"
                        if str(exc) == "provider returned an unusable or ungrounded response"
                        else "provider request failed"
                    )
                    failures.append(f"{tool.name}: {reason}")
                    continue
            raise RuntimeError("No configured provider succeeded")
        finally:
            with self._condition:
                self._inflight.discard(key)
                self._condition.notify_all()

    def catalog(self) -> list[dict]:
        now = time.monotonic()
        rows = []
        for tool in self._tools:
            health = self._health[tool.name]
            rows.append(
                {
                    "name": tool.name,
                    "provider": tool.provider,
                    "capabilities": list(tool.capabilities),
                    "chains": list(tool.chains),
                    "risk": tool.risk,
                    "configured": tool.enabled(),
                    "enabled": self._enabled(tool),
                    "priority": float(self._effective(tool, "priority")),
                    "cost_usd_per_invocation": float(self._effective(tool, "cost_usd")),
                    "quota_per_minute": int(self._effective(tool, "quota_per_minute")),
                    "quota_remaining": self._quota_remaining(tool, now),
                    "healthy": health.circuit_open_until <= now,
                    "reliability": round(health.reliability, 4) if health.calls else None,
                    "latency_ewma_ms": round(health.latency_ewma_ms, 1) if health.latency_ewma_ms else None,
                    "overrides": dict(self._overrides.get(tool.name, {})),
                }
            )
        return rows

    def preview(self, request: str, capability: str, chains: tuple[str, ...] = ()) -> list[dict]:
        catalog = {row["name"]: row for row in self.catalog()}
        return [catalog[tool.name] for tool in self.candidates(request, capability, chains)]
