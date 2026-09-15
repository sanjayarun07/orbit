"""Outcome-scored tools: what a tool's results actually did for answers.

ProviderRouter's health term only sees whether a call *errored*. This closes
the other half of the loop: after every chat turn, each tool that ran is
credited a success when its observation was usable -- the call did not fail
and the answer validator did not find the answer ungrounded in the retrieved
data -- and a miss otherwise. A smoothed success rate per tool feeds one term
of ProviderRouter._score, so a tool that keeps returning unusable data ranks
down on its own, with no matcher edit.

Smoothing is a Beta prior with mean `tool_outcome_prior_rate` and weight
`tool_outcome_prior_weight` observations: an unobserved tool sits exactly at
the prior and gets a zero adjustment (provisional), and only earns a real
adjustment as evidence accumulates (trusted). Rates are recomputed from the
last `tool_outcome_window_days` in Postgres; without Postgres the in-process
counters serve the same role for the process lifetime.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import date

from app.db import get_pg_pool
from app.settings import settings

logger = logging.getLogger(__name__)

_SKIP = {"semantic_cache", "finish"}


@dataclass
class _Counts:
    calls: int = 0
    successes: int = 0
    # Explicit user ratings on answers the tool contributed to (thumbs up /
    # down in the UI). Each counts as `tool_feedback_weight` virtual outcomes.
    feedback_up: int = 0
    feedback_down: int = 0


_counts: dict[str, _Counts] = {}
_lock = threading.Lock()


def _tool_calls(trajectory: dict) -> list[tuple[str, object]]:
    calls: list[tuple[str, object]] = []
    for key, value in trajectory.items():
        if key.startswith("tool_name_") and isinstance(value, str):
            index = key.rsplit("_", 1)[-1]
            calls.append((value, trajectory.get(f"observation_{index}")))
        elif isinstance(value, dict):
            calls.extend(_tool_calls(value))
    return calls


def _call_failed(observation: object) -> bool:
    return isinstance(observation, str) and ("failed:" in observation.lower() or "error" in observation[:120].lower())


def outcomes_from_turn(trajectory: object, validation: object) -> list[tuple[str, bool]]:
    """(tool_name, success) per tool call of one turn. A call succeeds when it
    did not fail and the answer's grounding check did not warn; a turn with no
    grounding check (no figures to trace) counts as success for a clean call."""
    if not isinstance(trajectory, dict):
        return []
    grounding_warned = False
    for check in getattr(validation, "checks", None) or []:
        if getattr(check, "name", None) == "grounding" and getattr(check, "status", None) == "warn":
            grounding_warned = True
    out: list[tuple[str, bool]] = []
    for name, observation in _tool_calls(trajectory):
        if name in _SKIP:
            continue
        out.append((name, not _call_failed(observation) and not grounding_warned))
    return out


def _rate(counts: _Counts) -> float:
    """Smoothed success rate: automatic outcomes plus user feedback, where one
    rating weighs `tool_feedback_weight` automatic outcomes (a person saying
    "this was wrong" is stronger evidence than a clean-looking call)."""
    k = max(0.0, settings.tool_outcome_prior_weight)
    w = max(0.0, settings.tool_feedback_weight)
    prior = settings.tool_outcome_prior_rate
    successes = counts.successes + w * counts.feedback_up
    calls = counts.calls + w * (counts.feedback_up + counts.feedback_down)
    return (successes + k * prior) / (calls + k) if (calls + k) > 0 else prior


def _observed(counts: _Counts) -> float:
    return counts.calls + max(0.0, settings.tool_feedback_weight) * (counts.feedback_up + counts.feedback_down)


def adjustment(tool_name: str) -> float:
    """Additive _score term: positive above the prior, negative below, zero
    for a tool with no evidence. Synchronous -- read by the router's ranker."""
    with _lock:
        counts = _counts.get(tool_name)
    if counts is None or _observed(counts) == 0:
        return 0.0
    return settings.provider_outcome_weight * (_rate(counts) - settings.tool_outcome_prior_rate)


