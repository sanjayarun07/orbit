"""Combining several tools into one answer.

Two shapes of request need more than one tool, and both used to get one:

- A COMPOUND message: "check trending tokens. check whale activity" -- two
  asks in one message. The first ask's card came back and the second was
  silently dropped. `split_asks` breaks such a message at sentence boundaries
  (and "also"/"then"), each clause is routed on its own through the same
  deterministic path, and `combine` joins the cards with their trajectories
  merged. A clause that cannot be answered without more information (whale
  activity of what?) contributes its clarifying question, not silence.

- A single ask that spans several tools: "what should I day-trade today
  based on technicals, news and sentiment" is movers + volume + sentiment +
  news. `compose_market_advice` invokes those tools by name and builds one
  evidence bundle.

In both cases `synthesize` writes a short reading over the combined evidence
through the synthesis tier, and the cards stay verbatim underneath it: the
model summarises, it never rewrites a table, so no figure can be lost or
invented in the summary's name.
"""
from __future__ import annotations

import asyncio
import logging
import re

from app.nodes import runtime
from app.provider_registry import get_provider_router

logger = logging.getLogger(__name__)

# Sentence boundaries and the connectors people use to append a second ask.
_CLAUSE_SPLIT = re.compile(r"(?<=[.;?!])\s+|\s+(?:and\s+also|also|then|plus)\s+(?=\w)", re.I)
_NOISE = re.compile(r"^(?:and|also|then|plus|please|pls|thanks?|ok|okay)\b\s*|\s*\b(?:and let me know|let me know|please|pls|thanks?)\s*$", re.I)

MARKET_ADVICE = re.compile(
    r"\b(?:what|which)\s+(?:crypto|coins?|tokens?|alts?)\s+(?:should|to|can)\s+(?:i\s+)?(?:buy|trade|long|short)\b"
    r"|\bsuggest\b[^.?!\n]{0,40}\b(?:crypto|coins?|tokens?|alts?)\b"
    r"|\bday[\s-]?trad\w*\b|\bswing[\s-]?trad\w*\b"
    r"|\bbest\s+(?:crypto|coins?|tokens?|alts?)\s+to\s+(?:buy|trade)\b"
    r"|\bwhat\s+(?:should|to)\s+(?:i\s+)?(?:buy|trade)\s+(?:today|now|right now|this week)\b",
    re.I,
)


def split_asks(request: str) -> list[str]:
    """The distinct asks in a message, or [request] when there is one. Splits
    only at sentence boundaries and explicit connectors, never at a bare
    "and" ("price and volume of BONK" is one ask)."""
    parts = [_NOISE.sub("", p.strip(" ,.;!?")).strip(" ,.;!?") for p in _CLAUSE_SPLIT.split(request or "")]
    parts = [p for p in parts if len(p.split()) >= 2]
    return parts if len(parts) >= 2 else [request]


def _shift(trajectory: dict, by: int) -> dict:
    out = {}
    for key, value in trajectory.items():
        m = re.fullmatch(r"(.+?)_(\d+)", key)
        out[f"{m.group(1)}_{int(m.group(2)) + by}" if m else key] = value
    return out


def combine(parts: list[tuple[str, dict]]) -> tuple[str, dict]:
    """(answer, trajectory) from per-clause (answer, trajectory) pairs."""
    answers, trajectory, offset = [], {}, 0
    for answer, traj in parts:
        answers.append(answer)
        traj = traj or {}
        steps = {int(m.group(1)) for k in traj for m in [re.fullmatch(r"tool_name_(\d+)", k)] if m}
        trajectory.update(_shift(traj, offset))
        offset += (max(steps) + 1) if steps else 0
    return "\n\n---\n\n".join(a for a in answers if a), trajectory


async def synthesize(request: str, cards: str, trajectory: dict, advice: bool = False) -> str:
    """A short reading over the combined evidence, above the verbatim cards."""
    if not cards.strip():
        return cards
    try:
        result = await runtime._call_synthesis_lm(runtime.composite_synthesizer, request=request, evidence=cards,
                                                  stance="a market read, not a recommendation" if advice else "a factual summary")
        summary = (getattr(result, "summary", "") or "").strip()
    except Exception:
        logger.warning("composite synthesis failed; cards only", exc_info=True)
        summary = ""
    if not summary:
        return cards
    note = "\n\n_Taken together from the cards below. Not financial advice._" if advice else ""
    return f"**Taken together**\n\n{summary}{note}\n\n---\n\n{cards}"


_ADVICE_PROBES = (
    ("coingecko_gainers_losers", "top gainers today"),
    ("coingecko_gainers_losers", "biggest losers today"),
    ("coingecko_top_volume", "most traded tokens today"),
    ("market_sentiment_snapshot", "fear and greed today"),
    ("perplexity_finance_search", "crypto market news today: what is moving prices and why"),
)


async def compose_market_advice(request: str) -> tuple[str, dict]:
    """Movers, volume, sentiment and news, by name; failures are counted, not
    fatal, so one provider being down still leaves a bundle."""
    router = get_provider_router()

    async def one(tool: str, probe: str):
        for attempt in (1, 2):   # a second, sequential try after the concurrent burst
            try:
                result = await asyncio.to_thread(router.invoke, tool, probe, ())
                if result is not None:
                    return tool, result
            except Exception:
                logger.warning("composition: %s failed (attempt %d)", tool, attempt, exc_info=True)
        return tool, None

    results = await asyncio.gather(*(one(t, p) for t, p in _ADVICE_PROBES))
    sections, trajectory, index = [], {}, 0
    for tool, result in results:
        if result is None or not result.output:
            continue
        sections.append(result.output)
        trajectory[f"tool_name_{index}"] = result.tool
        trajectory[f"observation_{index}"] = result.output
        index += 1
    if not sections:
        return "", {}
    return "\n\n---\n\n".join(sections), trajectory
