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


class Evidence(BaseModel):
    tool: str
    status: Status
    subject: dict[str, Any] = Field(default_factory=dict)          # kind, id (address), chain, symbol
    data: dict[str, Any] = Field(default_factory=dict)             # validated observations, small and structured
    sources: list[dict[str, Any]] = Field(default_factory=list)    # {"provider", "endpoint", "as_of"}
    coverage: dict[str, Any] = Field(default_factory=lambda: {"attempted": 0, "successful": 0, "missing": []})
    errors: list[dict[str, Any]] = Field(default_factory=list)     # {"operation", "reason", "count"?}
    as_of: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

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
        """What the API returns beside the answer: no bulk data."""
        return {"tool": self.tool, "status": self.status, "subject": self.subject, "sources": self.sources,
                "coverage": self.coverage, "errors": self.errors, "as_of": self.as_of}


_registry: ContextVar[list[Evidence] | None] = ContextVar("evidence_registry", default=None)


def start_turn() -> Token:
    return _registry.set([])


def end_turn(token: Token) -> None:
    _registry.reset(token)


def record(item: Evidence) -> Evidence:
    """Keep the envelope for whoever is listening this turn; return it."""
    bucket = _registry.get()
    if bucket is not None:
        bucket.append(item)
    return item


def collected() -> list[Evidence]:
    return list(_registry.get() or [])


def source(provider: str, endpoint: str | None = None) -> dict:
    return {"provider": provider, "endpoint": endpoint, "as_of": datetime.now(timezone.utc).isoformat()}


def complete(tool: str, subject: dict, data: dict, sources: list[dict], attempted: int = 1) -> Evidence:
    return record(Evidence(tool=tool, status="complete", subject=subject, data=data, sources=sources,
                           coverage={"attempted": attempted, "successful": attempted, "missing": []}))


def partial(tool: str, subject: dict, data: dict, sources: list[dict], *, attempted: int, successful: int,
            missing: list[str] | None = None, errors: list[dict] | None = None) -> Evidence:
    return record(Evidence(tool=tool, status="partial", subject=subject, data=data, sources=sources,
                           coverage={"attempted": attempted, "successful": successful, "missing": list(missing or [])}, errors=list(errors or [])))


def unavailable(tool: str, subject: dict, reason: str, sources: list[dict] | None = None, attempted: int = 1) -> Evidence:
    return record(Evidence(tool=tool, status="unavailable", subject=subject, sources=list(sources or []),
                           coverage={"attempted": attempted, "successful": 0, "missing": [reason]}, errors=[{"operation": tool, "reason": reason}]))


def undisclosed_gaps(answer: str, items: list[Evidence]) -> list[Evidence]:
    """Envelopes that were partial or unavailable while the answer never says
    so -- the validator's coverage check."""
    text = (answer or "").lower()
    if any(word in text for word in _GAP_WORDS):
        return []
    return [e for e in items if e.status != "complete"]
