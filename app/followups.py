"""Related questions under an answer -- shown only when they are about
THIS answer.

The chips this replaces were removed on 2026-09-14 for being generic ("View
Portfolio" under anything). The user's rule now (2026-09-18, looking at
Perplexity's follow-ups under "OPEN token unlock schedule"): if we can offer
follow-ups as relevant as those, show them; if not, show none. So a small
model writes candidates from the question and the answer, and a grounding
gate keeps only the ones that name something the answer actually said. Fewer
than two survivors means no section at all. Never a reason for a turn to
fail or wait: bounded, best effort, off for trade turns and clarifications.
"""
from __future__ import annotations

import asyncio
import logging
import re

import dspy

from app.clarify import is_clarification
from app.settings import settings

logger = logging.getLogger(__name__)

MAX_FOLLOWUPS = 5
MIN_FOLLOWUPS = 2
MIN_ANSWER_CHARS = 240
TIMEOUT_SECONDS = 6.0

_PRONOUN_START = re.compile(r"^\s*(?:what|how|why|when|where|which|who|is|are|does|do|can|should|will)\b.*\b(?:it|its|this|that|they|them|these|those)\b", re.I)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9.\-']{1,}")
_NUMBER = re.compile(r"\d[\d,.]*[kmb%]?", re.I)
# Questions Orbit cannot answer with data (predictions, effects, advice) and
# questions aimed at the user rather than at Orbit ("Do you want ...?").
_SPECULATIVE = re.compile(r"\b(?:impact|affect|effect|influence|sustainable|might|could|would|likely|predict|forecast|outlook|mitigate|sell pressure|"
                          r"should (?:i|you|one)|is it (?:a good|worth)|price (?:reaction|target)|how (?:will|would|might))\b", re.I)
_TO_THE_USER = re.compile(r"^\s*(?:do|would|are|did|can|could|will|have|should) you\b", re.I)
_GENERIC = {"crypto", "token", "tokens", "market", "markets", "price", "prices", "wallet", "portfolio", "trending", "trade",
            "buy", "sell", "risk", "risks", "chain", "chains", "the", "and", "for", "with", "about", "what", "how", "does",
            "next", "current", "latest", "now", "today", "coin", "coins", "project", "protocol", "supply", "unlock", "unlocks",
            "vesting", "schedule", "you", "your", "this", "that", "are", "is", "of", "in", "on", "to", "a", "an", "it", "its"}


class RelatedQuestions(dspy.Signature):
    """Write 3-5 follow-up questions a reader of THIS answer would ask next,
    one per line, no numbering. Each must be specific to the answer: name the
    token, project, wallet, person or figure the answer discussed (never "it"
    or "this token"), and ask about something the answer mentioned or left
    open -- a date it gave, an allocation it listed, a figure it cited, the
    next event it implied. Only FACTUAL questions answerable with data or
    research (a price, a holder count, an unlock date, a supply figure, an
    audit, a news event): never predictions ("what impact could"), never
    advice ("should I"), never questions to the user ("do you want"). Do not
    repeat the user's question. Output nothing else."""

    question: str = dspy.InputField(desc="What the user asked")
    answer: str = dspy.InputField(desc="The answer they just read (may be truncated)")
    followups: str = dspy.OutputField(desc="One question per line")


related_writer = dspy.Predict(RelatedQuestions)


def _terms(text: str) -> set[str]:
    return {w.lower().strip(".'-") for w in _WORD.findall(text or "")}


def _anchors(followup: str) -> list[str]:
    """The words in a follow-up that tie it to a subject: capitalised words,
    tickers, dotted domains, and numbers. Generic market words don't count."""
    out = []
    for word in _WORD.findall(followup):
        low = word.lower().strip(".'-")
        if low in _GENERIC or len(low) < 2:
            continue
        if word[0].isupper() or word.isupper() or "." in word.strip("."):
            out.append(low)
    out += [n.lower() for n in _NUMBER.findall(followup) if len(n) >= 2]
    return out


def grounded(followup: str, question: str, answer: str) -> bool:
    """A follow-up is grounded when at least one of its anchors appears in the
    answer or the question, and it does not lean on a pronoun for its subject."""
    text = followup.strip()
    if not text or len(text) < 12 or len(text) > 160:
        return False
    if _SPECULATIVE.search(text) or _TO_THE_USER.match(text):
        return False
    if _PRONOUN_START.search(text) and not any(a in _terms(answer) | _terms(question) for a in _anchors(text)):
        return False
    haystack = _terms(answer) | _terms(question) | {n.lower() for n in _NUMBER.findall(answer)}
    anchors = _anchors(text)
    if not anchors:
        return False
    return any(a in haystack for a in anchors)


def _normalise(line: str) -> str:
    line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip('"')
    return line if not line or line.endswith("?") else line + "?"


def filter_followups(raw: str, question: str, answer: str) -> list[str]:
    """The lines of a model's draft that survive the grounding gate, deduped
    against each other and the user's question; [] unless at least
    MIN_FOLLOWUPS do."""
    seen: set[str] = set()
    keep: list[str] = []
    q_key = re.sub(r"\W+", " ", (question or "").lower()).strip()
    for line in (raw or "").splitlines():
        text = _normalise(line)
        key = re.sub(r"\W+", " ", text.lower()).strip()
        if not text or key in seen or key == q_key or not grounded(text, question, answer):
            continue
        seen.add(key)
        keep.append(text)
        if len(keep) >= MAX_FOLLOWUPS:
            break
    return keep if len(keep) >= MIN_FOLLOWUPS else []


def eligible(intent: str | None, answer: str | None, trade_plan) -> bool:
    """Which turns get related questions: research and general answers with
    substance. Not trade turns (the card is the next step), not clarifying
    questions (the user's reply is), not errors or refusals."""
    if not settings.followups_enabled or trade_plan is not None:
        return False
    if intent not in ("research", "general"):
        return False
    text = (answer or "").strip()
    if len(text) < MIN_ANSWER_CHARS or is_clarification(text):
        return False
    lowered = text.lower()
    return not any(marker in lowered for marker in ("could not", "couldn't reach", "try again", "is not configured", "i don't want to guess"))


async def generate(question: str, answer: str, intent: str | None, trade_plan=None) -> list[str]:
    """Related questions for this answer, or [] -- bounded and never raising."""
    if not eligible(intent, answer, trade_plan):
        return []
    from app.nodes import runtime

    try:
        result = await asyncio.wait_for(runtime._call_lm(related_writer, question=question[:600], answer=answer[:6000]), timeout=TIMEOUT_SECONDS)
    except Exception:
        logger.info("related questions skipped", exc_info=True)
        return []
    return filter_followups(getattr(result, "followups", "") or "", question, answer)
