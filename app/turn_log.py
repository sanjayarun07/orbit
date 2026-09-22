"""The turn log: every chat request and what came back, kept for review.

Closed beta (user decision, 2026-09-21): "keep track of all requests and
response. we need to fix all issues and keep them logged so we know what
happened." Conversation history already lives in Redis per session with a
retention window, and a user can delete it; that is the user's record. This
is the operator's: durable, per turn, including the turns that never
produced an answer (rate limited, out of credits, timed out, crashed), with
the status, the latency, the tools that ran, the validation and gate
verdicts, and the user's rating when they gave one.

One row per turn in `chat_turns` (Postgres; bounded in-memory fallback), an
admin view at /admin/turns to list, search, inspect and flag turns, and a
review script (scripts/turn_review.py) that writes a digest of errors,
flags and thumbs-down turns for a period.

A turn is flagged by an operator (with a note) or automatically when the
user rates it down; `resolved_at` closes it. Nothing here is on the turn's
critical path: recording is best-effort and never raises into a response.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import get_pg_pool

logger = logging.getLogger(__name__)

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS chat_turns (
    id UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    latency_ms INTEGER,
    status TEXT NOT NULL,                 -- ok | error
    http_status INTEGER,
    error TEXT,
    transport TEXT,                       -- json | stream | mcp
    identity_kind TEXT,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    account_id TEXT,
    api_key_id TEXT,
    session_id TEXT,
    revision INTEGER,
    wallet TEXT,
    message TEXT NOT NULL,
    intent TEXT,
    capabilities TEXT[] NOT NULL DEFAULT '{}',
    tools TEXT[] NOT NULL DEFAULT '{}',
    answer TEXT,
    trajectory JSONB,
    validation JSONB,
    gate JSONB,
    risk JSONB,
    credits JSONB,
    plan_id TEXT,
    flagged BOOLEAN NOT NULL DEFAULT FALSE,
    flag_note TEXT,
    flagged_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS chat_turns_time ON chat_turns (created_at DESC);
CREATE INDEX IF NOT EXISTS chat_turns_status_time ON chat_turns (status, created_at DESC);
CREATE INDEX IF NOT EXISTS chat_turns_user_time ON chat_turns (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS chat_turns_session ON chat_turns (session_id, revision);
"""
_ready = False
_memory: deque[dict] = deque(maxlen=2000)
_lock = threading.Lock()

ANSWER_LIMIT = 20_000
STORE_TIMEOUT_SECONDS = 5.0     # a slow database keeps the row in memory, never the turn waiting
MESSAGE_LIMIT = 4_000
TRAJECTORY_LIMIT = 60_000
_IGNORED_TOOLS = {"semantic_cache", "finish"}


def reset_for_test() -> None:
    global _ready
    with _lock:
        _memory.clear()
    _ready = False


async def _pool():
    global _ready
    try:
        pool = await get_pg_pool()
    except Exception:
        return None
    if pool is not None and not _ready:
        try:
            async with pool.acquire() as conn:
                await conn.execute(_TABLE_SQL)
            _ready = True
        except Exception:
            logger.warning("turn_log: schema not ready; using memory store", exc_info=True)
            return None
    return pool


def _now() -> datetime:
    return datetime.now(timezone.utc)


def tools_in(trajectory: object) -> list[str]:
    """Distinct tool names in a (public or full) trajectory, in call order."""
    out: list[str] = []

    def walk(node):
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key.startswith("tool_name_") and isinstance(value, str) and value not in _IGNORED_TOOLS and value not in out:
                out.append(value)
            elif isinstance(value, dict):
                walk(value)
    walk(trajectory)
    return out


def _bounded_json(value: Any, limit: int) -> Any:
    """A JSON-able copy, replaced by a note when it would be too large to keep."""
    if value is None:
        return None
    try:
        text = json.dumps(value, default=str)
    except Exception:
        return {"note": "not serialisable"}
    if len(text) > limit:
        return {"note": f"omitted: {len(text)} bytes (limit {limit})", "tools": tools_in(value) if isinstance(value, dict) else []}
    return json.loads(text)


