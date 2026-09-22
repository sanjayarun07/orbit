"""Durable jobs: work that outlives one request (docs/durable-jobs-spec.md).

A job is a row that carries its own plan, working state, evidence, typed
signals and an idempotent operation cache. One worker per replica claims
due jobs with a compare-and-swap on (id, status, lease_id), heartbeats the
lease, and stops at the next guard when the lease is gone. Every write to a
running job is that same compare-and-swap: the row is the lock, so two
replicas need no other coordination. A lost lease is not a failure -- the
job goes back to `queued` with everything intact and the next claim resumes
from the last checkpoint. Cached operations are returned without a call,
so a resume never repeats a paid call.

States: queued -> running -> succeeded | failed | cancelled, with
waiting_input / waiting_approval as pauses and scheduled for recurring
work. Handlers are registered per kind and must be restartable from any
checkpoint: read the plan, skip finished steps.

Storage follows the repo contract: Postgres tables `jobs` and `job_events`
(cascading with the user), in-process dicts when memory is the store.
The engine never signs anything; an approval is a trade plan the user
confirms in their wallet.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from app.db import apply_schema, get_pg_pool, memory_is_the_store
from app.settings import settings

logger = logging.getLogger(__name__)

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    account_id TEXT,
    session_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    spec JSONB NOT NULL DEFAULT '{}'::jsonb,
    plan JSONB NOT NULL DEFAULT '[]'::jsonb,
    state JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    operations JSONB NOT NULL DEFAULT '{}'::jsonb,
    signals JSONB NOT NULL DEFAULT '[]'::jsonb,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_id TEXT,
    lease_until TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ,
    question TEXT,
    action_id TEXT,
    result JSONB,
    receipt_id TEXT,
    error TEXT,
    attached BOOLEAN NOT NULL DEFAULT TRUE,
    delivered BOOLEAN NOT NULL DEFAULT FALSE,
    delivering_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at TIMESTAMPTZ
);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS delivering_until TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS jobs_user_idx ON jobs (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_due_idx ON jobs (status, next_run_at, lease_until);
CREATE TABLE IF NOT EXISTS job_events (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS job_events_job_idx ON job_events (job_id, at);
"""

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
PAUSED = frozenset({"waiting_input", "waiting_approval"})
MAX_PLAN_STEPS = 12
MAX_ACTIVE = 3
TICK_SECONDS = 1.0
_JSON = ("spec", "plan", "state", "evidence", "operations", "signals", "result")
_TS = ("lease_until", "next_run_at", "created_at", "updated_at", "settled_at", "delivering_until")

_ready = False
_memory: dict[str, dict] = {}
_events: dict[str, list[dict]] = {}
_lock = threading.Lock()
_handlers: dict[str, Callable[["dict", "JobContext"], Awaitable[dict]]] = {}
_active: dict[str, asyncio.Task] = {}
_worker_running = False
_settled: dict[str, asyncio.Event] = {}      # attach() waits on these in-process

# The turn that creates a job: who owns it, who pays, which conversation
# receives the answer. Bound by execution_policy next to the receipt owner.
current_owner: ContextVar[tuple[str | None, str | None, str | None]] = ContextVar("jobs_owner", default=(None, None, None))


class LostLease(RuntimeError):
    """The lease is gone: paused, cancelled, taken over, or the account deleted."""


class StoreUnavailable(RuntimeError):
    """Postgres is configured and cannot be reached: a durable job cannot be
    created (review, 2026-09-22: memory silently stood in and the job
    vanished on restart, or became unfindable once the database returned).
    Callers that must still answer run an ephemeral job (`run_ephemeral`)."""


def bind_turn(user_id: str | None, account_id: str | None, session_id: str | None):
    return current_owner.set((user_id, account_id, session_id))


def register(kind: str, handler: Callable[["dict", "JobContext"], Awaitable[dict]]) -> None:
    _handlers[kind] = handler


def reset_for_test() -> None:
    global _ready, _worker_running
    with _lock:
        _memory.clear()
        _events.clear()
    _active.clear()
    _settled.clear()
    _ready = False
    _worker_running = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def _pool():
    """The pool, None when memory is the configured store, and StoreUnavailable
    when Postgres is configured but down: durable jobs never fall back."""
    global _ready
    try:
        pool = await get_pg_pool()
    except Exception as exc:
        if memory_is_the_store():
            return None
        raise StoreUnavailable(f"jobs store unavailable: {type(exc).__name__}") from exc
    if pool is None:
        if memory_is_the_store():
            return None
        raise StoreUnavailable("jobs store unavailable: no database connection")
    if not _ready:
        try:
            await apply_schema(pool, _TABLE_SQL)
            _ready = True
        except Exception as exc:
            raise StoreUnavailable(f"jobs schema not ready: {type(exc).__name__}") from exc
    return pool


