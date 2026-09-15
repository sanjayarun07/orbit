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
    k = max(0.0, settings.tool_outcome_prior_weight)
    prior = settings.tool_outcome_prior_rate
    return (counts.successes + k * prior) / (counts.calls + k) if (counts.calls + k) > 0 else prior


def adjustment(tool_name: str) -> float:
    """Additive _score term: positive above the prior, negative below, zero
    for a tool with no evidence. Synchronous -- read by the router's ranker."""
    with _lock:
        counts = _counts.get(tool_name)
    if counts is None or counts.calls == 0:
        return 0.0
    return settings.provider_outcome_weight * (_rate(counts) - settings.tool_outcome_prior_rate)


def snapshot() -> list[dict]:
    with _lock:
        items = [(name, _Counts(c.calls, c.successes)) for name, c in _counts.items()]
    rows = []
    for name, counts in sorted(items, key=lambda item: item[0]):
        rows.append({
            "tool": name, "calls": counts.calls, "successes": counts.successes,
            "rate": round(_rate(counts), 3),
            "adjustment": round(settings.provider_outcome_weight * (_rate(counts) - settings.tool_outcome_prior_rate), 3) if counts.calls else 0.0,
            "trusted": counts.calls >= settings.tool_outcome_prior_weight,
        })
    return rows


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
                "SELECT tool_name, SUM(calls) AS calls, SUM(successes) AS successes FROM tool_outcomes "
                "WHERE day >= CURRENT_DATE - $1::int GROUP BY tool_name",
                int(settings.tool_outcome_window_days),
            )
    except Exception:
        logger.warning("tool_outcomes: refresh failed; keeping current rates", exc_info=True)
        return
    fresh = {row["tool_name"]: _Counts(int(row["calls"]), int(row["successes"])) for row in rows}
    with _lock:
        _counts.clear()
        _counts.update(fresh)


async def refresh_worker() -> None:
    await refresh()
    while True:
        await asyncio.sleep(max(30, settings.tool_outcome_refresh_seconds))
        await refresh()