def build(*, message: str, status: str, latency_ms: int | None, http_status: int | None = None, error: str | None = None,
          transport: str | None = None, identity: Any = None, session_id: str | None = None, revision: int | None = None,
          wallet: str | None = None, response: Any = None) -> dict:
    """The row for one turn. `response` is the AgentResponse on success."""
    user = getattr(identity, "user", None) or {}
    api_key = getattr(identity, "api_key", None) or {}
    row = {
        "id": uuid.uuid4().hex, "created_at": _now(), "latency_ms": latency_ms, "status": status, "http_status": http_status,
        "error": (error or "")[:2000] or None, "transport": transport,
        "identity_kind": getattr(identity, "kind", None), "user_id": user.get("id"), "account_id": getattr(identity, "account_id", None),
        "api_key_id": api_key.get("id"), "session_id": session_id, "revision": revision, "wallet": wallet,
        "message": (message or "")[:MESSAGE_LIMIT], "intent": None, "capabilities": [], "tools": [], "answer": None,
        "trajectory": None, "validation": None, "gate": None, "risk": None, "credits": None,
        "plan_id": None, "flagged": False, "flag_note": None, "flagged_at": None, "resolved_at": None,
    }
    if response is not None:
        trajectory = getattr(response, "trajectory", None)
        validation = getattr(response, "validation", None)
        risk = getattr(response, "risk_assessment", None)
        plan = getattr(response, "trade_plan", None)
        row.update({
            "revision": getattr(response, "session_revision", revision), "session_id": getattr(response, "session_id", session_id),
            "intent": getattr(response, "intent", None), "capabilities": list(getattr(response, "capabilities", None) or []),
            "tools": tools_in(trajectory), "answer": (getattr(response, "answer", "") or "")[:ANSWER_LIMIT],
            "trajectory": _bounded_json(trajectory, TRAJECTORY_LIMIT),
            "validation": _bounded_json(validation.model_dump(mode="json") if hasattr(validation, "model_dump") else validation, 8_000),
            "gate": _bounded_json(getattr(response, "answer_gate", None), 4_000),
            "risk": _bounded_json(risk.model_dump(mode="json") if hasattr(risk, "model_dump") else risk, 4_000),
            "credits": _bounded_json(getattr(response, "credits", None), 1_000),
            "plan_id": getattr(plan, "plan_id", None) if plan is not None else None,
        })
    return row


async def record(**fields) -> dict | None:
    """Store one turn. Never raises into the chat path."""
    try:
        row = build(**fields)
    except Exception:
        logger.warning("turn_log: could not build the row", exc_info=True)
        return None
    try:
        pool = await asyncio.wait_for(_pool(), timeout=STORE_TIMEOUT_SECONDS)
        if pool is not None:
            await asyncio.wait_for(pool.execute(
                "INSERT INTO chat_turns (id, created_at, latency_ms, status, http_status, error, transport, identity_kind, user_id, account_id, api_key_id, "
                "session_id, revision, wallet, message, intent, capabilities, tools, answer, trajectory, validation, gate, risk, credits, plan_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20::jsonb, $21::jsonb, $22::jsonb, $23::jsonb, $24::jsonb, $25)",
                uuid.UUID(row["id"]), row["created_at"], row["latency_ms"], row["status"], row["http_status"], row["error"], row["transport"],
                row["identity_kind"], row["user_id"], row["account_id"], row["api_key_id"], row["session_id"], row["revision"], row["wallet"],
                row["message"], row["intent"], row["capabilities"], row["tools"], row["answer"],
                json.dumps(row["trajectory"]) if row["trajectory"] is not None else None,
                json.dumps(row["validation"]) if row["validation"] is not None else None,
                json.dumps(row["gate"]) if row["gate"] is not None else None,
                json.dumps(row["risk"]) if row["risk"] is not None else None,
                json.dumps(row["credits"]) if row["credits"] is not None else None,
                row["plan_id"],
            ), timeout=STORE_TIMEOUT_SECONDS)
            return row
    except Exception:
        logger.warning("turn_log: could not persist; keeping in memory", exc_info=True)
    with _lock:
        _memory.appendleft(row)
    return row