def _from_db(r) -> dict:
    row = dict(r)
    row["id"] = str(row["id"]).replace("-", "")
    row["user_id"] = str(row["user_id"]) if row.get("user_id") else None
    for key in _JSON:
        value = row.get(key)
        if isinstance(value, str):
            row[key] = json.loads(value)
    for key in _TS:
        if isinstance(row.get(key), datetime):
            row[key] = row[key].isoformat()
    return row


def _new_row(kind: str, spec: dict, *, user_id: str | None, account_id: str | None, session_id: str | None,
             next_run_at: datetime | None = None) -> dict:
    now = _now()
    return {
        "id": uuid.uuid4().hex, "user_id": user_id, "account_id": account_id, "session_id": session_id, "kind": kind,
        "status": "scheduled" if next_run_at else "queued", "spec": dict(spec or {}), "plan": [], "state": {}, "evidence": [],
        "operations": {}, "signals": [], "attempts": 0, "lease_id": None, "lease_until": None, "next_run_at": _iso(next_run_at),
        "question": None, "action_id": None, "result": None, "receipt_id": None, "error": None, "attached": True, "delivered": False, "delivering_until": None,
        "created_at": now.isoformat(), "updated_at": now.isoformat(), "settled_at": None,
    }


# ----------------------------------------------------------------------------
# store: create, read, compare-and-swap
# ----------------------------------------------------------------------------

async def create(kind: str, spec: dict, *, user_id: str | None = None, account_id: str | None = None,
                 session_id: str | None = None, next_run_at: datetime | None = None) -> dict:
    if kind not in _handlers:
        raise ValueError(f"no handler registered for job kind {kind!r}")
    row = _new_row(kind, spec, user_id=user_id, account_id=account_id, session_id=session_id, next_run_at=next_run_at)
    pool = await _pool()
    if pool is not None:
        await pool.execute(
            "INSERT INTO jobs (id, user_id, account_id, session_id, kind, status, spec, next_run_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)",
            uuid.UUID(row["id"]), row["user_id"], row["account_id"], row["session_id"], kind, row["status"], json.dumps(row["spec"]), next_run_at)
    else:
        with _lock:
            _memory[row["id"]] = row
            row = json.loads(json.dumps(row))          # a snapshot, never the store's own object
    await _event(row["id"], "created", f"{kind} queued")
    return row


async def get(job_id: str) -> dict | None:
    pool = await _pool()
    if pool is not None:
        r = await pool.fetchrow("SELECT * FROM jobs WHERE id = $1", uuid.UUID(job_id))
        return _from_db(r) if r else None
    with _lock:
        row = _memory.get(job_id)
        return json.loads(json.dumps(row)) if row else None


async def list_for(user_id: str, limit: int | None = 50) -> list[dict]:
    pool = await _pool()
    if pool is not None:
        sql = "SELECT * FROM jobs WHERE user_id = $1 ORDER BY created_at DESC"
        rows = await (pool.fetch(sql, user_id) if limit is None else pool.fetch(sql + " LIMIT $2", user_id, limit))
        return [_from_db(r) for r in rows]
    with _lock:
        mine = [json.loads(json.dumps(r)) for r in _memory.values() if r.get("user_id") == user_id]
    mine.sort(key=lambda r: r["created_at"], reverse=True)
    return mine if limit is None else mine[:limit]


async def events(job_id: str) -> list[dict]:
    pool = await _pool()
    if pool is not None:
        rows = await pool.fetch("SELECT id, at, kind, title, detail FROM job_events WHERE job_id = $1 ORDER BY at", uuid.UUID(job_id))
        return [{"id": str(r["id"]).replace("-", ""), "at": r["at"].isoformat(), "kind": r["kind"], "title": r["title"], "detail": r["detail"]} for r in rows]
    with _lock:
        return [dict(e) for e in _events.get(job_id, [])]


async def _event(job_id: str, kind: str, title: str, detail: str | None = None) -> None:
    pool = await _pool()
    entry = {"id": uuid.uuid4().hex, "at": _now().isoformat(), "kind": kind, "title": title[:200], "detail": (detail or "")[:4000] or None}
    if pool is not None:
        await pool.execute("INSERT INTO job_events (id, job_id, kind, title, detail) VALUES ($1, $2, $3, $4, $5)",
                           uuid.UUID(entry["id"]), uuid.UUID(job_id), kind, entry["title"], entry["detail"])
        return
    with _lock:
        _events.setdefault(job_id, []).append(entry)


_MERGE = ("operations",)                 # dict columns merged key by key
_APPEND = ("evidence", "signals")          # list columns appended to


