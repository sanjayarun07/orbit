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

from app import streaming
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


_ADDRESS = re.compile(r"(?<![A-Za-z0-9])(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})(?![A-Za-z0-9])")
_OWN_SUBJECT = re.compile(r"\$[A-Za-z][A-Za-z0-9._-]{0,15}|\b[A-Z][A-Z0-9]{2,9}\b")
_CHAIN_WORD = re.compile(r"\b(solana|base|ethereum|arbitrum|optimism|polygon|bsc|bnb|avalanche)\b", re.I)


_NOT_A_SYMBOL = {"USD", "USDC", "USDT", "ETF", "NFT", "DEX", "CEX", "LP", "TVL", "APY", "APR", "ATH", "ATL", "AI", "OK", "UTC", "PDF", "CSV", "USA"}


def _symbol_in(text: str) -> str | None:
    for m in _OWN_SUBJECT.finditer(text or ""):
        word = m.group(0).lstrip("$")
        if m.group(0).startswith("$") or (len(word) >= 3 and word not in _NOT_A_SYMBOL):
            return word
    return None


_RESOLUTION_NOTE = re.compile(r"\n(?:Resolved (?:from|subject)[^\n]*)$", re.S)
_FORMAT_CLAUSE = re.compile(
    r"^\s*(?:(?:please\s+)?(?:mark|label|flag|cite|include|exclude|separate|distinguish|treat|note|state|say|show|list|link|report|present|format|"
    r"explain\s+(?:in|with|using)|keep|use|do\s+not|don'?t|never|avoid|if\s+.*?(?:say\s+so|say\s+unknown|say\s+that))\b[^.?!]*"
    r"\b(?:claims?|assumptions?|sources?|evidence|links?|timestamps?|coverage|gaps?|units?|as\s+unknown|say\s+so|unknown|plain\s+language|"
    r"do\s+not\s+(?:invent|prepare|execute|trade)|not\s+(?:invent|prepare|execute)|no\s+trade|without\s+predicting|database-only|primary\s+source)\b"
    r"|^\s*(?:do\s+not|don'?t|never)\s+[^.?!]*$|^\s*if\s+[^.?!]*\bsay\s+so\b[^.?!]*$)", re.I | re.S)


_CONSTRAINT = re.compile(r"\b(?:without|only|excluding|except|at\s+least|at\s+most|under|over|below|above|between|single[- ]asset|"
                         r"on\s+(?:solana|base|ethereum|bsc|bnb|arbitrum|polygon|avalanche|hyperliquid|aster))\b|\b(?-i:[A-Z][A-Z0-9]{2,9})\b", re.I)


def split_request(request: str) -> tuple[str, str | None]:
    """The user's words and the resolution note the session appended, apart:
    the note is context for every clause, never a clause (recheck of
    2910d5e8: it was dispatched as an ask of its own)."""
    m = _RESOLUTION_NOTE.search(request or "")
    if not m:
        return request or "", None
    return (request or "")[:m.start()], m.group(0).strip()


def is_format_clause(clause: str) -> bool:
    """An instruction about the answer, not an ask: "Mark database-only
    claims", "Label assumptions; do not invent valuation multiples", "If
    snapshots are unavailable, say so"."""
    return bool(_FORMAT_CLAUSE.match(clause or ""))


def plan_asks(request: str) -> tuple[list[str], str]:
    """The substantive clauses to dispatch, each carrying the message's
    subject and the first clause's constraints when it names none of its own,
    plus the note (resolution + format instructions) that every clause and
    the synthesis must see."""
    user_text, note = split_request(request)
    clauses = split_asks(user_text)
    if len(clauses) < 2:
        return [request], ""
    substantive = [c for c in clauses if not is_format_clause(c)] or clauses[:1]
    instructions = [c for c in clauses if c not in substantive]
    carried = carry_subject(substantive, user_text)
    lead = substantive[0]
    out = []
    lead_has_terms = bool(_ADDRESS.search(lead) or _symbol_in(lead) or _CONSTRAINT.search(lead))
    for original, clause in zip(substantive, carried):
        if lead_has_terms and clause == original and original != lead and not _ADDRESS.search(original) and not _symbol_in(original):
            clause = f"{original} (context: {lead})"                        # the lead's asset and constraints travel with the ask
        if note:
            clause = f"{clause}\n{note}"
        out.append(clause)
    extra = " ".join(instructions)
    return out, ("\n".join(x for x in (note, f"Instructions for the answer: {extra}" if extra else "") if x)).strip()


