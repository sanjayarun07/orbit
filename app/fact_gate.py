"""The fact gate: an answer ships only when the facts satisfy the contract.

Two questions, asked in order. Do the facts cover what the contract
requires (enough ranking rows for the venue and window, enough holder rows,
a dated event inside the freshness window, yields with the asked exposure)?
And does the prose only state figures that exist in those facts? A missing
requirement becomes a stated gap or a precise question, never web prose; an
untraceable figure is named as such. The numeric validator
(app/answer_validator.py) stays advisory for every other answer; here the
contract makes the check decidable.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field

from app.contracts import QuestionContract
from app.facts import Fact, event_date_of

_MIN_ROWS = {"ranking_row": 3, "holder_row": 5, "yield_row": 1, "event": 1, "source": 1, "holding_row": 1, "quote_row": 1}
_DEFAULT_REQUIRED = {"market_ranking": ["ranking_row"], "holders": ["holder_row"], "yields": ["yield_row"], "recent_events": ["event"], "open_research": ["source"],
                     "portfolio": ["holding_row"], "transaction_intent": ["quote_row"]}
_AHEAD_SLACK = timedelta(hours=36)     # a date one calendar day ahead can be today in another timezone; October is not "recent" in September
_FIGURE = re.compile(r"(?<![\w.])[+\-]?\$?\d[\d,]*(?:\.\d+)?\s*(?:%|[kKmMbB]\b|[xX]\b|(?:thousand|million|billion|mn|bn|trillion)\b)?", re.I)
_WORD_MULT = {"thousand": 1e3, "million": 1e6, "mn": 1e6, "billion": 1e9, "bn": 1e9, "trillion": 1e12}
_YEARLIKE = re.compile(r"^(?:19|20)\d{2}$")


class GateResult(BaseModel):
    ok: bool
    scope_satisfied: bool = True
    missing: list[str] = Field(default_factory=list)          # requirements no fact meets
    unsupported: list[str] = Field(default_factory=list)      # figures in the prose that no fact carries
    contradictions: list[str] = Field(default_factory=list)   # "7,719 below 7,706": a comparison the numbers deny
    stale: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def gap_sentence(self, contract: QuestionContract) -> str:
        parts = []
        if not self.scope_satisfied:
            parts.append("The exact scope asked for could not be verified from a source that covers it.")
        parts += [f"Not established: {m}." for m in self.missing]
        if self.stale:
            parts.append("Older than the freshness the question needs: " + "; ".join(self.stale) + ".")
        parts += [n[0].upper() + n[1:] + "." for n in self.notes if n]          # "Covers 5.7 hours of the 24 hours asked."
        return " ".join(parts)


def _numbers_in_facts(facts: list[Fact]) -> set[str]:
    sigs: set[str] = set()

    def add(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            sigs.add(_sig(v))
        elif isinstance(v, str):
            for m in _FIGURE.finditer(v):
                n = _parse(m.group(0))
                if n is not None:
                    sigs.add(_sig(n))
        elif isinstance(v, dict):
            for x in v.values():
                add(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                add(x)
    for f in facts:
        add(f.value); add(f.attrs); add(f.subject)
    return sigs


def _parse(token: str) -> float | None:
    text = token.strip().replace(",", "").replace("$", "").replace(" ", "")
    mult = 1.0
    word = re.search(r"(thousand|million|billion|trillion|mn|bn)$", text, re.I)
    if word:
        # "$52.4 million" in the prose is the card's "$52.4M" (a withheld
        # summary over that spelling, live 2026-09-23).
        mult = _WORD_MULT[word.group(1).lower()]
        text = text[:word.start()]
    elif text and text[-1] in "kKmMbB%xX":
        mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get(text[-1].lower(), 1.0)
        text = text[:-1]
    try:
        return float(text) * mult
    except ValueError:
        return None


def _sig(value: float) -> str:
    """A tolerant signature: three significant figures of the magnitude."""
    if value == 0:
        return "0"
    return f"{abs(value):.3g}"


def prose_of(answer: str) -> str:
    """The answer without its tables, links and code: the sentences the model wrote."""
    lines = [l for l in (answer or "").splitlines() if not l.strip().startswith("|")]
    text = "\n".join(lines)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"`[^`]*`", "", text)
    text = re.sub(r"https?://\S+", "", text)
    return text


_DATETIME = re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\b|\b\d{1,2}:\d{2}(?::\d{2})?\b"
                       r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,\s*(?:19|20)\d{2})?\b"        # "September 24, 2025" is a date, not 24 and 2025
                       r"|\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?(?:\s+(?:19|20)\d{2})?\b", re.I)


def _cumulative(facts: list[Fact]) -> set[str]:
    """Signatures of the running totals of each fact kind's values, in the
    order given: "the top 10 holders control 35.34%" is arithmetic on the
    holder rows, not a figure from nowhere."""
    sigs: set[str] = set()
    by_kind: dict[str, list[float]] = {}
    for f in facts:
        if isinstance(f.value, (int, float)) and not isinstance(f.value, bool):
            by_kind.setdefault(f.kind, []).append(float(f.value))
    for values in by_kind.values():
        total = 0.0
        for v in values:
            total += v
            sigs.add(_sig(total))
    return sigs


def unsupported_figures(answer: str, facts: list[Fact], evidence_text: str = "") -> list[str]:
    """Figures in the prose that neither a fact, the evidence cards, nor a
    running total of the facts carries. Dates and clock times are not figures."""
    known = _numbers_in_facts(facts) | _cumulative(facts)
    values: list[float] = []
    for f in facts:
        if isinstance(f.value, (int, float)) and not isinstance(f.value, bool):
            values.append(float(f.value))
        for v in (f.attrs or {}).values():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                values.append(float(v))
    for m in _FIGURE.finditer(_DATETIME.sub(" ", evidence_text or "")):
        n = _parse(m.group(0))
        if n is not None:
            known.add(_sig(n))
            values.append(n)
    out: list[str] = []
    for m in _FIGURE.finditer(_DATETIME.sub(" ", prose_of(answer))):
        token = m.group(0).strip().rstrip(",.")           # "2025," is the year 2025 (a withheld summary over that comma, 2026-09-24)
        n = _parse(token)
        if n is None or _YEARLIKE.match(token) or (abs(n) <= 12 and "%" not in token and "$" not in token and not token[-1:].lower() in "kmb"):
            continue
        if _sig(n) in known or _rounds_to(n, token, values):
            continue
        if token not in out:
            out.append(token)
    return out[:8]


def _rounds_to(n: float, token: str, values: list[float]) -> bool:
    """Whether a known figure rounds to the prose figure at the prose's own
    precision: "82%" for a card's 82.3%, "$10" for $10.19, "5.7 USDC" for
    5.72737 (a correct portfolio summary was withheld over 82%, 2026-09-24).
    A rounding never widens more than the written digits allow."""
    core = re.sub(r"[^0-9.]", "", token)
    decimals = len(core.split(".")[1]) if "." in core else 0
    scale = {"k": 1e3, "m": 1e6, "b": 1e9}.get(token[-1:].lower(), 1.0) if token[-1:].lower() in "kmb" else 1.0
    for v in values:
        if v == 0:
            continue
        if round(v / scale, decimals) == round(n / scale, decimals) and abs(v - n) <= abs(n) * 0.02 + scale * 0.5 * 10 ** -decimals:
            return True
    return False


def _numeric_core(token: str) -> str:
    return re.sub(r"[^0-9.]", "", token).strip(".")


def missing_exact_figures(claim: str, evidence_text: str) -> list[str]:
    """Figures in a claim that the evidence does not carry at the same value:
    for a headline, "27,243.24" is not "27,244.28" even though the tolerant
    signature the answer check uses calls them the same (the Home strip,
    2026-09-23), while "$87,000" is "$87K" and "$998.95 million" is
    "$998.95M". Dates, times and years are not figures."""
    values = set()
    for m in _FIGURE.finditer(_DATETIME.sub(" ", evidence_text or "")):
        n = _parse(m.group(0))
        if n is not None:
            values.add(round(n, 6))
    out: list[str] = []
    for m in _FIGURE.finditer(_DATETIME.sub(" ", claim or "")):
        token = m.group(0).strip().rstrip(",.")
        n = _parse(token)
        if n is None or _YEARLIKE.match(token) or (abs(n) < 10 and "%" not in token and "$" not in token and not token[-1:].lower() in "kmb"):
            continue
        if round(n, 6) not in values and token not in out:
            out.append(token)
    return out


_COMPARATIVE = re.compile(r"(?P<a>\$?\d[\d,]*(?:\.\d+)?%?)\s*,?\s*(?:(?:is|was|which\s+is|sits|closed|closing|trading|now)\s+)?(?P<rel>below|under|beneath|lower\s+than|less\s+than|down\s+from|above|over|higher\s+than|more\s+than|up\s+from)\s+(?:(?:a|an|the|its|yesterday'?s|prior|previous|earlier|last|close|level|of|at)\s+){0,4}(?P<b>\$?\d[\d,]*(?:\.\d+)?%?)", re.I)
_LOWER = ("below", "under", "beneath", "lower than", "less than", "down from")


def contradictions(answer: str) -> list[str]:
    """Numeric comparatives the numbers themselves deny: "7,719 was below
    a prior 7,706 close" (expanded UI review, 2026-09-24). Only explicit
    "A below/above B" phrases with two figures of the same unit are judged."""
    out: list[str] = []
    for m in _COMPARATIVE.finditer(prose_of(answer or "")):
        a, b = _parse(m.group("a")), _parse(m.group("b"))
        if a is None or b is None or a == b:
            continue
        if ("%" in m.group("a")) != ("%" in m.group("b")):
            continue
        rel = " ".join(m.group("rel").lower().split())
        says_lower = rel in _LOWER
        if (a < b) != says_lower:
            phrase = m.group(0).strip()
            if phrase not in out:
                out.append(phrase[:80])
    return out[:5]


def check(contract: QuestionContract, facts: list[Fact], answer: str | None = None, *, scope_satisfied: bool = True, now: datetime | None = None,
          evidence_text: str = "") -> GateResult:
    now = now or datetime.now(timezone.utc)
    result = GateResult(ok=True, scope_satisfied=scope_satisfied)
    # A fact older than the freshness the contract asks for is named and set
    # aside: it satisfies nothing (review 2026-09-23: stale evidence passed
    # the gate as ok). A fact with no stated time under a freshness contract
    # is not known to be current and satisfies nothing either (second review:
    # an undated fact passed as current); the pipeline stamps every fact it
    # fetched live with the fetch time, so only unstamped, undated material
    # (a knowledge-base passage, a source with no date) is set aside.
    by_kind: dict[str, list[Fact]] = {}
    for f in facts:
        if contract.freshness_seconds:
            age = None
            if f.observed_at:
                try:
                    age = (now - datetime.fromisoformat(f.observed_at)).total_seconds()
                except ValueError:
                    age = None
            if age is None and f.kind not in ("event", "source"):
                label = f"{f.source} (no observation time stated)"
                if label not in result.stale:
                    result.stale.append(label)
                continue
            if age is not None and age > contract.freshness_seconds:
                label = f"{f.source} ({age / 60:.0f} min old)"
                if label not in result.stale:
                    result.stale.append(label)
                continue
        by_kind.setdefault(f.kind, []).append(f)
    # A contract that names no required facts still requires what its kind
    # means (second review: a model contract with an empty list passed with
    # zero facts).
    required_facts = list(contract.required_facts or []) or list(_DEFAULT_REQUIRED.get(contract.kind, ["source"]))
    if contract.kind == "portfolio" and contract.subject.id and evidence_text:
        # The holdings shown must be the wallet asked about: a token contract
        # in the conversation's focus was once read as "the wallet".
        wallet = contract.subject.id
        short = f"{wallet[:6]}…{wallet[-4:]}"
        if wallet not in evidence_text and short not in evidence_text and f"{wallet[:4]}…{wallet[-4:]}" not in evidence_text:
            result.missing.append("holdings of the wallet asked (the card is for another address)")
            result.ok = False
            by_kind.pop("holding_row", None)
    for required in required_facts:
        rows = by_kind.get(required, [])
        need = _MIN_ROWS.get(required, 1)
        if required == "source":
            rows = [r for r in rows if r.event_date or r.attrs.get("date")]          # a source without a date is not the dated, linked source the contract asks for
        if contract.kind == "market_ranking" and required == "ranking_row":
            # A filter is met only by a row that states the property: a row
            # with no type column does not establish a tokenized stock, and a
            # row with no liquidity figure does not establish a liquidity floor.
            if contract.filters.get("stocks_only"):
                rows = [r for r in rows if r.attrs.get("type") == "stock"]
            if contract.filters.get("crypto_only"):
                rows = [r for r in rows if r.attrs.get("type") != "stock"]     # a global crypto ranking has no type column and no stocks
            if contract.filters.get("min_liquidity_usd"):
                floor = float(contract.filters["min_liquidity_usd"])
                rows = [r for r in rows if r.attrs.get("liquidity_usd") is not None and r.attrs["liquidity_usd"] >= floor]
        if contract.kind == "yields" and required == "yield_row" and contract.filters.get("single_asset"):
            rows = [r for r in rows if r.attrs.get("exposure") == "single"]
        if contract.kind == "recent_events" and required == "event":
            window = timedelta(hours=contract.window_hours or 24 * 30)
            dated = [r for r in rows if event_date_of(r) is not None]
            # Recent means inside the window and not in the future: an event
            # dated October 1 passed a September 23 "last 24 hours" contract
            # (second review, 2026-09-23). A day ahead is allowed for timezones.
            recent = [r for r in dated if -_AHEAD_SLACK <= now - event_date_of(r) <= window]
            upcoming = [r for r in dated if now - event_date_of(r) < -_AHEAD_SLACK]
            if not dated:
                result.missing.append("a dated event (the sources gave no event date)")
                result.ok = False
                continue
            if not recent:
                past = [event_date_of(r) for r in dated if now - event_date_of(r) >= -_AHEAD_SLACK]
                detail = (f"the newest past event is {max(past).date()}" if past else "every dated event is in the future") + (f"; {len(upcoming)} upcoming" if upcoming and past else "")
                result.missing.append(f"an event inside the last {window.days} days ({detail})")
                result.ok = False
                continue
            rows = recent
        if len(rows) < need:
            what = {"ranking_row": "enough ranked rows for the asked scope", "holder_row": "holder positions", "yield_row": "a matching yield pool", "event": "an event", "source": "a dated, linked source"}.get(required, required)
            qualifier = ""
            if contract.filters.get("single_asset") and required == "yield_row":
                qualifier = " that is single-asset"
            if contract.filters.get("stocks_only") and required == "ranking_row":
                qualifier = " that are tokenized stocks"
            result.missing.append(f"{what}{qualifier} ({len(rows)} of {need})")
            result.ok = False
    if answer:
        # A figure the prose states that no fact carries fails the gate: the
        # pipeline re-synthesizes once without it, then names what remains.
        result.unsupported = unsupported_figures(answer, facts, evidence_text)
        if result.unsupported:
            result.ok = False
        result.contradictions = contradictions(answer)
        if result.contradictions:
            result.ok = False
    if not scope_satisfied:
        result.ok = False
    return result