async def cas(job_id: str, expected: dict, patch: dict, merge: dict | None = None, append: dict | None = None) -> dict | None:
    """Apply `patch` only if every `expected` column still holds; the fresh row,
    or None when someone else got there first. `merge` adds keys into a dict
    column and `append` adds items to a list column atomically in the
    database (jsonb ||), so concurrent checkpoints never overwrite each
    other's entries (review, 2026-09-22: two concurrent operations kept one)."""
    patch = {**patch, "updated_at": _now()}
    merge, append = dict(merge or {}), dict(append or {})
    pool = await _pool()
    if pool is not None:
        sets, args, n = [], [], 1
        for key, value in patch.items():
            if key in _JSON:
                sets.append(f"{key} = ${n}::jsonb"); args.append(json.dumps(value) if value is not None else None)
            else:
                sets.append(f"{key} = ${n}"); args.append(value)
            n += 1
        for key, value in merge.items():
            sets.append(f"{key} = COALESCE({key}, '{{}}'::jsonb) || ${n}::jsonb"); args.append(json.dumps(value)); n += 1
        for key, value in append.items():
            sets.append(f"{key} = COALESCE({key}, '[]'::jsonb) || ${n}::jsonb"); args.append(json.dumps(list(value))); n += 1
        wheres = [f"id = ${n}"]; args.append(uuid.UUID(job_id)); n += 1
        for key, value in expected.items():
            wheres.append(f"{key} IS NOT DISTINCT FROM ${n}"); args.append(value); n += 1
        r = await pool.fetchrow(f"UPDATE jobs SET {', '.join(sets)} WHERE {' AND '.join(wheres)} RETURNING *", *args)
        return _from_db(r) if r else None
    return _memory_cas(job_id, expected, patch, merge, append)


def _memory_cas(job_id: str, expected: dict, patch: dict, merge: dict, append: dict) -> dict | None:
    expected = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in expected.items()}   # rows keep ISO strings
    with _lock:
        row = _memory.get(job_id)
        if row is None or any(row.get(k) != v for k, v in expected.items()):
            return None
        for key, value in patch.items():
            row[key] = value.isoformat() if isinstance(value, datetime) else (json.loads(json.dumps(value)) if key in _JSON and value is not None else value)
        for key, value in merge.items():
            row[key] = {**(row.get(key) or {}), **json.loads(json.dumps(value))}
        for key, value in append.items():
            row[key] = [*(row.get(key) or []), *json.loads(json.dumps(list(value)))]
        return json.loads(json.dumps(row))


async def _set(job_id: str, **patch) -> dict | None:
    """An unguarded update, for columns no lease owns (attached, delivered)."""
    return await cas(job_id, {}, patch)


# ----------------------------------------------------------------------------
# the handler's context
# ----------------------------------------------------------------------------