def carry_subject(clauses: list[str], request: str) -> list[str]:
    """Clauses that name no asset of their own get the message's subject, an
    address or a ticker: "Investigate the launch of token <mint>. Show
    deployer funding…" split into a second clause that asked for the contract
    again, and "What changed in ANSEM … ? Compare saved snapshots…" into
    clauses that asked which token (2026-09-23)."""
    chain = _CHAIN_WORD.search(request or "")
    on = f" on {chain.group(1).lower()}" if chain else ""
    m = _ADDRESS.search(request or "")
    if m:
        tail = f" (token {m.group(1)}{on})"
    else:
        symbol = _symbol_in(request)
        if not symbol:
            return clauses
        tail = f" ({symbol} token{on})"
    return [c if (_ADDRESS.search(c) or _symbol_in(c)) else c + tail for c in clauses]


_SYNTHESIS_HEAD = re.compile(r"^\*\*Taken together\*\*\n\n.*?\n\n---\n\n", re.S)


def strip_synthesis(answer: str) -> str:
    """The cards without an earlier "Taken together" block, so a second
    composition reads the cards and writes one summary, not two."""
    return _SYNTHESIS_HEAD.sub("", answer or "", count=1)


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
        answers.append(strip_synthesis(answer))          # one "Taken together" per answer: the one written after this
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
        stance = "a market read, not a recommendation" if advice else "a factual summary"
        if streaming.active():
            streaming.emit("status", text="Reading the cards together")
            result = await runtime.stream_synthesis(runtime.composite_synthesizer, "summary", lambda text: streaming.emit("delta", text=text),
                                                    request=request, evidence=cards, stance=stance)
        else:
            result = await runtime._call_synthesis_lm(runtime.composite_synthesizer, request=request, evidence=cards, stance=stance)
        summary = (getattr(result, "summary", "") or "").strip()
    except Exception:
        logger.warning("composite synthesis failed; cards only", exc_info=True)
        summary = ""
    if not summary:
        return cards
    summary = audit_guard(summary, cards)
    note = "\n\n_Taken together from the cards below. Not financial advice._" if advice else ""
    return f"**Taken together**\n\n{summary}{note}\n\n---\n\n{cards}"


_AUDIT_CLAIM = re.compile(r"\b(?:audited|audit(?:ed)?\s+by|has\s+(?:an|a)\s+audit|passed\s+(?:an|a|its)\s+audit|security\s+audit(?:s)?\s+(?:by|from|confirm))\b", re.I)
_AUDIT_EVIDENCE = re.compile(r"\b(?:audit report|audited by|audit(?:ed)?\s+(?:by|from)\s+(?:certik|ottersec|zellic|halborn|trail of bits|hacken|peckshield|slowmist|quantstamp|sec3|neodyme|kudelski|cyberscope|hashex|solidproof)"
                             r"|(?:certik|ottersec|zellic|halborn|trail of bits|hacken|peckshield|slowmist|quantstamp|sec3|neodyme)\b[^\n]{0,80}\baudit)\b", re.I)


def audit_guard(summary: str, cards: str) -> str:
    """Never let a summary call a token audited on the strength of a
    verification or Shield check. Live (2026-09-18): "Audit report on ANSEM"
    and "Is RENDER audited?" were summarised as audited by Jupiter while the
    card itself said those checks are not an audit. When the summary claims
    an audit and no card names an audit report or auditing firm, the claim
    is corrected in front of it rather than left to stand."""
    if not _AUDIT_CLAIM.search(summary or "") or _AUDIT_EVIDENCE.search(cards or ""):
        return summary
    return ("**No audit report was found in the evidence.** Jupiter verification, Shield warnings and organic-score checks "
            "are listing and safety signals, not a security audit; treat any mention of an audit below as unsupported.\n\n" + summary)


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
                    if result.output:
                        streaming.emit("card", markdown=result.output, tool=result.tool)
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
