"""The conversation's research objective, kept across turns.

The four-turn comparison with Minara (2026-09-24): "research projects with
two tokens like Venice VVV and DIEM", then "apart from staking how else can
we tie the dual token to the main token", then "how it works in akash
network", then "which other projects follow lock collateral -> mint a
tradable second token". Each of Orbit's web queries was the latest message
alone, so turn three explained what Akash is and turn four listed Lido and
bridges. The objective is one sentence the research tier keeps up to date
after every research turn, and every discovery search and synthesis reads
it with the current question.
"""
from __future__ import annotations

import asyncio
import logging

import dspy

from app.settings import settings

logger = logging.getLogger(__name__)

NONE = "none"
_MAX_CHARS = 320


class ResearchObjective(dspy.Signature):
    """State, in one sentence, the research objective this conversation is
    pursuing: what the user is trying to find out across turns, with the
    comparison, the mechanism and the constraints they have stated
    ("compare dual-token mechanisms to Venice's VVV/DIEM: the project's own
    token as collateral and a separately tradable second token"). Read the
    previous objective, the latest request and the answer's lead. Keep the
    objective when the request continues it, refine it when the request
    narrows or extends it (a new example, an excluded reading), and replace
    it when the request starts a different topic. Never restate the latest
    request alone; never add facts from the answer. Output exactly "none"
    when the conversation is not research: a control, a price check, a
    wallet read, chit-chat."""

    previous_objective: str = dspy.InputField(desc='"none" when there is none yet')
    request: str = dspy.InputField()
    answer_lead: str = dspy.InputField(desc="the first lines of the answer given")
    objective: str = dspy.OutputField(desc='one sentence, or "none"')


_program = dspy.Predict(ResearchObjective)


def enabled() -> bool:
    return bool(settings.research_objective_enabled)


def applies(intent: str | None, contract: dict | None, previous: str | None) -> bool:
    """Whether this turn can change the objective: a research turn, or any
    turn while an objective stands (it may end it)."""
    kind = (contract or {}).get("kind")
    if kind in ("open_research", "recent_events"):
        return True
    return bool(previous) and intent in ("research", "general")


async def update(previous: str | None, request: str, answer: str, *, intent: str | None = None, contract: dict | None = None) -> str | None:
    """The objective after this turn: the model's one sentence, or the
    previous one when the model is unavailable, or None when it says none."""
    if not enabled() or not applies(intent, contract, previous):
        return previous
    from app.clarify import is_clarification
    if is_clarification(answer or ""):
        return previous                  # a question back to the user changes nothing about what they are researching
    from app.nodes import runtime
    try:
        result = await asyncio.wait_for(
            runtime._call_research_lm(_program, previous_objective=previous or NONE, request=request or "", answer_lead=(answer or "")[:600]),
            timeout=20)
    except Exception:
        logger.info("research objective update skipped", exc_info=True)
        return previous
    text = (getattr(result, "objective", "") or "").strip().strip('"')
    if not text or text.lower().startswith(NONE):
        return None
    return text[:_MAX_CHARS]


def attach(resolved_request: str, session_context: dict | None) -> str:
    """The resolved request with the conversation's objective under it, for
    the router, the planner, the discovery search and the synthesis."""
    objective = ((session_context or {}).get("research_objective") or "").strip()
    if not enabled() or not objective or "Research objective of this conversation" in (resolved_request or ""):
        return resolved_request
    from app.routing.subject_probe import continues_subject, subject_of
    from app.context_entities import _THEME_PRONOUN
    ask = (resolved_request or "").splitlines()[0]
    if subject_of(ask) and not continues_subject(ask) and not _THEME_PRONOUN.search(ask):
        # A request with its own new subject and no reference back ("top 10
        # holders of musebook on robinhood" after an NBIS question) is not
        # in service of the standing objective; the update after the turn
        # replaces or ends it (live, 2026-09-24).
        return resolved_request
    return (f"{resolved_request}\nResearch objective of this conversation: {objective}. Answer the current question in service of that "
            "objective; when the question is ambiguous, the objective settles what it means.")