class JobContext:
    def __init__(self, job: dict, lease_id: str):
        self.job = job
        self.lease_id = lease_id
        self.signal = asyncio.Event()

    @property
    def id(self) -> str:
        return self.job["id"]

    _ephemeral = False

    async def guard(self) -> None:
        """Raise LostLease unless this lease still owns a running job whose
        owner still exists."""
        if self.signal.is_set():
            raise LostLease("cancelled")
        if self._ephemeral:
            return
        latest = await get(self.id)
        if latest is None or latest.get("lease_id") != self.lease_id or latest.get("status") != "running":
            self.signal.set()
            raise LostLease("lease lost")
        if latest.get("user_id"):
            from app import turn_log
            if await turn_log.is_deleted(str(latest["user_id"])):
                self.signal.set()
                raise LostLease("account deleted")
        self.job = latest

    async def checkpoint(self, merge: dict | None = None, append: dict | None = None, **patch) -> dict:
        if self.signal.is_set():
            raise LostLease("cancelled")
        if self._ephemeral:
            fresh = _memory_cas(self.id, {}, {**patch, "updated_at": _now()}, merge or {}, append or {})
            self.job = fresh or self.job
            return self.job
        fresh = await cas(self.id, {"lease_id": self.lease_id, "status": "running"}, patch, merge=merge, append=append)
        if fresh is None:
            self.signal.set()
            raise LostLease("lease lost at checkpoint")
        self.job = fresh
        return fresh

    async def event(self, kind: str, title: str, detail: str | None = None) -> None:
        await self.guard()
        if self._ephemeral:
            return
        await _event(self.id, kind, title, detail)

    async def plan(self, steps: list[str]) -> list[dict]:
        """Set the plan once; a resumed job keeps the plan it has."""
        if self.job.get("plan"):
            return self.job["plan"]
        plan = [{"id": str(i), "title": t, "status": "pending", "started_at": None, "finished_at": None, "note": None}
                for i, t in enumerate(steps[:MAX_PLAN_STEPS])]
        await self.checkpoint(plan=plan)
        return plan

    def done(self, step_id: str) -> bool:
        return any(s["id"] == step_id and s["status"] == "done" for s in self.job.get("plan") or [])

    async def step(self, step_id: str, title: str | None = None) -> None:
        plan = [dict(s) for s in self.job.get("plan") or []]
        for s in plan:
            if s["id"] == step_id and s["status"] != "done":
                s["status"], s["started_at"] = "running", _now().isoformat()
        await self.checkpoint(plan=plan)
        if not self._ephemeral:
            await _event(self.id, "step", title or next((s["title"] for s in plan if s["id"] == step_id), step_id))

    async def finish_step(self, step_id: str, note: str | None = None) -> None:
        plan = [dict(s) for s in self.job.get("plan") or []]
        for s in plan:
            if s["id"] == step_id:
                s["status"], s["finished_at"], s["note"] = "done", _now().isoformat(), note
        await self.checkpoint(plan=plan)

    async def call(self, name: str, factory: Callable[[], Awaitable[Any] | Any], *, args: Any = None, volatile: bool = False,
                   cost_usd: float = 0.0) -> Any:
        """The operation cache. A cached result comes back without a call and
        writes no event; a fresh one is made, cached with its cost at once, and
        checkpointed. Price-class results (volatile) are refetched past
        `job_volatile_max_age_seconds`; everything else lives with the job."""
        key = hashlib.sha256(f"{name}:{json.dumps(args, sort_keys=True, default=str)}".encode()).hexdigest()
        cached = (self.job.get("operations") or {}).get(key)
        if cached is not None:
            age = (_now() - datetime.fromisoformat(cached["at"])).total_seconds()
            if not volatile or age <= settings.job_volatile_max_age_seconds:
                return cached["result"]
        await self.guard()
        result = factory()
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            result = await result
        entry = {"name": name, "result": _jsonable(result), "at": _now().isoformat(), "cost_usd": cost_usd, "volatile": volatile}
        await self.checkpoint(merge={"operations": {key: entry}})       # one key, merged; concurrent calls keep each other's
        return entry["result"]

    async def add_evidence(self, item: dict) -> None:
        await self.checkpoint(append={"evidence": [item]})

    async def add_signal(self, signal: Any) -> None:
        dumped = signal.model_dump(mode="json") if hasattr(signal, "model_dump") else signal
        await self.checkpoint(append={"signals": [_jsonable(dumped)]})

    async def ask(self, question: str) -> None:
        """Pause for the user: the question goes to the conversation, the
        user's next message in it answers, and the job is requeued."""
        await self.checkpoint(status="waiting_input", question=question, lease_id=None, lease_until=None)
        await _event(self.id, "paused", "Waiting for an answer", question)
        self.signal.set()
        raise _Paused()

    async def approve(self, action_id: str) -> None:
        """Pause for a wallet approval of the named trade plan; the plan's own
        TTL is the expiry."""
        await self.checkpoint(status="waiting_approval", action_id=action_id, lease_id=None, lease_until=None)
        await _event(self.id, "paused", "Waiting for approval", action_id)
        self.signal.set()
        raise _Paused()


class _Paused(Exception):
    """Raised inside a handler by ask()/approve() to unwind it cleanly."""


def _jsonable(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=_default))
    except (TypeError, ValueError):
        return str(value)


def _default(value: Any):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dataclass_fields__"):
        import dataclasses
        return dataclasses.asdict(value)
    return str(value)


# ----------------------------------------------------------------------------
# claiming and running
# ----------------------------------------------------------------------------

async def due(now: datetime | None = None, limit: int = MAX_ACTIVE) -> list[dict]:
    now = now or _now()
    pool = await _pool()
    if pool is not None:
        rows = await pool.fetch(
            "SELECT * FROM jobs WHERE status = 'queued' OR (status = 'scheduled' AND next_run_at <= $1) "
            "OR (status = 'running' AND lease_until < $1) ORDER BY created_at LIMIT $2", now, limit)
        return [_from_db(r) for r in rows]
    with _lock:
        rows = [json.loads(json.dumps(r)) for r in _memory.values()]
    out = []
    for r in sorted(rows, key=lambda r: r["created_at"]):
        if r["status"] == "queued" or (r["status"] == "scheduled" and r["next_run_at"] and datetime.fromisoformat(r["next_run_at"]) <= now) \
                or (r["status"] == "running" and r["lease_until"] and datetime.fromisoformat(r["lease_until"]) < now):
            out.append(r)
        if len(out) >= limit:
            break
    return out


