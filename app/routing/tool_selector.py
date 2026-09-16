"""Model arbitration among already-qualified provider tools.

Not the same job as tool_semantic.py's embedding fallback. That module
widens *reachability*: a tool whose regex gate missed but whose description
is a close semantic match gets one more chance to even become a candidate.
This module narrows *choice*: once several tools have ALREADY passed their
own regex gate plus the chain/health/quota filters -- so every option shown
here is independently legitimate -- it reads each one's app.tool_catalog
spec (the API it calls, what it returns, what it can never answer) and picks
the single best fit for the request's actual wording. It is the answer to
"the same keywords match several tools; which one's API actually serves
this question" -- exactly the failure mode a fixed priority number or a
keyword count cannot resolve, because a paid-boosts list and a volume-
ranked markets endpoint share most of their vocabulary and neither wins a
priority fight the right way for every phrasing.

Purely a REORDER. The output is always a permutation of the input list; a
tool this module didn't put first is still available right behind it, so a
bad model pick costs the router one extra failed attempt, never a wrong
answer that silently succeeds. Any failure -- disabled, too few candidates,
timeout, malformed output, a name outside the shown set -- returns the
input order unchanged.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

import dspy

from app.call_budget import charge_and_check
from app.metrics import increment
from app.settings import settings

if TYPE_CHECKING:
    from app.provider_router import ProviderTool

logger = logging.getLogger(__name__)


class ToolArbitration(dspy.Signature):
    """Choose the ONE listed tool whose API actually answers the request.

    Every tool shown already independently qualified (its own matcher fired,
    it supports the request's chain, it is healthy and within quota) -- your
    only job is choosing which of THESE, by what its API returns, fits the
    request best. Read each tool's "answers" (what it can serve) and "never
    for" (what it must not be used for) carefully: two tools can share every
    keyword in the request and still answer completely different questions
    -- a paid promotion list is not a volume ranking, a price-change ranking
    is not a volume ranking, a single token's trading pair is not a market-
    wide ranking, a wallet's balances are not its transaction history. When
    the request names no clear winner among the listed options, choose the
    first-listed one (it is already the deterministic best guess) rather
    than guessing a tie apart. Never choose a tool not listed below.
    """

    request: str = dspy.InputField()
    candidates: str = dspy.InputField(desc="numbered tools, each with what its API answers and never answers")
    chosen_tool: str = dspy.OutputField(desc="the exact name of one listed tool")
    reason: str = dspy.OutputField(desc="one short clause: what in the request pointed to this tool's API")


_arbiter = dspy.Predict(ToolArbitration)


def _format_candidates(tools: tuple["ProviderTool", ...]) -> str:
    lines = []
    for i, tool in enumerate(tools, 1):
        spec = getattr(tool, "spec", None)
        if spec is not None:
            bits = [spec.summary or tool.description or ""]
            if spec.answers:
                bits.append("answers: " + "; ".join(spec.answers[:3]))
            if spec.not_answers:
                bits.append("never for: " + "; ".join(spec.not_answers[:3]))
            elif spec.not_for:
                bits.append("never for: " + ", ".join(sorted(spec.not_for)))
            detail = " -- ".join(b for b in bits if b)
        else:
            detail = tool.description or "(no further description)"
        lines.append(f"{i}. {tool.name}: {detail}")
    return "\n".join(lines)


class ToolSelector:
    """Sync (mirrors ToolEmbeddingRouter's shape -- this codebase's proven
    pattern for a model-assisted routing fallback): its own bounded LM, a
    small LRU cache keyed on the exact candidate set, and a circuit breaker
    so a wedged provider degrades to a no-op instead of stalling every turn
    that reaches an ambiguous capability."""

    def __init__(self) -> None:
        self._lm: dspy.LM | None = None
        self._lm_model: str | None = None
        self._lock = threading.Lock()
        self._cache: "OrderedDict[tuple, tuple[str, str]]" = OrderedDict()
        self._retry_at = 0.0
        self._consecutive_failures = 0

    def _model_name(self) -> str:
        return settings.tool_selector_model or settings.intent_model or settings.model

    def _lm_for(self, model: str) -> dspy.LM:
        if self._lm is None or self._lm_model != model:
            kwargs: dict = {}
            if settings.tool_selector_api_base:
                kwargs["api_base"] = settings.tool_selector_api_base
            if settings.tool_selector_api_key:
                kwargs["api_key"] = settings.tool_selector_api_key
            self._lm = dspy.LM(model, timeout=settings.tool_selector_timeout_seconds, num_retries=1, **kwargs)
            self._lm_model = model
        return self._lm

    def select(self, request: str, ranked: list["ProviderTool"], chains: tuple[str, ...]) -> list["ProviderTool"]:
        if not settings.llm_tool_selection_enabled or len(ranked) < max(2, settings.tool_selector_min_candidates):
            return ranked
        shown = tuple(ranked[: max(1, settings.tool_selector_max_candidates)])
        names = tuple(t.name for t in shown)
        key = (names, " ".join(request.lower().split()))

        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                chosen_name, reason = cached
            else:
                chosen_name = None
        if chosen_name is None:
            if time.monotonic() < self._retry_at:
                increment("provider_tool_llm_selection_circuit_open")
                return ranked
            if not charge_and_check(settings.tool_selector_cost_usd):
                return ranked
            try:
                started = time.monotonic()
                with dspy.context(lm=self._lm_for(self._model_name())):
                    result = _arbiter(request=request, candidates=_format_candidates(shown))
                chosen_name, reason = result.chosen_tool.strip(), (result.reason or "").strip()
                self._consecutive_failures = 0
                increment("provider_tool_llm_selection_calls")
                logger.debug("tool_selector: %.0fms -> %s (%s)", (time.monotonic() - started) * 1000, chosen_name, reason)
            except Exception:
                self._consecutive_failures += 1
                increment("provider_tool_llm_selection_errors")
                logger.warning("tool_selector call failed", exc_info=True)
                if self._consecutive_failures >= 3:
                    self._retry_at = time.monotonic() + 30.0
                    self._consecutive_failures = 0
                return ranked
            with self._lock:
                self._cache[key] = (chosen_name, reason)
                while len(self._cache) > max(1, settings.tool_selector_cache_entries):
                    self._cache.popitem(last=False)

        if chosen_name not in names:
            increment("provider_tool_llm_selection_invalid")
            return ranked
        if chosen_name == names[0]:
            increment("provider_tool_llm_selection_agreed")
            return ranked
        increment("provider_tool_llm_selection_overrode")
        logger.info("tool_selector: overrode %s -> %s for %r (%s)", names[0], chosen_name, request[:120], reason)
        chosen = next(t for t in ranked if t.name == chosen_name)
        return [chosen] + [t for t in ranked if t.name != chosen_name]


_selector = ToolSelector()


def select_tool(request: str, ranked: list["ProviderTool"], chains: tuple[str, ...] = ()) -> list["ProviderTool"]:
    """Public entry point used by ProviderRouter.candidates/_ranked_union and
    by the offline routing-eval harness. A pure reorder of `ranked`; see the
    module docstring for the safety property that makes this a no-risk add."""
    return _selector.select(request, ranked, chains)
