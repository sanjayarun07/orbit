"""Multi-turn conversation history, keyed by client-provided session_id.

Backed by Redis when available (durable across restarts); falls back to an
in-process dict automatically if Redis is unreachable. Serves two purposes:
- bounded context fed back into DSPy prompts (append_turn/history_text)
- full displayable history for the chat UI (get_messages/clear_history)
"""

import asyncio
import json
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from app.db import get_redis
from app.settings import settings

def _base_ttl() -> int:
    return int(settings.chat_history_ttl_seconds)


_retention: dict[str, int] = {}   # session_id -> seconds, for the in-memory store
_MAX_DISPLAY_MESSAGES = 200  # total messages retained for UI history

_sessions: dict[str, deque[dict]] = {}
_last_seen: dict[str, float] = {}
_contexts: dict[str, dict] = {}
_session_locks: dict[str, asyncio.Lock] = {}


@dataclass
class SessionTurnLease:
    """One in-flight mutation lease for a conversation."""

    lock: Any
    distributed: bool = False

    async def release(self) -> None:
        if self.distributed:
            try:
                await self.lock.release()
            except Exception:
                # Redis may already have expired a lease after a hard timeout.
                pass
        elif self.lock.locked():
            self.lock.release()


async def acquire_session_turn(session_id: str) -> SessionTurnLease:
    """Serialize each conversation across tabs, workers, and retries."""
    redis = await get_redis()
    wait_seconds = max(1, settings.request_queue_timeout_seconds)
    if redis is not None:
        lock = redis.lock(
            f"chat_turn_lock:{session_id}",
            timeout=max(15, settings.chat_execution_timeout_seconds + 15),
            blocking_timeout=wait_seconds,
        )
        if not await lock.acquire():
            raise asyncio.TimeoutError("Another request is already updating this chat")
        return SessionTurnLease(lock, distributed=True)
    lock = _session_locks.setdefault(session_id, asyncio.Lock())
    await asyncio.wait_for(lock.acquire(), timeout=wait_seconds)
    return SessionTurnLease(lock)


def _ttl_for(session_id: str) -> int:
    """Seconds a session's keys should live: the long, signed-in retention
    once extend_retention has marked it, the short default otherwise.

    Only a hint. `_retention` is per-process, so a restarted API or simply a
    second worker returns the short default for a conversation that was already
    granted the long one. That is why every expiry write below goes through
    `_extend_only`, which never shortens what is already stored -- retention has
    to be a property of the data, not of whichever process saw the turn.
    """
    return _retention.get(session_id, _base_ttl())


def _extend_only(pipeline, key: str, seconds: int) -> None:
    """Queue an expiry that can lengthen a key's life but never shorten it.

    Two commands, and both are needed. `GT` alone is a trap: Redis treats a key
    with no expiry as having an infinite one, so `GT` refuses to set the first
    expiry and the key would live forever. `NX` covers exactly that case -- a
    key that has no TTL yet, such as a conversation's first message -- and `GT`
    covers every refresh after it.
    """
    pipeline.expire(key, seconds, nx=True)
    pipeline.expire(key, seconds, gt=True)


async def _refresh_expiry(redis, key: str, seconds: int) -> None:
    async with redis.pipeline(transaction=False) as pipeline:
        _extend_only(pipeline, key, seconds)
        await pipeline.execute()


async def extend_retention(session_id: str, seconds: int) -> None:
    """Keep this conversation for `seconds` from now -- called for every turn
    a signed-in account owns, so their history stops expiring in two hours."""
    seconds = int(seconds)
    if seconds <= _base_ttl():
        return
    _retention[session_id] = seconds
    redis = await get_redis()
    if redis is not None:
        async with redis.pipeline(transaction=False) as pipeline:
            _extend_only(pipeline, f"chat_history:{session_id}", seconds)
            _extend_only(pipeline, f"chat_context:{session_id}", seconds)
            await pipeline.execute()
        return
    _last_seen[session_id] = time.time()


def _prune_expired() -> None:
    now = time.time()
    expired = [sid for sid, ts in _last_seen.items() if now - ts > _retention.get(sid, _base_ttl())]
    for sid in expired:
        _sessions.pop(sid, None)
        _contexts.pop(sid, None)
        lock = _session_locks.get(sid)
        if lock is None or not lock.locked():
            _session_locks.pop(sid, None)
        _last_seen.pop(sid, None)
        _retention.pop(sid, None)
    overflow = len(_sessions) - settings.memory_session_max_entries + 1
    if overflow > 0:
        oldest = sorted(_last_seen, key=_last_seen.get)[:overflow]
        for sid in oldest:
            _sessions.pop(sid, None)
            _contexts.pop(sid, None)
            lock = _session_locks.get(sid)
            if lock is None or not lock.locked():
                _session_locks.pop(sid, None)
            _last_seen.pop(sid, None)


def _entry(role: str, content: str, metadata: dict | None = None) -> dict:
    entry = {"role": role, "content": content, "ts": time.time()}
    if metadata:
        entry.update(metadata)
    return entry


async def append_turn(
    session_id: str,
    role: str,
    content: str,
    metadata: dict | None = None,
) -> None:
    entry = _entry(role, content, metadata)
    redis = await get_redis()
    if redis is not None:
        key = f"chat_history:{session_id}"
        await redis.rpush(key, json.dumps(entry))
        await redis.ltrim(key, -_MAX_DISPLAY_MESSAGES, -1)
        await _refresh_expiry(redis, key, _ttl_for(session_id))
        return
    _prune_expired()
    history = _sessions.setdefault(session_id, deque(maxlen=_MAX_DISPLAY_MESSAGES))
    history.append(entry)
    _last_seen[session_id] = time.time()