_FIELDS = ("id", "created_at", "latency_ms", "status", "http_status", "error", "transport", "identity_kind", "user_id", "account_id", "api_key_id",
           "session_id", "revision", "wallet", "message", "intent", "capabilities", "tools", "answer", "plan_id", "flagged", "flag_note", "flagged_at", "resolved_at")
_JSON_FIELDS = ("trajectory", "validation", "gate", "risk", "credits")


def _columns(prefix: str = "") -> str:
    """The select list, table-qualified when joined (chat_feedback shares
    session_id, revision and account_id -- ambiguous unqualified, live)."""
    plain = ", ".join(f"{prefix}{name}" for name in _FIELDS)
    as_text = ", ".join(f"{prefix}{name}::text AS {name}" for name in _JSON_FIELDS)
    return f"{plain}, {as_text}"


_COLUMNS = _columns()


def _from_db(r) -> dict:
    row = dict(r)
    row["id"] = str(row["id"]).replace("-", "")
    row["user_id"] = str(row["user_id"]) if row.get("user_id") else None
    for key in ("trajectory", "validation", "gate", "risk", "credits"):
        if isinstance(row.get(key), str):
            try:
                row[key] = json.loads(row[key])
            except ValueError:
                pass
    row["capabilities"] = list(row.get("capabilities") or [])
    row["tools"] = list(row.get("tools") or [])
    return row


def _matches(row: dict, *, status: str | None, user_id: str | None, account_id: str | None, session_id: str | None, q: str | None,
             since: datetime | None, flagged: bool | None, intent: str | None) -> bool:
    if status and row.get("status") != status:
        return False
    if user_id and row.get("user_id") != user_id:
        return False
    if account_id and row.get("account_id") != account_id:
        return False
    if session_id and row.get("session_id") != session_id:
        return False
    if intent and row.get("intent") != intent:
        return False
    if flagged is not None and bool(row.get("flagged")) != flagged:
        return False
    if since and row["created_at"] < since:
        return False
    if q:
        needle = q.lower()
        if needle not in (row.get("message") or "").lower() and needle not in (row.get("answer") or "").lower() and needle not in (row.get("error") or "").lower():
            return False
    return True


async def list_turns(*, days: float = 1.0, status: str | None = None, user_id: str | None = None, account_id: str | None = None,
                     session_id: str | None = None, q: str | None = None, flagged: bool | None = None, intent: str | None = None,
                     limit: int = 100) -> list[dict]:
    """Turns newest first, with the user's rating attached when one exists."""
    since = _now() - timedelta(days=max(0.01, days))
    limit = max(1, min(int(limit), 1000))
    pool = await _pool()
    if pool is not None:
        clauses, params = ["t.created_at >= $1"], [since]
        for column, value in (("t.status", status), ("t.user_id::text", user_id), ("t.account_id", account_id), ("t.session_id", session_id), ("t.intent", intent)):
            if value:
                params.append(value)
                clauses.append(f"{column} = ${len(params)}")
        if flagged is not None:
            params.append(flagged)
            clauses.append(f"t.flagged = ${len(params)}")
        if q:
            params.append(f"%{q}%")
            clauses.append(f"(t.message ILIKE ${len(params)} OR t.answer ILIKE ${len(params)} OR t.error ILIKE ${len(params)})")
        params.append(limit)
        rows = await pool.fetch(
            f"SELECT {_columns('t.')}, f.rating AS rating, f.comment AS rating_comment "
            f"FROM chat_turns t LEFT JOIN chat_feedback f ON f.session_id = t.session_id AND f.revision = t.revision "
            f"WHERE {' AND '.join(clauses)} ORDER BY t.created_at DESC LIMIT ${len(params)}", *params)
        out = []
        for r in rows:
            row = _from_db({k: v for k, v in dict(r).items() if k not in ("rating", "rating_comment")})
            row["rating"], row["rating_comment"] = r["rating"], r["rating_comment"]
            out.append(row)
        return out
    with _lock:
        rows = [dict(r) for r in _memory if _matches(r, status=status, user_id=user_id, account_id=account_id, session_id=session_id, q=q,
                                                      since=since, flagged=flagged, intent=intent)]
    for row in rows:
        row.setdefault("rating", None)
        row.setdefault("rating_comment", None)
    return rows[:limit]


