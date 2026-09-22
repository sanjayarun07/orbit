"""Decision receipts: what the analysts saw, what they said, what was skipped.

One record per decision, fully serialised, so "why did Orbit say that about
X" is answered from the record alone -- the ai-hedge-fund CycleRecord idea,
scoped to what Orbit decides today (a deep-dive verdict; later a desk cycle).
Nothing about a decision lives anywhere else: the typed Signals, the coverage
of the evidence, every skipped dimension with its reason, the price at the
time, and the verdict text.

Storage: a `decision_records` table when Postgres is configured (per user,
cascading on account deletion; anonymous decisions carry no user), with a
bounded in-process list as the fallback for tests and keyless deployments.
The current user is bound per turn through a ContextVar, the same way the
TradingView token is, so the research node never has to be handed an
identity it does not otherwise need.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from app.db import get_pg_pool
from app.signals import Signal, Subject, SubjectSkip

logger = logging.getLogger(__name__)

current_user: ContextVar[str | None] = ContextVar("decision_records_user", default=None)

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS decision_records (
    id UUID PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    subject JSONB NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    signals JSONB NOT NULL,
    skipped JSONB NOT NULL,
    coverage JSONB NOT NULL,
    verdict TEXT NOT NULL,
    price DOUBLE PRECISION,
    session_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS decision_records_user_idx ON decision_records (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS decision_records_subject_idx ON decision_records (subject_key, created_at DESC);
"""
_schema_ready = False
_records: list[dict] = []
_MEMORY_LIMIT = 500
_lock = threading.Lock()
VERDICT_LIMIT = 6000


def reset_for_test() -> None:
    global _schema_ready
    with _lock:
        _records.clear()
    _schema_ready = False


def bind_turn(user_id: str | None):
    """Attribute every decision made during this turn to `user_id` (None for an
    anonymous turn). Returns the token for `current_user.reset`."""
    return current_user.set(user_id)


async def _pool():
    global _schema_ready
    try:
        pool = await get_pg_pool()
    except Exception:
        return None
    if pool is not None and not _schema_ready:
        try:
            async with pool.acquire() as conn:
                await conn.execute(_TABLE_SQL)
            _schema_ready = True
        except Exception:
            logger.warning("decision_records: schema not ready; using memory store", exc_info=True)
            return None
    return pool


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build(*, kind: str, subject: Subject, signals: list[Signal], verdict: str, coverage: list[dict] | None = None,
          skipped: list[SubjectSkip] | None = None, price: float | None = None, session_id: str | None = None,
          user_id: str | None = None, as_of: datetime | None = None) -> dict:
    """The record as a plain dict -- what gets stored and what `why()` renders."""
    moment = as_of or _now()
    return {
        "id": uuid.uuid4().hex,
        "user_id": user_id if user_id is not None else current_user.get(),
        "kind": kind,
        "subject_key": subject.key,
        "subject": subject.model_dump(),
        "as_of": moment.isoformat(),
        "signals": [s.model_dump() for s in signals],
        "skipped": [s.model_dump() for s in (skipped or [])],
        "coverage": list(coverage or []),
        "verdict": (verdict or "")[:VERDICT_LIMIT],
        "price": price,
        "session_id": session_id,
    }


async def record(**fields) -> dict | None:
    """Store one decision. Never raises into the chat path: a receipt that
    could not be written is logged, and the answer still goes out. A receipt
    for an account deleted meanwhile (on any worker) is not kept."""
    row = build(**fields)
    if row.get("user_id"):
        from app import turn_log

        if await turn_log.is_deleted(str(row["user_id"])):
            return None
    try:
        pool = await _pool()
        if pool is not None:
            await pool.execute(
                "INSERT INTO decision_records (id, user_id, kind, subject_key, subject, as_of, signals, skipped, coverage, verdict, price, session_id) "
                "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb, $8::jsonb, $9::jsonb, $10, $11, $12)",
                row["id"], row["user_id"], row["kind"], row["subject_key"], json.dumps(row["subject"]),
                datetime.fromisoformat(row["as_of"]), json.dumps(row["signals"]), json.dumps(row["skipped"]),
                json.dumps(row["coverage"]), row["verdict"], row["price"], row["session_id"],
            )
            return row
    except Exception:
        logger.warning("decision_records: could not persist; keeping in memory", exc_info=True)
    with _lock:
        _records.append(row)
        del _records[:-_MEMORY_LIMIT]
    return row