def snapshot() -> list[dict]:
    with _lock:
        items = [(name, _Counts(c.calls, c.successes, c.feedback_up, c.feedback_down)) for name, c in _counts.items()]
    rows = []
    for name, counts in sorted(items, key=lambda item: item[0]):
        observed = _observed(counts)
        rows.append({
            "tool": name, "calls": counts.calls, "successes": counts.successes,
            "feedback_up": counts.feedback_up, "feedback_down": counts.feedback_down,
            "rate": round(_rate(counts), 3),
            "adjustment": round(settings.provider_outcome_weight * (_rate(counts) - settings.tool_outcome_prior_rate), 3) if observed else 0.0,
            "trusted": observed >= settings.tool_outcome_prior_weight,
        })
    return rows


def tools_in(trajectory: object) -> list[str]:
    """Distinct rated-able tool names in a turn's trajectory (public or full)."""
    if not isinstance(trajectory, dict):
        return []
    seen: list[str] = []
    for name, _ in _tool_calls(trajectory):
        if name not in _SKIP and name not in seen:
            seen.append(name)
    return seen


async def record_feedback(tool_names: list[str], up_delta: int, down_delta: int) -> None:
    """Apply a user rating (or its change/retraction) to every tool that
    contributed to the rated answer. Deltas can be negative; counters never
    go below zero. Persists to today's row when Postgres is configured."""
    names = [name for name in tool_names if name not in _SKIP]
    if not names or (up_delta == 0 and down_delta == 0):
        return
    with _lock:
        for name in names:
            counts = _counts.setdefault(name, _Counts())
            counts.feedback_up = max(0, counts.feedback_up + up_delta)
            counts.feedback_down = max(0, counts.feedback_down + down_delta)
    try:
        pool = await get_pg_pool()
    except Exception:
        pool = None
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            for name in names:
                await conn.execute(
                    """
                    INSERT INTO tool_outcomes (tool_name, day, calls, successes, feedback_up, feedback_down)
                    VALUES ($1, $2, 0, 0, GREATEST(0, $3), GREATEST(0, $4))
                    ON CONFLICT (tool_name, day) DO UPDATE
                    SET feedback_up = GREATEST(0, tool_outcomes.feedback_up + $3),
                        feedback_down = GREATEST(0, tool_outcomes.feedback_down + $4)
                    """,
                    name, date.today(), int(up_delta), int(down_delta),
                )
    except Exception:
        logger.warning("tool_outcomes: could not persist feedback", exc_info=True)


async def record_turn(trajectory: object, validation: object) -> None:
    """Fold one finished turn into the counters and, when Postgres is
    configured, into today's row per tool. Never raises into the chat path."""
    outcomes = outcomes_from_turn(trajectory, validation)
    if not outcomes:
        return
    with _lock:
        for name, success in outcomes:
            counts = _counts.setdefault(name, _Counts())
            counts.calls += 1
            counts.successes += int(success)
    try:
        pool = await get_pg_pool()
    except Exception:
        pool = None
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            for name, success in outcomes:
                await conn.execute(
                    """
                    INSERT INTO tool_outcomes (tool_name, day, calls, successes) VALUES ($1, $2, 1, $3)
                    ON CONFLICT (tool_name, day) DO UPDATE
                    SET calls = tool_outcomes.calls + 1, successes = tool_outcomes.successes + EXCLUDED.successes
                    """,
                    name, date.today(), int(success),
                )
    except Exception:
        logger.warning("tool_outcomes: could not persist turn outcomes", exc_info=True)


async def refresh() -> None:
    """Replace the in-process counters with the rolling window from Postgres."""
    try:
        pool = await get_pg_pool()
    except Exception:
        pool = None
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT tool_name, SUM(calls) AS calls, SUM(successes) AS successes, "
                "SUM(feedback_up) AS feedback_up, SUM(feedback_down) AS feedback_down FROM tool_outcomes "
                "WHERE day >= CURRENT_DATE - $1::int GROUP BY tool_name",
                int(settings.tool_outcome_window_days),
            )
    except Exception:
        logger.warning("tool_outcomes: refresh failed; keeping current rates", exc_info=True)
        return
    fresh = {row["tool_name"]: _Counts(int(row["calls"]), int(row["successes"]), int(row["feedback_up"] or 0), int(row["feedback_down"] or 0)) for row in rows}
    with _lock:
        _counts.clear()
        _counts.update(fresh)


async def refresh_worker() -> None:
    await refresh()
    while True:
        await asyncio.sleep(max(30, settings.tool_outcome_refresh_seconds))
        await refresh()