async def get_turn(turn_id: str) -> dict | None:
    pool = await _pool()
    if pool is not None:
        try:
            key = uuid.UUID(turn_id)
        except ValueError:
            return None
        r = await pool.fetchrow(f"SELECT {_COLUMNS} FROM chat_turns WHERE id = $1", key)
        return _from_db(r) if r else None
    with _lock:
        return next((dict(r) for r in _memory if r["id"] == turn_id), None)


async def flag(turn_id: str, note: str | None = None, *, resolved: bool | None = None) -> dict | None:
    """Mark a turn as an issue (with a note), or resolve it. Returns the row."""
    now = _now()
    pool = await _pool()
    if pool is not None:
        try:
            key = uuid.UUID(turn_id)
        except ValueError:
            return None
        if resolved:
            await pool.execute("UPDATE chat_turns SET resolved_at = $2, flag_note = COALESCE($3, flag_note) WHERE id = $1", key, now, note)
        else:
            await pool.execute("UPDATE chat_turns SET flagged = TRUE, flagged_at = COALESCE(flagged_at, $2), flag_note = COALESCE($3, flag_note), resolved_at = NULL WHERE id = $1",
                               key, now, note)
        return await get_turn(turn_id)
    with _lock:
        row = next((r for r in _memory if r["id"] == turn_id), None)
        if row is None:
            return None
        if resolved:
            row["resolved_at"] = now
            row["flag_note"] = note or row.get("flag_note")
        else:
            row["flagged"] = True
            row["flagged_at"] = row.get("flagged_at") or now
            row["flag_note"] = note or row.get("flag_note")
            row["resolved_at"] = None
        return dict(row)


async def scrub_user(user_id: str) -> int:
    """Account deletion: the person's words go, the operational row stays
    (status, latency, tools, timing) with no owner. Returns rows scrubbed."""
    pool = await _pool()
    if pool is not None:
        try:
            status = await pool.execute("UPDATE chat_turns SET message = '[deleted]', answer = NULL, trajectory = NULL, wallet = NULL, user_id = NULL, "
                                        "account_id = NULL, session_id = NULL WHERE user_id = $1::uuid", user_id)
            return int(status.split()[-1]) if status and status.split()[-1].isdigit() else 0
        except Exception:
            logger.warning("turn_log: scrub failed for %s", user_id[:8], exc_info=True)
            return 0
    n = 0
    with _lock:
        for row in _memory:
            if row.get("user_id") == user_id:
                row.update({"message": "[deleted]", "answer": None, "trajectory": None, "wallet": None, "user_id": None, "account_id": None, "session_id": None})
                n += 1
    return n


async def flag_by_revision(session_id: str, revision: int, note: str) -> None:
    """A thumbs-down from the user flags the turn it rated."""
    pool = await _pool()
    if pool is not None:
        await pool.execute("UPDATE chat_turns SET flagged = TRUE, flagged_at = COALESCE(flagged_at, NOW()), flag_note = COALESCE(flag_note, $3) "
                           "WHERE session_id = $1 AND revision = $2", session_id, revision, note)
        return
    with _lock:
        for row in _memory:
            if row.get("session_id") == session_id and row.get("revision") == revision:
                row["flagged"] = True
                row["flagged_at"] = row.get("flagged_at") or _now()
                row["flag_note"] = row.get("flag_note") or note