async def claim(row: dict, lease_id: str | None = None) -> tuple[dict, str] | None:
    """Take the lease by compare-and-swap on the row as last seen. None when
    another worker got it, or it moved on."""
    lease_id = lease_id or uuid.uuid4().hex
    expected = {"status": row["status"], "lease_id": row.get("lease_id")}
    if row["status"] == "running":
        expected["lease_until"] = datetime.fromisoformat(row["lease_until"]) if row.get("lease_until") else None
    fresh = await cas(row["id"], expected, {"status": "running", "lease_id": lease_id,
                                           "lease_until": _now() + timedelta(seconds=settings.job_lease_seconds),
                                           "attempts": int(row.get("attempts") or 0) + 1})
    return (fresh, lease_id) if fresh else None


async def run(row: dict) -> dict | None:
    """Claim and run one job to its next pause or settlement. Returns the
    row as left, or None when the claim failed."""
    claimed = await claim(row)
    if claimed is None:
        return None
    job, lease_id = claimed
    ctx = JobContext(job, lease_id)
    handler = _handlers.get(job["kind"])
    if handler is None:
        return await _settle(ctx, "failed", error=f"no handler for kind {job['kind']!r}")
    await _event(job["id"], "resumed" if job["attempts"] > 1 else "started", f"attempt {job['attempts']}")
    heartbeat = asyncio.create_task(_heartbeat(ctx))
    from app import decision_records, evidence as _evidence
    from app.integrations import tradingview

    envelopes = _evidence.start_turn()          # every tool the handler calls records beside its card
    # The job runs as its owner: their TradingView connection and their
    # receipts, exactly as the request that started it (review, 2026-09-22).
    # An owned job binds its owner; an anonymous job keeps whatever the
    # caller bound (an inline run inside a request keeps that request's).
    owner = job.get("user_id")
    tv_bound = await tradingview.bind_turn(owner) if owner else None
    receipts_bound = decision_records.bind_turn(owner) if owner else None
    try:
        result = await handler(job, ctx)
        recorded = [e.public() for e in _evidence.collected()]
        if recorded:
            await ctx.checkpoint(append={"evidence": recorded})
        return await _settle(ctx, "succeeded", result=result)
    except _Paused:
        return await get(job["id"])
    except LostLease as exc:
        # Not a failure: back to the queue with state intact, unless the
        # account is gone, in which case there is nothing to keep.
        if str(exc) == "account deleted":
            await cas(job["id"], {"lease_id": lease_id}, {"status": "cancelled", "lease_id": None, "lease_until": None, "settled_at": _now()})
            await scrub_user(str(job.get("user_id") or ""))
            return await get(job["id"])
        await cas(job["id"], {"lease_id": lease_id, "status": "running"}, {"status": "queued", "lease_id": None, "lease_until": None})
        return await get(job["id"])
    except asyncio.CancelledError:
        await cas(job["id"], {"lease_id": lease_id, "status": "running"}, {"status": "queued", "lease_id": None, "lease_until": None})
        raise
    except Exception as exc:  # noqa: BLE001 - a handler error is the job's to record
        logger.warning("job %s (%s) failed", job["id"], job["kind"], exc_info=True)
        await _event(job["id"], "error", "Attempt failed", f"{type(exc).__name__}: {exc}"[:2000])
        exhausted = job["attempts"] >= settings.job_max_attempts
        if exhausted:
            return await _settle(ctx, "failed", error=f"{type(exc).__name__}: {exc}"[:2000])
        await cas(job["id"], {"lease_id": lease_id, "status": "running"}, {"status": "queued", "lease_id": None, "lease_until": None,
                                                                          "error": f"{type(exc).__name__}: {exc}"[:2000]})
        return await get(job["id"])
    finally:
        if tv_bound is not None:
            tradingview.current_token.reset(tv_bound)
        if receipts_bound is not None:
            decision_records.current_user.reset(receipts_bound)
        _evidence.end_turn(envelopes)
        heartbeat.cancel()
        _active.pop(job["id"], None)


async def _heartbeat(ctx: JobContext) -> None:
    period = max(1.0, settings.job_lease_seconds / 3)
    try:
        while not ctx.signal.is_set():
            await asyncio.sleep(period)
            fresh = await cas(ctx.id, {"lease_id": ctx.lease_id, "status": "running"},
                              {"lease_until": _now() + timedelta(seconds=settings.job_lease_seconds)})
            if fresh is None:
                ctx.signal.set()
                return
    except asyncio.CancelledError:
        return