def _from_db(r) -> dict:
    def _json(value):
        return json.loads(value) if isinstance(value, str) else value
    return {
        "id": str(r["id"]).replace("-", ""), "user_id": str(r["user_id"]) if r["user_id"] else None, "kind": r["kind"],
        "subject_key": r["subject_key"], "subject": _json(r["subject"]), "as_of": r["as_of"].isoformat(),
        "signals": _json(r["signals"]), "skipped": _json(r["skipped"]), "coverage": _json(r["coverage"]),
        "verdict": r["verdict"], "price": r["price"], "session_id": r["session_id"],
    }


async def list_for(user_id: str, limit: int | None = 20) -> list[dict]:
    """A user's own receipts, newest first; `limit=None` is all of them."""
    pool = await _pool()
    if pool is not None:
        sql = ("SELECT id, user_id, kind, subject_key, subject::text AS subject, as_of, signals::text AS signals, skipped::text AS skipped, "
               "coverage::text AS coverage, verdict, price, session_id FROM decision_records WHERE user_id = $1 ORDER BY created_at DESC")
        rows = await (pool.fetch(sql, user_id) if limit is None else pool.fetch(sql + " LIMIT $2", user_id, limit))
        return [_from_db(r) for r in rows]
    with _lock:
        mine = [dict(r) for r in _records if r.get("user_id") == user_id]
    mine = list(reversed(mine))
    return mine if limit is None else mine[:limit]


async def for_subject(subject_key: str, limit: int = 5, user_id: str | None = None) -> list[dict]:
    """Earlier decisions about one subject, newest first -- what the reflection
    loop and a "what did you say last time" ask read."""
    pool = await _pool()
    if pool is not None:
        if user_id:
            rows = await pool.fetch(
                "SELECT id, user_id, kind, subject_key, subject::text AS subject, as_of, signals::text AS signals, skipped::text AS skipped, "
                "coverage::text AS coverage, verdict, price, session_id FROM decision_records WHERE subject_key = $1 AND user_id = $2 "
                "ORDER BY created_at DESC LIMIT $3", subject_key, user_id, limit)
        else:
            rows = await pool.fetch(
                "SELECT id, user_id, kind, subject_key, subject::text AS subject, as_of, signals::text AS signals, skipped::text AS skipped, "
                "coverage::text AS coverage, verdict, price, session_id FROM decision_records WHERE subject_key = $1 "
                "ORDER BY created_at DESC LIMIT $2", subject_key, limit)
        return [_from_db(r) for r in rows]
    with _lock:
        rows = [dict(r) for r in _records if r["subject_key"] == subject_key and (user_id is None or r.get("user_id") == user_id)]
    return list(reversed(rows))[:limit]


def public(row: dict) -> dict:
    """The receipt as the API returns it: no user id, the verdict trimmed."""
    return {k: v for k, v in row.items() if k != "user_id"}


def why(row: dict) -> str:
    """Render one receipt as the answer to "why did you say that": every
    analyst's vote or abstention, what was not seen, and the bottom line."""
    subject = Subject(**row["subject"])
    lines = [f"# Decision receipt — {subject.label()} ({row['kind'].replace('_', ' ')})",
             f"**As of** {row['as_of'][:16].replace('T', ' ')} UTC" + (f" · **Price then** ${row['price']:,.6g}" if row.get("price") else ""), ""]
    signals = [Signal(**s) for s in row.get("signals") or []]
    if signals:
        lines += ["| Analyst | View | Conviction | Why |", "|---|---|---:|---|"]
        for s in signals:
            if s.abstained:
                lines.append(f"| {s.model_name} | abstained | — | {s.metadata.get('abstain_reason', '')} |")
            else:
                view = "bullish" if s.value > 0.05 else "bearish" if s.value < -0.05 else "neutral"
                lines.append(f"| {s.model_name} | {view} | {s.value:+.2f} | {(s.reasoning or '').splitlines()[0][:160] if s.reasoning else ''} |")
    skipped = row.get("skipped") or []
    if skipped:
        lines += ["", "Not seen:"] + [f"- {s['subject']}: {s['reason']}" for s in skipped]
    covered = [c.get("name") for c in (row.get("coverage") or []) if c.get("status") == "available"]
    if covered:
        lines += ["", "Evidence used: " + ", ".join(str(c) for c in covered)]
    verdict = (row.get("verdict") or "").strip()
    if verdict:
        lines += ["", "## Verdict as given", verdict]
    return "\n".join(lines)


def public_signal_summary(signals: list[Signal]) -> list[dict[str, Any]]:
    """Compact per-analyst view for a chat card: name, view, conviction."""
    out = []
    for s in signals:
        out.append({"analyst": s.model_name, "abstained": s.abstained, "conviction": round(s.value, 3),
                    "reason": (s.metadata.get("abstain_reason") if s.abstained else (s.reasoning or ""))[:200]})
    return out