async def summary(days: float = 1.0) -> dict:
    """The numbers for a review: turns, errors by status, latency, the tools
    that ran most, open flags, thumbs down. Over the whole period from SQL
    when Postgres holds the rows (review, 2026-09-22: a 1,000-row sample was
    presented as period-wide); the memory store counts its rows."""
    pool = await _pool()
    if pool is not None:
        return await _summary_sql(pool, days)
    since = _now() - timedelta(days=max(0.01, days))
    with _lock:
        rows = [dict(r) for r in _memory if r["created_at"] >= since]
    for row in rows:
        row.setdefault("rating", None)
    latencies = sorted(r["latency_ms"] for r in rows if r.get("latency_ms") is not None)

    def pct(p: float) -> int | None:
        if not latencies:
            return None
        return int(latencies[min(len(latencies) - 1, int(p * len(latencies)))])

    errors: dict[str, int] = {}
    for r in rows:
        if r["status"] == "error":
            key = f"{r.get('http_status') or '?'}"
            errors[key] = errors.get(key, 0) + 1
    tools: dict[str, int] = {}
    for r in rows:
        for t in r.get("tools") or []:
            tools[t] = tools.get(t, 0) + 1
    intents: dict[str, int] = {}
    for r in rows:
        intents[r.get("intent") or "none"] = intents.get(r.get("intent") or "none", 0) + 1
    return {
        "days": days, "turns": len(rows), "ok": sum(1 for r in rows if r["status"] == "ok"),
        "errors": sum(1 for r in rows if r["status"] == "error"), "errors_by_status": errors,
        "latency_ms": {"p50": pct(0.5), "p95": pct(0.95), "max": latencies[-1] if latencies else None},
        "open_flags": sum(1 for r in rows if r.get("flagged") and not r.get("resolved_at")),
        "thumbs_down": sum(1 for r in rows if r.get("rating") == "down"),
        "validation_warnings": sum(1 for r in rows if (r.get("validation") or {}).get("status") == "warn"),
        "gate_interventions": sum(1 for r in rows if r.get("gate")),
        "by_intent": intents, "tools": sorted(tools.items(), key=lambda kv: -kv[1])[:15],
        "users": len({r.get("user_id") for r in rows if r.get("user_id")}),
    }


async def _summary_sql(pool, days: float) -> dict:
    since = _now() - timedelta(days=max(0.01, days))
    totals = await pool.fetchrow(
        "SELECT count(*) AS turns, count(*) FILTER (WHERE status = 'ok') AS ok, count(*) FILTER (WHERE status = 'error') AS errors, "
        "count(DISTINCT user_id) AS users, count(*) FILTER (WHERE flagged AND resolved_at IS NULL) AS open_flags, "
        "count(*) FILTER (WHERE validation->>'status' = 'warn') AS validation_warnings, count(*) FILTER (WHERE gate IS NOT NULL) AS gate_interventions, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50, percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95, max(latency_ms) AS max "
        "FROM chat_turns WHERE created_at >= $1", since)
    errors = await pool.fetch("SELECT COALESCE(http_status::text, '?') AS code, count(*) AS n FROM chat_turns WHERE created_at >= $1 AND status = 'error' GROUP BY 1", since)
    intents = await pool.fetch("SELECT COALESCE(intent, 'none') AS intent, count(*) AS n FROM chat_turns WHERE created_at >= $1 GROUP BY 1", since)
    tools = await pool.fetch("SELECT t.tool, count(*) AS n FROM chat_turns c, unnest(c.tools) AS t(tool) WHERE c.created_at >= $1 GROUP BY 1 ORDER BY 2 DESC LIMIT 15", since)
    thumbs = await pool.fetchval("SELECT count(*) FROM chat_turns c JOIN chat_feedback f ON f.session_id = c.session_id AND f.revision = c.revision "
                                 "WHERE c.created_at >= $1 AND f.rating = 'down'", since)
    return {
        "days": days, "turns": int(totals["turns"]), "ok": int(totals["ok"]), "errors": int(totals["errors"]),
        "errors_by_status": {r["code"]: int(r["n"]) for r in errors},
        "latency_ms": {"p50": int(totals["p50"]) if totals["p50"] is not None else None, "p95": int(totals["p95"]) if totals["p95"] is not None else None,
                       "max": int(totals["max"]) if totals["max"] is not None else None},
        "open_flags": int(totals["open_flags"]), "thumbs_down": int(thumbs or 0), "validation_warnings": int(totals["validation_warnings"]),
        "gate_interventions": int(totals["gate_interventions"]), "by_intent": {r["intent"]: int(r["n"]) for r in intents},
        "tools": [(r["tool"], int(r["n"])) for r in tools], "users": int(totals["users"]),
    }


def public(row: dict, *, full: bool = False) -> dict:
    """The row as the admin API returns it. The list view trims the heavy
    fields; the detail view keeps them."""
    out = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}
    if not full:
        out["answer"] = (row.get("answer") or "")[:400]
        out.pop("trajectory", None)
    return out
