"""Answer ratings (thumbs up / down) that train tool ranking.

A rating is attached to one assistant turn (session + revision). The tools
that produced that answer are read from the stored turn, never from the
client, and each gets the rating folded into its outcome counters
(app/tool_outcomes.py), where it weighs `tool_feedback_weight` automatic
outcomes. One rating per turn: rating again changes it, rating "none" retracts
it, and the counters move by the difference -- so a click can never be
double-counted.
"""

from __future__ import annotations

import json
import logging

from app import tool_outcomes
from app.db import get_pg_pool, get_redis
from app.sessions import get_messages

logger = logging.getLogger(__name__)

RATINGS = ("up", "down", "none")
_TTL = 30 * 24 * 60 * 60
_memory: dict[tuple[str, int], dict] = {}


class TurnNotFound(Exception):
    pass


async def _turn(session_id: str, revision: int) -> dict:
    for message in await get_messages(session_id):
        if message.get("role") == "assistant" and int(message.get("session_revision") or -1) == revision:
            return message
    raise TurnNotFound(f"No assistant turn at revision {revision} in this conversation")


async def _load(session_id: str, revision: int) -> dict | None:
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow("SELECT rating, comment, tools FROM chat_feedback WHERE session_id = $1 AND revision = $2", session_id, revision)
        return {"rating": row["rating"], "comment": row["comment"], "tools": list(row["tools"] or [])} if row else None
    redis = await get_redis()
    if redis is not None:
        raw = await redis.get(f"chat_feedback:{session_id}:{revision}")
        return json.loads(raw) if raw else None
    return _memory.get((session_id, revision))


async def _save(session_id: str, revision: int, record: dict, account_id: str | None) -> None:
    pool = await get_pg_pool()
    if pool is not None:
        await pool.execute(
            """
            INSERT INTO chat_feedback (session_id, revision, rating, comment, account_id, tools)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (session_id, revision) DO UPDATE
            SET rating = EXCLUDED.rating, comment = EXCLUDED.comment, account_id = EXCLUDED.account_id, updated_at = NOW()
            """,
            session_id, revision, record["rating"], record.get("comment"), account_id, record["tools"],
        )
        return
    redis = await get_redis()
    if redis is not None:
        await redis.set(f"chat_feedback:{session_id}:{revision}", json.dumps(record), ex=_TTL)
        return
    _memory[(session_id, revision)] = record


async def rate(session_id: str, revision: int, rating: str, comment: str | None = None, account_id: str | None = None) -> dict:
    """Apply a rating to a turn and return {rating, tools, changed}."""
    if rating == "down":
        # The operator's log sees every thumbs-down as an open issue (app/turn_log.py).
        try:
            from app import turn_log
            await turn_log.flag_by_revision(session_id, revision, f"user rated down: {comment or 'no comment'}")
        except Exception:
            logger.warning("turn_log flag on feedback failed", exc_info=True)
    if rating not in RATINGS:
        raise ValueError("rating must be 'up', 'down' or 'none'")
    turn = await _turn(session_id, revision)
    tools = tool_outcomes.tools_in(turn.get("trajectory"))
    previous = await _load(session_id, revision)
    before = previous["rating"] if previous else "none"
    if before == rating and (previous or {}).get("comment") == comment:
        return {"rating": rating, "tools": tools, "changed": False}
    up = int(rating == "up") - int(before == "up")
    down = int(rating == "down") - int(before == "down")
    record = {"rating": rating, "comment": (comment or "")[:500] or None, "tools": tools}
    await _save(session_id, revision, record, account_id)
    await tool_outcomes.record_feedback(tools, up, down)
    return {"rating": rating, "tools": tools, "changed": True}


async def list_for_account(account_id: str) -> list[dict]:
    """Every rating an account gave, for its data export. Postgres only:
    the Redis and memory stores are keyed by turn, not by account."""
    try:
        pool = await get_pg_pool()
    except Exception:
        pool = None
    if pool is None:
        return []
    rows = await pool.fetch("SELECT session_id, revision, rating, comment, tools, updated_at FROM chat_feedback WHERE account_id = $1 ORDER BY updated_at DESC LIMIT 1000", account_id)
    return [{"session_id": r["session_id"], "revision": r["revision"], "rating": r["rating"], "comment": r["comment"], "tools": list(r["tools"] or []),
             "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None} for r in rows]


async def rating_for(session_id: str, revision: int) -> str:
    record = await _load(session_id, revision)
    return record["rating"] if record else "none"


def reset() -> None:
    _memory.clear()
