"""The evidence envelope: what a tool saw, from where, and what it could not see.

A tool answers with a card (markdown) the user reads. Beside the card it
now records an envelope the machinery reads: status, subject, validated
observations, sources with their time, coverage (attempted, successful,
missing) and the operations that failed with their reasons. A partial
failure stays visible to every later stage -- the validator, the receipt,
the job's evidence -- instead of disappearing into prose. The live BONK
bundle check of 2026-09-22 ("25 traced, none found", with all 25 lookups
failed) is the case this exists for: "25 attempted, 7 successful, 18
unavailable" must never read as "25 traced".

Envelopes are collected per turn through a ContextVar, bound where the
turn starts (execution_policy) and by the job engine for a job run, so a
tool records without knowing who is listening; `record` is a no-op when
nobody is. Adapted from the shape of Dexter's market-data tool (results,
sources and an `_errors` collection travelling together), with coverage
and validation as Orbit's additions.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal["complete", "partial", "unavailable"]

_GAP_WORDS = ("unavailable", "not available", "failed", "could not", "couldn't", "inconclusive", "unknown", "not covered", "not valued",
              "partial", "missing", "did not complete", "no data", "not traced", "lookups failed", "left out")


class Anchor(BaseModel):
    """Where one observation can be verified without trusting us: a
    transaction, a slot or block, a provider record, or a quote at a slot.
    `at` is the provider's own time for the fact, never our collection time."""
    kind: Literal["tx", "slot", "record", "quote"]
    ref: str                                   # signature, slot number, record id (a wallet, a pool), quote id
    provider: str
    chain: str | None = None
    at: str | None = None
    note: str | None = None                    # what the anchor supports, in a few words

    def link(self) -> str | None:
        if self.kind == "tx" and self.chain == "solana":
            return f"https://solscan.io/tx/{self.ref}"
        if self.kind == "tx" and (self.chain or "").startswith("evm:"):
            return None
        return None


MAX_ANCHORS = 60


class Evidence(BaseModel):
    tool: str
    status: Status
    subject: dict[str, Any] = Field(default_factory=dict)          # kind, id (address), chain, symbol
    data: dict[str, Any] = Field(default_factory=dict)             # validated observations, small and structured
    sources: list[dict[str, Any]] = Field(default_factory=list)    # {"provider", "endpoint", "as_of"}
    coverage: dict[str, Any] = Field(default_factory=lambda: {"attempted": 0, "successful": 0, "missing": []})
    errors: list[dict[str, Any]] = Field(default_factory=list)     # {"operation", "reason", "count"?}
    anchors: list[Anchor] = Field(default_factory=list)            # what each observation rests on (capped at MAX_ANCHORS)
    interpreter: str = "2026-09-23.1"                              # the code version that read the provider's answer
    as_of: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())    # collection time; anchors carry the provider's

    def anchored(self) -> bool:
        return bool(self.anchors)

    def add_anchor(self, anchor: Anchor) -> None:
        if len(self.anchors) < MAX_ANCHORS:
            self.anchors.append(anchor)

    def gap(self) -> str:
        """One sentence naming what was not seen, for a card or an answer."""
        if self.status == "complete":
            return ""
        cov = self.coverage or {}
        parts = []
        if cov.get("attempted"):
            parts.append(f"{cov.get('successful', 0)} of {cov['attempted']} lookups succeeded")
        for item in cov.get("missing") or []:
            parts.append(str(item))
        for err in self.errors[:3]:
            parts.append(f"{err.get('operation')}: {err.get('reason')}" + (f" ({err['count']}x)" if err.get("count") else ""))
        head = "No data" if self.status == "unavailable" else "Partial data"
        return f"{head} from {self.tool}: " + "; ".join(parts) if parts else f"{head} from {self.tool}."

    def public(self) -> dict:
        """What the API returns beside the answer: no bulk data, the anchors
        that make the observations checkable."""
        return {"tool": self.tool, "status": self.status, "subject": self.subject, "sources": self.sources,
                "coverage": self.coverage, "errors": self.errors, "as_of": self.as_of, "interpreter": self.interpreter,
                "anchors": [a.model_dump(mode="json") for a in self.anchors[:MAX_ANCHORS]]}


_registry: ContextVar[list[Evidence] | None] = ContextVar("evidence_registry", default=None)


def start_turn() -> Token:
    return _registry.set([])


def end_turn(token: Token) -> None:
    _registry.reset(token)


def keep(item: Evidence) -> Evidence:
    """Keep the envelope for whoever is listening this turn; return it."""
    bucket = _registry.get()
    if bucket is not None:
        bucket.append(item)
    return item


def collected() -> list[Evidence]:
    return list(_registry.get() or [])


def tx(ref: str, provider: str, chain: str | None, at: str | int | float | None = None, note: str | None = None) -> Anchor:
    return Anchor(kind="tx", ref=str(ref), provider=provider, chain=chain, at=None if at is None else str(at), note=note)     # a provider's epoch int is still a stamp, never a crash


def record(ref: str, provider: str, chain: str | None, at: str | int | float | None = None, note: str | None = None) -> Anchor:
    return Anchor(kind="record", ref=str(ref), provider=provider, chain=chain, at=None if at is None else str(at), note=note)


def slot(ref: int | str, provider: str, chain: str | None, at: str | None = None, note: str | None = None) -> Anchor:
    return Anchor(kind="slot", ref=str(ref), provider=provider, chain=chain, at=at, note=note)


def source(provider: str, endpoint: str | None = None) -> dict:
    return {"provider": provider, "endpoint": endpoint, "as_of": datetime.now(timezone.utc).isoformat()}


def complete(tool: str, subject: dict, data: dict, sources: list[dict], attempted: int = 1) -> Evidence:
    return keep(Evidence(tool=tool, status="complete", subject=subject, data=data, sources=sources,
                           coverage={"attempted": attempted, "successful": attempted, "missing": []}))


def partial(tool: str, subject: dict, data: dict, sources: list[dict], *, attempted: int, successful: int,
            missing: list[str] | None = None, errors: list[dict] | None = None) -> Evidence:
    return keep(Evidence(tool=tool, status="partial", subject=subject, data=data, sources=sources,
                           coverage={"attempted": attempted, "successful": successful, "missing": list(missing or [])}, errors=list(errors or [])))


def unavailable(tool: str, subject: dict, reason: str, sources: list[dict] | None = None, attempted: int = 1) -> Evidence:
    return keep(Evidence(tool=tool, status="unavailable", subject=subject, sources=list(sources or []),
                           coverage={"attempted": attempted, "successful": 0, "missing": [reason]}, errors=[{"operation": tool, "reason": reason}]))


def undisclosed_gaps(answer: str, items: list[Evidence]) -> list[Evidence]:
    """Envelopes that were partial or unavailable while the answer never says
    so -- the validator's coverage check."""
    text = (answer or "").lower()
    if any(word in text for word in _GAP_WORDS):
        return []
    return [e for e in items if e.status != "complete"]