async def _settle(ctx: JobContext, status: str, *, result: dict | None = None, error: str | None = None) -> dict | None:
    patch: dict = {"status": status, "lease_id": None, "lease_until": None, "settled_at": _now(), "error": error}
    if result is not None:
        patch["result"] = _jsonable(result)
        if isinstance(result, dict) and result.get("receipt_id"):
            patch["receipt_id"] = result["receipt_id"]
        # A recurring handler returns next_run_at: the row goes back to
        # `scheduled` with its plan and cache intact, one row for every run.
        next_at = result.get("next_run_at") if isinstance(result, dict) else None
        if status == "succeeded" and next_at:
            patch.update({"status": "scheduled", "next_run_at": datetime.fromisoformat(next_at) if isinstance(next_at, str) else next_at,
                          "settled_at": None, "plan": [], "attempts": 0})
            status = "scheduled"
    fresh = await cas(ctx.id, {"lease_id": ctx.lease_id, "status": "running"}, patch)
    if fresh is None:
        return await get(ctx.id)
    await _event(ctx.id, "settled" if status != "scheduled" else "rescheduled", status, error)
    if status == "scheduled":
        return fresh
    await deliver(fresh)
    _notify_settled(ctx.id)
    return await get(ctx.id) or fresh            # as delivered, not as settled


def _notify_settled(job_id: str) -> None:
    ev = _settled.get(job_id)
    if ev is not None:
        ev.set()


async def deliver(job: dict) -> None:
    """A settled job that outlived its request: the answer goes to the
    conversation and the credits are charged once, keyed by the job id.
    Idempotent through `delivered`."""
    if job.get("status") not in TERMINAL or job.get("delivered") or (job.get("attached", True) and not _attachment_expired(job)):
        return
    from app import credits, sessions

    # Claim the delivery with an EXPIRING claim (delivering_until), so two
    # deliverers never both append, and a crash after the claim is not a lost
    # answer: the claim lapses and the next pass retries. `delivered` is set
    # only after the write succeeded (review, 2026-09-22).
    until = job.get("delivering_until")
    if until and datetime.fromisoformat(until) > _now():
        return
    claimed = await cas(job["id"], {"delivered": False, "status": job["status"], "delivering_until": _parse_ts(until)},
                        {"delivering_until": _now() + timedelta(seconds=DELIVERY_CLAIM_SECONDS)})
    if claimed is None:
        return
    answer = ((job.get("result") or {}).get("answer") if isinstance(job.get("result"), dict) else None) or (
        f"The {job['kind'].replace('_', ' ')} could not be completed: {job.get('error') or job['status']}.")
    try:
        billed_by_turn = False
        if job.get("session_id"):
            existing = next((m for m in await sessions.get_messages(job["session_id"]) if m.get("job_id") == job["id"]), None)
            if existing is None:
                await sessions.append_turn(job["session_id"], "assistant", answer, {"job_id": job["id"], "kind": job["kind"], "delivered_by": "job"})
            else:
                # A message the attached TURN committed was charged as that
                # turn. One this delivery wrote on an earlier attempt was not
                # necessarily charged: the origin decides, not the presence
                # (review, 2026-09-22: a failed charge was skipped on retry).
                billed_by_turn = existing.get("delivered_by") != "job"
        if not billed_by_turn and job.get("account_id") and job.get("status") == "succeeded":
            cost = _cost_of(job["kind"])
            if cost:
                await credits.charge_once(job["account_id"], cost, f"job:{job['kind']}", "job", job["id"], {"kind": job["kind"]})
    except Exception:
        logger.warning("job %s: delivery failed; will retry", job["id"], exc_info=True)
        await cas(job["id"], {"delivered": False}, {"delivering_until": None})
        return
    await cas(job["id"], {"delivered": False}, {"delivered": True, "delivering_until": None})


DELIVERY_CLAIM_SECONDS = 120.0


