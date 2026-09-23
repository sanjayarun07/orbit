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

_MIN_ROWS = {"ranking_row": 3, "holder_row": 5, "yield_row": 1, "event": 1, "source": 1}
_FIGURE = re.compile(r"(?<![\w.])[+\-]?\$?\d[\d,]*(?:\.\d+)?\s*(?:%|[kKmMbB]\b|[xX]\b)?")
_YEARLIKE = re.compile(r"^(?:19|20)\d{2}$")


class GateResult(BaseModel):
    ok: bool
    scope_satisfied: bool = True
    missing: list[str] = Field(default_factory=list)          # requirements no fact meets
    unsupported: list[str] = Field(default_factory=list)      # figures in the prose that no fact carries
    stale: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def gap_sentence(self, contract: QuestionContract) -> str:
        parts = []
        if not self.scope_satisfied:
            parts.append("The exact scope asked for could not be verified from a source that covers it.")
        parts += [f"Not established: {m}." for m in self.missing]
        if self.stale:
            parts.append("Older than the freshness the question needs: " + "; ".join(self.stale) + ".")
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
    if text and text[-1] in "kKmMbB%xX":
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


_DATETIME = re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\b|\b\d{1,2}:\d{2}(?::\d{2})?\b")


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
    for m in _FIGURE.finditer(_DATETIME.sub(" ", evidence_text or "")):
        n = _parse(m.group(0))
        if n is not None:
            known.add(_sig(n))
    out: list[str] = []
    for m in _FIGURE.finditer(_DATETIME.sub(" ", prose_of(answer))):
        token = m.group(0).strip()
        n = _parse(token)
        if n is None or _YEARLIKE.match(token) or (abs(n) <= 12 and "%" not in token and "$" not in token and not token[-1:].lower() in "kmb"):
            continue
        if _sig(n) not in known and token not in out:
            out.append(token)
    return out[:8]


def check(contract: QuestionContract, facts: list[Fact], answer: str | None = None, *, scope_satisfied: bool = True, now: datetime | None = None,
          evidence_text: str = "") -> GateResult:
    now = now or datetime.now(timezone.utc)
    result = GateResult(ok=True, scope_satisfied=scope_satisfied)
    # A fact older than the freshness the contract asks for is named and set
    # aside: it satisfies nothing (review 2026-09-23: stale evidence passed
    # the gate as ok). A fact with no stated time is taken as current.
    by_kind: dict[str, list[Fact]] = {}
    for f in facts:
        if contract.freshness_seconds and f.observed_at:
            try:
                age = (now - datetime.fromisoformat(f.observed_at)).total_seconds()
            except ValueError:
                age = None
            if age is not None and age > contract.freshness_seconds:
                label = f"{f.source} ({age / 60:.0f} min old)"
                if label not in result.stale:
                    result.stale.append(label)
                continue
        by_kind.setdefault(f.kind, []).append(f)
    for required in contract.required_facts or []:
        rows = by_kind.get(required, [])
        need = _MIN_ROWS.get(required, 1)
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
            recent = [r for r in dated if now - event_date_of(r) <= window]
            if not dated:
                result.missing.append("a dated event (the sources gave no event date)")
                result.ok = False
                continue
            if not recent:
                result.missing.append(f"an event inside the last {window.days} days (the newest dated event is {max(event_date_of(r) for r in dated).date()})")
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
    if not scope_satisfied:
        result.ok = False
    return result
