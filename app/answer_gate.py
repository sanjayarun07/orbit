"""The answer is checked against the question before the user sees it.

User rule (2026-09-18): "1000 ways of asking the same question ... even if
we have to degrade performance, the result should be correct for the
query. It is always better than a wrong answer." Every phrasing fix so far
patched the way in; this gate guards the way out. After a research turn
has its answer, a model judges one thing: is this answer about the subject
the user named, and does it give what they asked for? An answer that is
about something else, or that lacks the asked-for information, does not
ship. The turn goes to the web for the question as asked and is checked
again; if that fails too, the user gets a question naming what could not
be found -- never a confident answer about the wrong thing.

Costs one small-model call per research answer (about a second) and a web
search on a miss. That is the trade the user asked for.
"""
from __future__ import annotations

import asyncio
import logging
import re

import dspy

from app import streaming
from app.clarify import is_clarification, is_market_text
from app.settings import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 12.0
MAX_ANSWER_CHARS = 7000


class AnswerCheck(dspy.Signature):
    """Judge whether an answer addresses the user's question. Two tests only:
    (1) SUBJECT -- is the answer about the same thing the user named (the
    token, protocol, company, wallet or market they asked about)? An answer
    about a namesake, a different asset, a different sense of the word, or a
    generic calendar or overview when one asset was named, fails.
    (2) COVERAGE -- does the answer give the information the question asked
    for (price, holders, unlock dates, investors, safety, news ...)? An
    answer that says the data or passages do not include it, or that gives
    other facts about the right subject, fails.
    "Top holders" or "largest wallets" asks for a ranked list of wallets with
    their shares; an aggregate ("smart money holds 7-17M") without any wallet
    is "missing". "Whale activity" asks for specific large transfers or
    positions, not a market summary.
    Orbit is a crypto-and-markets assistant: when the user's word has a
    token, protocol, company or market reading, an answer whose main subject
    is something else (a medicine, a civic organisation, a planet, a
    dictionary sense) fails (1) even if it mentions the market reading
    in passing.
    verdict: "answers" when both pass; "wrong_subject" when (1) fails;
    "missing" when only (2) fails; "asks" when the answer is itself a
    question to the user. Be strict about the subject and lenient about
    detail: partial figures with sources are "answers"."""

    question: str = dspy.InputField()
    answer: str = dspy.InputField(desc="The answer as the user would read it (may be truncated)")
    verdict: str = dspy.OutputField(desc="answers | wrong_subject | missing | asks")
    subject: str = dspy.OutputField(desc="What the answer is actually about, in a few words")
    missing: str = dspy.OutputField(desc="What the question asked for that the answer lacks, as a SHORT NOUN PHRASE naming the thing ('the position's open time', 'the token's USD value'); never a sentence; empty when nothing")


answer_checker = dspy.Predict(AnswerCheck)

_VERDICTS = {"answers", "wrong_subject", "missing", "asks"}


def parse_verdict(value: str | None) -> str:
    text = (value or "").strip().lower().strip('"\'.` ')
    for v in _VERDICTS:
        if text.startswith(v):
            return v
    return "answers"       # an unreadable verdict never blocks an answer


def eligible(answer: str | None, result: dict) -> bool:
    """Which answers get checked: real research answers. Not questions to
    the user, not trade cards, not errors, not tiny replies."""
    if not settings.answer_gate_enabled:
        return False
    if result.get("pipeline") in ("contract", "research_loop"):
        # These paths already proved or explicitly withheld their claims.
        # A topical web fallback must not turn an abstention into prose.
        return False
    text = (answer or "").strip()
    if len(text) < 80 or is_clarification(text) or result.get("pending_token") or result.get("trade_plan"):
        return False
    return not text.lower().startswith(("could not", "i couldn't reach", "something went wrong"))


async def check(question: str, answer: str) -> dict:
    """{"verdict", "subject", "missing"}; "answers" on any failure to judge."""
    from app.nodes import runtime

    try:
        streaming.emit("status", text="Checking the answer against the question")
        result = await asyncio.wait_for(runtime._call_lm(answer_checker, question=question[:600], answer=answer[:MAX_ANSWER_CHARS]), timeout=TIMEOUT_SECONDS)
    except Exception:
        logger.info("answer check skipped", exc_info=True)
        return {"verdict": "answers", "subject": "", "missing": ""}
    return {"verdict": parse_verdict(getattr(result, "verdict", "")), "subject": (getattr(result, "subject", "") or "").strip(),
            "missing": (getattr(result, "missing", "") or "").strip()}


async def _web_answer(question: str) -> str | None:
    """The question as asked, to the web -- the same search the router uses
    when it is unsure."""
    from app.routing import subject_probe

    try:
        # Scoped the way the research node scopes a web tool: the gate's
        # fallback answered "Compare OPEN and MOVE" with two Nasdaq stocks.
        found = await asyncio.to_thread(subject_probe.context_search, subject_probe.market_scoped(question))
    except Exception:
        logger.info("web answer for the gate failed", exc_info=True)
        return None
    return found if found and is_market_text(found) and not is_clarification(found) else None


_LACKS = re.compile(r"^\s*(?:the\s+)?(?:answer|data|response|passages?|card)\s+(?:lacks?|does not (?:include|state|mention|contain|provide|specify)|doesn't (?:include|state|mention|contain|provide|specify))\s+", re.I)
_ADDRESS = re.compile(r"\b(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})\b")