def _parse_ts(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _attachment_expired(job: dict) -> bool:
    """A request cannot still be attached past the attach window plus a
    grace: a cancelled request or a restarted process left `attached` true
    (review, 2026-09-22), and the answer must still be delivered."""
    try:
        started = datetime.fromisoformat(job["created_at"])
    except (KeyError, TypeError, ValueError):
        return True
    return (_now() - started).total_seconds() > settings.job_attach_seconds + 30


def _cost_of(kind: str) -> int:
    return {"deep_dive": settings.credit_cost_deep_dive, "desk": settings.credit_cost_team_turn}.get(kind, settings.credit_cost_tool_turn)


# ----------------------------------------------------------------------------
# attaching a request, answering, approving, cancelling
# ----------------------------------------------------------------------------

async def attach(job_id: str, timeout: float | None = None, on_event: Callable[[dict], None] | None = None) -> dict:
    """Wait for the job to settle for up to `timeout` seconds, reporting each
    new event through `on_event`. Without a worker in this process (tests, a
    bare dev server) the job runs inline. Returns the row as it stands;
    a still-running row means the request has detached and `deliver` will
    finish the story."""
    timeout = settings.job_attach_seconds if timeout is None else timeout
    ev = _settled.setdefault(job_id, asyncio.Event())
    seen = 0

    async def report() -> None:
        nonlocal seen
        if on_event is None:
            return
        for entry in (await events(job_id))[seen:]:
            seen += 1
            if entry["kind"] in ("step", "paused", "error"):
                on_event(entry)

    try:
        if not _worker_running:
            row = await get(job_id)
            if row and row["status"] in ("queued", "scheduled"):
                await run(row)
            await report()
            return await get(job_id) or {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await report()
            row = await get(job_id)
            if row is None or row["status"] in TERMINAL or row["status"] in PAUSED:
                return row or {}
            try:
                await asyncio.wait_for(ev.wait(), timeout=min(1.0, max(0.05, deadline - time.monotonic())))
            except asyncio.TimeoutError:
                pass
        row = await _set(job_id, attached=False)
        await _event(job_id, "detached", "The request moved on; the answer will be appended to the conversation")
        return row or {}
    except asyncio.CancelledError:
        # The request was cancelled (client gone, shutdown): the job keeps
        # running and its answer is delivered to the conversation. A job
        # that had already settled was acknowledged for this request, which
        # never returned it: hand it back to delivery.
        try:
            await asyncio.shield(_detach_unacknowledged(job_id))
        except Exception:
            logger.warning("job %s: could not detach on cancellation", job_id, exc_info=True)
        raise
    finally:
        _settled.pop(job_id, None)


async def acknowledge(job_id: str) -> dict | None:
    """The chat turn has COMMITTED this job's answer to the conversation and
    charged it as its own turn: mark it delivered, so the attachment's expiry
    never delivers and charges it again. Called after the commit, never
    before (review, 2026-09-22: acknowledging at attach lost the answer when
    the turn failed between attach and commit). A job whose turn never
    commits keeps delivered = false and maintenance delivers it once the
    attachment has expired."""
    row = await get(job_id)
    if row and row["status"] in TERMINAL and not row.get("delivered"):
        return await cas(job_id, {"delivered": False}, {"delivered": True}) or row
    return row


async def _detach_unacknowledged(job_id: str) -> None:
    row = await _set(job_id, attached=False)
    if row and row.get("status") in TERMINAL and row.get("delivered"):
        await cas(job_id, {"delivered": True}, {"delivered": False})


async def answer(job_id: str, user_id: str, text: str) -> dict | None:
    """The user's reply to a waiting_input question requeues the job with
    the answer in its state."""
    row = await get(job_id)
    if row is None or row.get("user_id") != user_id or row["status"] != "waiting_input":
        return None
    fresh = await cas(job_id, {"status": "waiting_input"}, {"status": "queued", "question": None,
                                                            "state": {**(row.get("state") or {}), "answer": text}})
    if fresh:
        await _event(job_id, "resumed", "Answer received", text[:500])
    return fresh


async def approved(job_id: str, action_id: str) -> dict | None:
    row = await get(job_id)
    if row is None or row["status"] != "waiting_approval" or row.get("action_id") != action_id:
        return None
    fresh = await cas(job_id, {"status": "waiting_approval"}, {"status": "queued", "action_id": None,
                                                               "state": {**(row.get("state") or {}), "approved": action_id}})
    if fresh:
        await _event(job_id, "resumed", "Approved", action_id)
    return fresh


async def expire_approval(job_id: str, action_id: str) -> dict | None:
    """The plan expired unconfirmed: the step failed, the job is requeued to
    ask again; the wallet was never asked twice."""
    row = await get(job_id)
    if row is None or row["status"] != "waiting_approval" or row.get("action_id") != action_id:
        return None
    fresh = await cas(job_id, {"status": "waiting_approval"}, {"status": "queued", "action_id": None,
                                                               "state": {**(row.get("state") or {}), "expired": action_id}})
    if fresh:
        await _event(job_id, "error", "Approval expired", action_id)
    return fresh


async def cancel(job_id: str, user_id: str) -> dict | None:
    row = await get(job_id)
    if row is None or row.get("user_id") != user_id or row["status"] in TERMINAL:
        return None
    fresh = await cas(job_id, {"status": row["status"]}, {"status": "cancelled", "lease_id": None, "lease_until": None, "settled_at": _now()})
    if fresh:
        await _event(job_id, "settled", "cancelled")
        task = _active.get(job_id)
        if task is not None:
            task.cancel()
        _notify_settled(job_id)
    return fresh


async def run_ephemeral(kind: str, spec: dict, *, user_id: str | None = None) -> dict:
    """Run a handler once, in memory, when the durable store is unavailable:
    no lease, no resumption, no delivery, nothing kept. The caller says so in
    its answer. Never used when the store is up."""
    handler = _handlers[kind]
    row = _new_row(kind, spec, user_id=user_id, account_id=None, session_id=None)
    row["status"], row["lease_id"], row["attempts"] = "running", "ephemeral", 1
    with _lock:
        _memory[row["id"]] = row
    try:
        ctx = JobContext(row, "ephemeral")
        ctx._ephemeral = True
        result = await handler(row, ctx)
        return {**_jsonable(result), "ephemeral": True}
    finally:
        with _lock:
            _memory.pop(row["id"], None)
            _events.pop(row["id"], None)


# ----------------------------------------------------------------------------
# the worker
# ----------------------------------------------------------------------------

async def tick(now: datetime | None = None) -> int:
    from app import execution_policy      # lazy: execution_policy imports the graph, which imports the handlers

    started = 0
    for row in await due(now):
        if row["id"] in _active or len(_active) >= MAX_ACTIVE:
            continue
        task = execution_policy.background(run(row))
        _active[row["id"]] = task
        started += 1
    return started


async def maintain() -> None:
    """Outcomes that settled without their delivery (a crash between the two
    writes) are delivered now."""
    pool = await _pool()
    cutoff = _now() - timedelta(seconds=settings.job_attach_seconds + 30)
    if pool is not None:
        rows = [_from_db(r) for r in await pool.fetch(
            "SELECT * FROM jobs WHERE status IN ('succeeded', 'failed') AND delivered = FALSE AND (attached = FALSE OR created_at < $1) "
            "AND (delivering_until IS NULL OR delivering_until < NOW()) LIMIT 20", cutoff)]
    else:
        with _lock:
            rows = [json.loads(json.dumps(r)) for r in _memory.values()
                    if r["status"] in ("succeeded", "failed") and not r.get("delivered")
                    and (not r.get("attached", True) or datetime.fromisoformat(r["created_at"]) < cutoff)]
    for row in rows:
        await deliver(row)


async def worker() -> None:
    """One per replica; the row-level lease makes replicas safe together."""
    global _worker_running
    if not settings.jobs_enabled:
        return
    _worker_running = True
    last_maintenance = 0.0
    try:
        while True:
            try:
                await tick()
                if time.monotonic() - last_maintenance > 60:
                    last_maintenance = time.monotonic()
                    await maintain()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("jobs worker tick failed", exc_info=True)
            await asyncio.sleep(TICK_SECONDS)
    finally:
        _worker_running = False


# ----------------------------------------------------------------------------
# the person's data
# ----------------------------------------------------------------------------

def public(row: dict) -> dict:
    keep = ("id", "kind", "status", "spec", "plan", "attempts", "question", "action_id", "result", "receipt_id", "error",
            "created_at", "updated_at", "settled_at")
    return {k: row.get(k) for k in keep}


async def scrub_user(user_id: str) -> int:
    """Account deletion: the person's jobs keep no words and no owner. The
    database rows cascade with the user; this covers a job that settled
    or was cancelled during the deletion, and the memory store."""
    if not user_id:
        return 0
    n = 0
    pool = await _pool()
    if pool is not None:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Events carry answers and error text: gone before the rows
                # lose their owner (review, 2026-09-22), in one transaction.
                await conn.execute("DELETE FROM job_events WHERE job_id IN (SELECT id FROM jobs WHERE user_id = $1::uuid)", user_id)
                status = await conn.execute(
                    "UPDATE jobs SET spec = '{}'::jsonb, plan = '[]'::jsonb, state = '{}'::jsonb, evidence = '[]'::jsonb, operations = '{}'::jsonb, "
                    "signals = '[]'::jsonb, result = NULL, question = NULL, error = NULL, user_id = NULL, account_id = NULL, session_id = NULL, "
                    "status = CASE WHEN status IN ('succeeded', 'failed', 'cancelled') THEN status ELSE 'cancelled' END WHERE user_id = $1::uuid", user_id)
        n = int(status.split()[-1]) if status and status.split()[-1].isdigit() else 0
    with _lock:
        for row in _memory.values():
            if row.get("user_id") == user_id:
                row.update({"spec": {}, "plan": [], "state": {}, "evidence": [], "operations": {}, "signals": [], "result": None,
                            "question": None, "error": None, "user_id": None, "account_id": None, "session_id": None,
                            "status": row["status"] if row["status"] in TERMINAL else "cancelled"})
                _events.pop(row["id"], None)
                n += 1
    return n
