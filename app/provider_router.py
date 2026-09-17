"""Provider-neutral routing with quota, cost, health, cache and fallback policy."""

from __future__ import annotations

from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
import logging
import re
from threading import Condition, Lock
import time
from typing import Any, Callable

import httpx

from app import streaming, tool_outcomes
from app.call_budget import charge_and_check
from app.metrics import increment
from app.provider_analytics import emit_provider_event
from app.routing.tool_semantic import tool_embedding_router
from app.routing.tool_selector import select_tool
from app.settings import settings


Handler = Callable[[str], str]
logger = logging.getLogger(__name__)
Enabled = Callable[[], bool]
Matches = Callable[[str], bool]


def _always_match(_request: str) -> bool:
    return True


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


# Worth one quick retry before falling back to a worse/cheaper candidate --
# never for a ValueError or the "ungrounded response" RuntimeError raised
# below, neither of which a retry of the identical request can fix.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_RETRY_BACKOFF_SECONDS = 0.3


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return False


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
    # Default is the shared _always_match sentinel (identity-checkable): a tool
    # that never sets matches= is a broad catch-all for its capability, which
    # matched_capabilities() must skip so it doesn't claim every request.
    matches: Matches = _always_match
    keywords: tuple[str, ...] = ()
    chains: tuple[str, ...] = ()
    cost_usd: float = 0.0
    quota_per_minute: int = 60
    cache_ttl_seconds: int = 60
    priority: float = 0.0
    risk: str = "read_only"
    # Opt-in embedding-fallback text (see candidates() and
    # app/routing/tool_semantic.py) -- tools that never set this are
    # entirely unaffected by the semantic fallback: zero embedding calls,
    # zero behavior change. Only set on tools where the regex `matches`
    # gate is known to have real, hard-to-close coverage gaps.
    description: str | None = None
    # Attached by app.tool_catalog.attach_specs: what the tool's API can and
    # cannot answer, scored in _score() via spec.fit(request).
    spec: Any = None


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
    retries: int = 0

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

    def tools(self) -> tuple[ProviderTool, ...]:
        return tuple(self._tools)

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

    def _score(self, tool: ProviderTool, request: str, chains: tuple[str, ...]) -> float:
        # Deliberately capability-independent: a tool's fitness for a request
        # (priority, keyword specificity, chain fit, health, cost) does not
        # depend on which capability bucket it was reached through. That is
        # what makes ranking a UNION of tools drawn from several capabilities
        # (try_route_across) sound -- see _ranked_union.
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
        # What the tool's API can answer versus what the request asks for
        # (app.tool_catalog): a volume ranking asked of a paid-boosts tool is
        # pushed down no matter how many keywords overlap.
        spec_fit = float(tool.spec.fit(request)) if tool.spec is not None else 0.0
        return (
            float(self._effective(tool, "priority"))
            + 8.0
            + keyword_hits * 2.0
            + chain_fit
            + spec_fit
            + health.reliability * settings.provider_health_weight
            + tool_outcomes.adjustment(tool.name)
            - health.consecutive_failures * 2.0
            - latency_penalty
            - cost_penalty
            - breadth_penalty
        )

    def _select_candidates(
        self,
        request: str,
        capability: str,
        chains: tuple[str, ...],
        provider: str | None,
        allow_semantic_fallback: bool,
    ) -> tuple[list[ProviderTool], set[str]]:
        """Return the tools eligible for one capability plus the subset that
        matched via their real regex `matches` gate (as opposed to the
        embedding fallback).

        A tool's regex `matches` gate is a hard, all-or-nothing filter -- if
        it returns False the tool is normally excluded entirely. When
        allow_semantic_fallback is True (the default), a capability+chain-
        eligible, description-bearing tool that failed its regex gate gets one
        more chance via embedding similarity (see app/routing/tool_semantic.py)
        before being dropped; tools without a `description` are unaffected
        either way. allow_semantic_fallback=False is for callers outside an
        active chat turn (the admin preview() endpoints), where there is no
        call-budget to bound repeat embedding spend against.
        """
        eligible = [
            tool
            for tool in self._tools
            if capability in tool.capabilities
            and (provider is None or tool.provider == provider)
            and tool.risk == "read_only"
            and self._enabled(tool)
        ]
        selected: list[ProviderTool] = []
        regex_matched: set[str] = set()
        gated: list[ProviderTool] = []
        for tool in eligible:
            if tool.matches(request):
                selected.append(tool)
                regex_matched.add(tool.name)
            elif allow_semantic_fallback and tool.description and (
                not chains or not tool.chains or set(chains) & set(tool.chains)
            ):
                # Chain compatibility is a hard filter here even though it's
                # only ever soft-scored for regex-matched tools elsewhere --
                # text similarity alone must never approve a tool for a chain
                # it doesn't support.
                gated.append(tool)
        if gated:
            match = tool_embedding_router(settings.tool_embedding_model).qualify(
                request, capability, tuple((tool.name, tool.description) for tool in gated),
            )
            increment(f"provider_tool_semantic_{match.reason}")
            selected.extend(tool for tool in gated if tool.name in match.qualifying)
        return selected, regex_matched

    def _rank(
        self,
        tools: list[ProviderTool],
        regex_matched: set[str],
        request: str,
        chains: tuple[str, ...],
    ) -> list[ProviderTool]:
        scored = [(self._score(tool, request, chains), tool) for tool in tools]
        return [
            tool
            for score, tool in sorted(
                scored,
                # A tool that only qualified through the embedding fallback
                # (not its own regex `matches` gate) is a weaker, fuzzier
                # signal than an explicit keyword match and must never outrank
                # one purely on raw priority number.
                key=lambda item: (item[1].name in regex_matched, item[0]),
                reverse=True,
            )
            if score != float("-inf")
        ]

    def candidates(
        self,
        request: str,
        capability: str,
        chains: tuple[str, ...] = (),
        provider: str | None = None,
        allow_semantic_fallback: bool = True,
    ) -> list[ProviderTool]:
        selected, regex_matched = self._select_candidates(
            request, capability, chains, provider, allow_semantic_fallback
        )
        ranked = self._rank(selected, regex_matched, request, chains)
        return select_tool(request, ranked, chains)

    def matched_capabilities(self, request: str, chains: tuple[str, ...] = ()) -> set[str]:
        """Capabilities of every enabled tool whose OWN narrow `matches` gate
        fires for this request (chain-compatible).

        This is the reachability backstop that makes tool selection independent
        of the upstream lexicon: a tool that specifically recognizes a request
        (goldrush_hyperliquid_market on "funding rate ... hyperliquid",
        dexscreener_boosted_tokens on "trending tokens on pump.fun", ...) is
        reachable even when the intent classifier never emitted its capability.
        Broad catch-all tools (matches is the _always_match default -- web
        search, dexscreener_pair_search) are deliberately excluded: they match
        everything and would make every capability look reachable, defeating
        the point. Requiring a real gate also preserves the "keyword-less
        request stays out of the deterministic router" property for free -- a
        bare "tell me about Ethereum" fires no narrow gate, so nothing is
        surfaced here.
        """
        matched: set[str] = set()
        for tool in self._tools:
            if tool.matches is _always_match or tool.risk != "read_only" or not self._enabled(tool):
                continue
            if chains and tool.chains and not (set(chains) & set(tool.chains)):
                continue
            if tool.matches(request):
                matched.update(tool.capabilities)
        return matched

    def _ranked_union(
        self,
        request: str,
        capabilities: tuple[str, ...],
        chains: tuple[str, ...],
        provider: str | None,
    ) -> list[ProviderTool]:
        """Rank the UNION of candidates across several capabilities in one
        pass. A tool registered under more than one of the requested
        capabilities appears once; the regex-matched set is unioned so the
        regex > semantic-fallback tiebreak in _rank still holds globally.
        """
        union: OrderedDict[str, ProviderTool] = OrderedDict()
        regex_matched: set[str] = set()
        for capability in capabilities:
            selected, matched = self._select_candidates(
                request, capability, chains, provider, allow_semantic_fallback=True
            )
            regex_matched |= matched
            for tool in selected:
                union.setdefault(tool.name, tool)
        ranked = self._rank(list(union.values()), regex_matched, request, chains)
        return select_tool(request, ranked, chains)

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

    def _invoke_with_retry(self, tool: ProviderTool, request: str, effective_cost: float) -> str:
        """One bounded retry for a transient (network/5xx/429) failure only
        -- a ValueError or any other logic error propagates immediately, as
        does a transient failure that can't clear the same quota/budget
        checks route() already applies (a retry is an honest second call to
        that paid API, not a free one). Every real provider call passes here,
        so this is where a streaming client learns which tool is running."""
        streaming.emit("status", text=f"Running {tool.name.replace('_', ' ')}")
        try:
            return tool.handler(request)
        except Exception as exc:
            if not _is_transient(exc):
                raise
            if not self._quota_remaining(tool, time.monotonic(), reserve=True):
                raise
            if not charge_and_check(effective_cost):
                raise
            increment(f"provider_{tool.provider}_retries")
            with self._lock:
                self._health[tool.name].retries += 1
            time.sleep(_RETRY_BACKOFF_SECONDS)
            return tool.handler(request)  # a second failure propagates normally

    def route(
        self,
        request: str,
        capability: str,
        chains: tuple[str, ...] = (),
        *,
        provider: str | None = None,
    ) -> ProviderResult:
        """Strict variant: raises RuntimeError when no provider succeeds.

        Kept for callers (and tests) that genuinely want an exception on the
        empty case. Callers that treat "no provider succeeded" as a normal,
        recoverable outcome -- which is every caller in this app that falls
        back to another path -- should use try_route() and branch on None
        instead of relying on catching this RuntimeError, since a forgotten
        `except` silently reintroduces the crash class this split removes.
        """
        result = self.try_route(request, capability, chains, provider=provider)
        if result is None:
            raise RuntimeError("No configured provider succeeded")
        return result

    def try_route(
        self,
        request: str,
        capability: str,
        chains: tuple[str, ...] = (),
        *,
        provider: str | None = None,
    ) -> ProviderResult | None:
        """Non-raising variant: returns None when no configured provider
        succeeds (none eligible, all failed, or the per-turn budget was
        reached) instead of raising, so the empty case is handled at the
        call site by branching on None rather than by remembering to catch.
        """
        cache_capability = f"{capability}@{provider}" if provider else capability
        return self._route_ranked(
            request,
            cache_capability,
            lambda: self.candidates(request, capability, chains, provider),
        )

    def invoke(self, tool_name: str, request: str, chains: tuple[str, ...] = ()) -> ProviderResult | None:
        """Run ONE named read-only tool on a request. The caller chose the tool
        explicitly (an MCP host, the admin lab), so the request->tool matchers
        are bypassed -- but not quota, circuit breaker, budget, cache, retries
        or outcome accounting, which all live in _route_ranked. Raises KeyError
        for an unknown name; returns None when the tool is disabled,
        unconfigured, not read-only, or fails."""
        tool = next((t for t in self._tools if t.name == tool_name), None)
        if tool is None:
            raise KeyError(tool_name)
        if tool.risk != "read_only" or not self._enabled(tool):
            return None
        return self._route_ranked(request, f"{tool.capabilities[0]}@tool:{tool.name}", lambda: [tool])

    def try_route_across(
        self,
        request: str,
        capabilities: tuple[str, ...],
        chains: tuple[str, ...] = (),
        *,
        provider: str | None = None,
    ) -> ProviderResult | None:
        """Route a request that legitimately spans several capabilities by
        ranking every eligible tool from ALL of them in one pass, so the
        single best-scoring tool wins regardless of which capability bucket
        it sits in.

        This replaces the old "pick one capability by a fixed priority order,
        then route within it" step (app/nodes/research.py), which discarded
        the tool-level specificity signal `_score` already computes and let a
        broad tool in an earlier-ordered bucket beat a specific tool in a
        later one (e.g. a "recent trades" request answered with top-holders
        data). Same None contract and same cache/coalescing as try_route.
        """
        caps = tuple(dict.fromkeys(capabilities))
        if not caps:
            return None
        if len(caps) == 1:
            return self.try_route(request, caps[0], chains, provider=provider)
        suffix = f"@{provider}" if provider else ""
        cache_capability = "|".join(sorted(caps)) + suffix
        return self._route_ranked(
            request,
            cache_capability,
            lambda: self._ranked_union(request, caps, chains, provider),
        )

    def plan_across(
        self,
        request: str,
        capabilities: tuple[str, ...],
        chains: tuple[str, ...] = (),
        limit: int = 3,
    ) -> list[ProviderTool]:
        """The tools one request should run, best first: the top-ranked tool,
        then up to `limit - 1` more that (a) claim this request through their
        OWN matcher -- never a catch-all riding along on its capability --
        and (b) add ground the chosen set does not cover, by catalog
        dimension (liquidity, holders, security, ...) or, without a spec, by
        capability. "Is BONK safe" is a security dossier AND the market
        overview the same mint resolves; "price of SOL" is one tool, because a
        second price tool covers the same ground. Cost stays bounded by
        `limit` and the per-turn budget every invocation charges."""
        caps = tuple(dict.fromkeys(capabilities))
        if not caps:
            return []
        ranked = self._ranked_union(request, caps, chains, None)
        chosen: list[ProviderTool] = []
        covered: set[str] = set()

        def ground(tool: ProviderTool) -> set[str]:
            dims = set(tool.spec.dimensions) if tool.spec is not None and tool.spec.dimensions else set()
            return dims or set(tool.capabilities)

        for tool in ranked:
            if chosen:
                if tool.matches is _always_match or not tool.matches(request):
                    continue
                if ground(tool) <= covered:
                    continue
            chosen.append(tool)
            covered |= ground(tool)
            if len(chosen) >= max(1, limit):
                break
        return chosen

    def _route_ranked(self, request, cache_capability, ranked_candidates):
        """Shared executor for try_route/try_route_across: cache + inflight
        coalescing, then attempt the ranked candidates best-first with the
        per-tool budget/quota/retry/failover loop. `ranked_candidates` is a
        zero-arg callable evaluated inside the inflight guard (so candidate
        selection, which can issue embedding calls, is coalesced too).
        Returns the first usable ProviderResult, or None if nothing succeeds.
        """
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
            for tool in ranked_candidates():
                if not self._quota_remaining(tool, time.monotonic(), reserve=True):
                    increment(f"provider_{tool.provider}_quota_skips")
                    continue
                attempted.append(tool.name)
                started = time.monotonic()
                increment(f"provider_{tool.provider}_calls")
                effective_cost = float(self._effective(tool, "cost_usd"))
                increment("provider_estimated_cost_microusd", round(effective_cost * 1_000_000))
                if not charge_and_check(effective_cost):
                    # Stop trying further candidates the same way running out
                    # of them does -- falls through to the same None return
                    # every caller already handles (and the RuntimeError
                    # route() still raises on top of it).
                    increment("provider_budget_skips")
                    failures.append(f"{tool.name}: per-turn data budget reached")
                    break
                try:
                    output = self._invoke_with_retry(tool, request, effective_cost)
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
            return None
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
                    "retries": health.retries,
                    "latency_ewma_ms": round(health.latency_ewma_ms, 1) if health.latency_ewma_ms else None,
                    "overrides": dict(self._overrides.get(tool.name, {})),
                }
            )
        return rows

    def preview(self, request: str, capability: str, chains: tuple[str, ...] = ()) -> list[dict]:
        # Admin-dashboard inspection, not an active chat turn -- no call
        # budget exists to bound repeat embedding spend against, so the
        # semantic fallback is disabled here regardless of who calls this.
        catalog = {row["name"]: row for row in self.catalog()}
        candidates = self.candidates(request, capability, chains, allow_semantic_fallback=False)
        return [catalog[tool.name] for tool in candidates]
