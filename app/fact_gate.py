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

_MIN_ROWS = {"ranking_row": 3, "holder_row": 5, "yield_row": 1, "event": 1}
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


def unsupported_figures(answer: str, facts: list[Fact]) -> list[str]:
    known = _numbers_in_facts(facts)
    out: list[str] = []
    for m in _FIGURE.finditer(prose_of(answer)):
        token = m.group(0).strip()
        n = _parse(token)
        if n is None or _YEARLIKE.match(token) or (abs(n) <= 12 and "%" not in token and "$" not in token and not token[-1:].lower() in "kmb"):
            continue
        if _sig(n) not in known and token not in out:
            out.append(token)
    return out[:8]


def check(contract: QuestionContract, facts: list[Fact], answer: str | None = None, *, scope_satisfied: bool = True, now: datetime | None = None) -> GateResult:
    now = now or datetime.now(timezone.utc)
    result = GateResult(ok=True, scope_satisfied=scope_satisfied)
    by_kind: dict[str, list[Fact]] = {}
    for f in facts:
        by_kind.setdefault(f.kind, []).append(f)
    for required in contract.required_facts or []:
        rows = by_kind.get(required, [])
        need = _MIN_ROWS.get(required, 1)
        if contract.kind == "market_ranking" and required == "ranking_row":
            if contract.filters.get("stocks_only"):
                rows = [r for r in rows if r.attrs.get("type") in (None, "stock")]
            if contract.filters.get("crypto_only"):
                rows = [r for r in rows if r.attrs.get("type") != "stock"]
            if contract.filters.get("min_liquidity_usd"):
                floor = float(contract.filters["min_liquidity_usd"])
                rows = [r for r in rows if r.attrs.get("volume_usd") is None or r.attrs["volume_usd"] >= floor]
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
            what = {"ranking_row": "enough ranked rows for the asked scope", "holder_row": "holder positions", "yield_row": "a matching yield pool", "event": "an event"}.get(required, required)
            qualifier = ""
            if contract.filters.get("single_asset") and required == "yield_row":
                qualifier = " that is single-asset"
            if contract.filters.get("stocks_only") and required == "ranking_row":
                qualifier = " that are tokenized stocks"
            result.missing.append(f"{what}{qualifier} ({len(rows)} of {need})")
            result.ok = False
    if contract.freshness_seconds:
        for f in facts:
            if f.observed_at:
                try:
                    seen = datetime.fromisoformat(f.observed_at)
                except ValueError:
                    continue
                age = (now - seen).total_seconds()
                if age > contract.freshness_seconds and f.source not in {s for s in result.stale}:
                    result.stale.append(f"{f.source} ({age / 60:.0f} min old)")
    if answer:
        result.unsupported = unsupported_figures(answer, facts)
    if not scope_satisfied:
        result.ok = False
    return result