def _missing_phrase(missing: str) -> str:
    """The judge's `missing` as a noun phrase that reads inside a sentence.
    It sometimes answers with a whole sentence ("The answer lacks the date
    or time when the wallet opened the ONDO long position."), which used to
    be dropped into the middle of one (2026-09-18)."""
    text = " ".join((missing or "").split()).rstrip(".")
    text = _LACKS.sub("", text)
    if text.startswith("The "):          # never lowercase a name: "Mercury the token" stays capitalised
        text = "the " + text[4:]
    return text or "what you asked for"


_TICKER = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b|\b([A-Z]{2,10})\b")
_FORECAST = re.compile(r"\b(?:will\s+\S+\s+(?:go|be)\s+up|going\s+(?:up|down)|what\s+will\s+happen|next\s+\d+\s*(?:-\s*\d+\s*)?(?:hours?|hrs?|days?|weeks?)|"
                       r"predict(?:ion)?|forecast|price\s+target|where\s+(?:is|will)\s+\S+\s+(?:be|go))\b", re.I)


def _could_not_find(question: str, verdict: dict) -> str:
    """The honest close when neither the tools nor the web answered: what is
    missing, what we did find, and the one next step that fits the question.
    A question that already names an address is not answered by asking the
    user to name a token."""
    missing = _missing_phrase(verdict.get("missing") or "")
    subject = (verdict.get("subject") or "").strip()
    # Echo what we did find only when it says something the question didn't:
    # "about <the question, restated>" is noise.
    q_words = {w for w in re.findall(r"[a-z0-9]+", (question or "").lower()) if len(w) > 2}
    s_words = {w for w in re.findall(r"[a-z0-9]+", subject.lower()) if len(w) > 2}
    restates = bool(s_words) and len(s_words - q_words) / len(s_words) < 0.34
    found = f" What I could pull is about {subject}." if subject and not restates else ""
    address = _ADDRESS.search(question or "")
    ticker = _TICKER.search(question or "")
    if address:
        short = f"{address.group(1)[:6]}…{address.group(1)[-4:]}"
        step = (f" My sources for `{short}` return its current state -- balances, open positions, recent transfers -- not its history, "
                "so ask about what it holds now, or paste a transaction hash and I'll read that.")
    elif _FORECAST.search(question or ""):
        # "What will BTC do in the next 5 hours" names the asset; asking the
        # user to name it is wrong (live, 2026-09-21). Say what exists instead.
        step = (" Nobody's data says where a price goes in the next few hours, and I won't guess. What I can pull is the tape right now: "
                "price and 24h move, funding and open interest, liquidations, key levels, and what X is saying -- ask for those.")
    elif ticker:
        step = (f" For {ticker.group(1) or ticker.group(2)} I can pull price and market structure, holders, security, unlocks, news, "
                "or X sentiment -- ask for one of those and I'll look again.")
    else:
        step = (" Name the token, protocol or company precisely (a $ticker, the full name, or a contract address) and say what you "
                "want to know, and I'll look again.")
    return f"I couldn't find {missing}.{found}{step}"


async def gate(question: str, result: dict) -> dict:
    """The research result, or a replacement when its answer failed the
    check: the web's answer to the question (checked once more), else a
    question to the user. Never raises."""
    answer = result.get("answer") or ""
    if not eligible(answer, result):
        return result
    verdict = await check(question, answer)
    if verdict["verdict"] == "asks" and not is_clarification(answer):
        # The judge called a dictionary entry a question (live: "What is
        # OPEN?"); an answer that does not ask the user is judged on subject.
        verdict = {**verdict, "verdict": "wrong_subject"}
    if verdict["verdict"] in ("answers", "asks"):
        return result
    logger.info("answer gate: %s (subject=%r missing=%r) for %r", verdict["verdict"], verdict["subject"], verdict["missing"], question[:80])
    streaming.emit("status", text="Running perplexity web search")
    from_web = await _web_answer(question)
    if from_web:
        second = await check(question, from_web)
        if second["verdict"] in ("answers", "asks"):
            note = ("_The tools' data was about something else, so this comes from the web._" if verdict["verdict"] == "wrong_subject"
                    else "_The tools' data did not cover this, so this comes from the web._")
            kept = "" if verdict["verdict"] == "wrong_subject" else f"\n\n---\n\n{answer}"
            trajectory = {"thought_0": f"The first answer failed the check ({verdict['verdict']}); the web answered the question as asked.",
                          "tool_name_0": "perplexity_context_search", "tool_args_0": {"query": question}, "observation_0": from_web}
            return {**result, "answer": f"{from_web.strip()}\n\n{note}{kept}", "trajectory": trajectory, "answer_gate": {**verdict, "resolved_by": "web"}}
    if verdict["verdict"] == "missing" and answer.strip():
        # The subject was right and part of the question was answered: keep
        # what the tools found and say what they did not cover, rather than
        # replace real data with a refusal (live: a BTC tape that had price
        # and 24h change lost them because funding was missing, 2026-09-22).
        gap = _missing_phrase(verdict.get("missing") or "")
        return {**result, "answer": f"{answer.strip()}\n\n_Not covered by the sources this turn: {gap}._",
                "answer_gate": {**verdict, "resolved_by": "partial"}}
    return {**result, "answer": _could_not_find(question, verdict), "trajectory": None, "answer_gate": {**verdict, "resolved_by": "ask"}}