def _parse_entry(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Legacy plain "role: content" format from before structured storage.
        role, _, content = raw.partition(": ")
        return {"role": role, "content": content, "ts": None}


async def get_messages(session_id: str) -> list[dict]:
    """Return all retained messages for display, oldest first."""
    redis = await get_redis()
    if redis is not None:
        raw = await redis.lrange(f"chat_history:{session_id}", 0, -1)
        return [_parse_entry(item) for item in raw]
    _prune_expired()
    if session_id in _last_seen:
        _last_seen[session_id] = time.time()
    return list(_sessions.get(session_id, []))


async def history_text(session_id: str) -> str:
    """Return recent turns within both message and character token proxies."""
    return history_text_from_messages(await get_messages(session_id))


def history_text_from_messages(messages: list[dict]) -> str:
    """Build bounded prompt history from one consistent message snapshot."""
    recent = messages[-settings.max_context_messages :]
    remaining = settings.max_context_chars
    selected: list[str] = []
    for message in reversed(recent):
        line = f"{message['role']}: {message['content']}"
        if len(line) > remaining:
            if not selected and remaining > 40:
                selected.append(line[: remaining - 1].rstrip() + "…")
            break
        selected.append(line)
        remaining -= len(line) + 1
    return "\n".join(reversed(selected))


async def get_session_snapshot(session_id: str) -> tuple[list[dict], dict]:
    """Read messages and canonical context as one logical snapshot.

    Callers that mutate the session must hold its turn lease while using this.
    """
    messages = await get_messages(session_id)
    redis = await get_redis()
    if redis is not None:
        raw = await redis.get(f"chat_context:{session_id}")
        if raw:
            try:
                return messages, json.loads(raw)
            except json.JSONDecodeError:
                pass
        return messages, _context_from_messages(messages)
    _prune_expired()
    return messages, dict(_contexts.get(session_id) or _context_from_messages(messages))


def _context_from_messages(messages: list[dict]) -> dict:
    """Recover canonical state for sessions created before context snapshots."""
    context: dict = {"revision": 0, "focus": None, "active_workflow": None}
    for message in messages:
        if message.get("role") == "user":
            context["revision"] += 1
            content = str(message.get("content") or "")
            if re.match(
                r"^\s*(?:cancel|dismiss|discard|stop|forget|never\s+mind)\b",
                content,
                re.IGNORECASE,
            ):
                context["active_workflow"] = None
            continue
        capsules = message.get("context_capsules") or []
        if capsules:
            # Match advance_session_context exactly so recovery after Redis
            # eviction cannot change a token focus into a wallet focus.
            context["focus"] = next(
                (item for item in reversed(capsules) if item.get("kind") == "token"),
                capsules[-1],
            )
        lock = message.get("intent_lock")
        if lock:
            workflow_intent = message.get("intent")
            if workflow_intent not in {"trade", "cross_chain_swap"}:
                workflow_intent = "cross_chain_swap" if lock.get("source_chain") != lock.get("destination_chain") else "trade"
            context["active_workflow"] = {
                "intent": workflow_intent,
                "status": "pending_approval",
                "plan_id": (message.get("trade_plan") or {}).get("plan_id"),
                **lock,
            }
        context["last_intent"] = message.get("intent") or context.get("last_intent")
        context["last_capabilities"] = message.get("capabilities") or context.get("last_capabilities", [])
    return context


async def get_session_context(session_id: str) -> dict:
    redis = await get_redis()
    if redis is not None:
        raw = await redis.get(f"chat_context:{session_id}")
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass
        return _context_from_messages(await get_messages(session_id))
    _prune_expired()
    if session_id in _contexts:
        return dict(_contexts[session_id])
    return _context_from_messages(await get_messages(session_id))


async def save_session_context(session_id: str, context: dict) -> None:
    redis = await get_redis()
    if redis is not None:
        key = f"chat_context:{session_id}"
        # keepttl, then extend-only: overwriting the value must not silently
        # reset a signed-in account's retention to the short default.
        await redis.set(key, json.dumps(context), keepttl=True)
        await _refresh_expiry(redis, key, _ttl_for(session_id))
        return
    _prune_expired()
    _contexts[session_id] = dict(context)
    _last_seen[session_id] = time.time()


async def commit_turn(
    session_id: str,
    user_content: str,
    assistant_content: str,
    assistant_metadata: dict,
    context: dict,
) -> None:
    """Commit the user turn, assistant artifacts, and routing state together."""
    user_entry = _entry("user", user_content)
    assistant_entry = _entry("assistant", assistant_content, assistant_metadata)
    redis = await get_redis()
    if redis is not None:
        history_key = f"chat_history:{session_id}"
        context_key = f"chat_context:{session_id}"
        async with redis.pipeline(transaction=True) as pipeline:
            pipeline.rpush(history_key, json.dumps(user_entry), json.dumps(assistant_entry))
            pipeline.ltrim(history_key, -_MAX_DISPLAY_MESSAGES, -1)
            pipeline.set(context_key, json.dumps(context), keepttl=True)
            _extend_only(pipeline, history_key, _ttl_for(session_id))
            _extend_only(pipeline, context_key, _ttl_for(session_id))
            await pipeline.execute()
        return
    _prune_expired()
    history = _sessions.setdefault(session_id, deque(maxlen=_MAX_DISPLAY_MESSAGES))
    history.extend((user_entry, assistant_entry))
    _contexts[session_id] = dict(context)
    _last_seen[session_id] = time.time()


async def clear_history(session_id: str) -> None:
    redis = await get_redis()
    if redis is not None:
        await redis.delete(f"chat_history:{session_id}", f"chat_context:{session_id}")
        return
    _sessions.pop(session_id, None)
    _contexts.pop(session_id, None)
    _last_seen.pop(session_id, None)
