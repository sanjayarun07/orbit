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
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at TIMESTAMPTZ
);
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
_TS = ("lease_until", "next_run_at", "created_at", "updated_at", "settled_at")

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
    global _ready
    try:
        pool = await get_pg_pool()
    except Exception:
        return None
    if pool is not None and not _ready:
        try:
            await apply_schema(pool, _TABLE_SQL)
            _ready = True
        except Exception:
            logger.warning("jobs: schema not ready; using memory store", exc_info=True)
            return None
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
        "question": None, "action_id": None, "result": None, "receipt_id": None, "error": None, "attached": True, "delivered": False,
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


async def cas(job_id: str, expected: dict, patch: dict) -> dict | None:
    """Apply `patch` only if every `expected` column still holds; the fresh row,
    or None when someone else got there first."""
    patch = {**patch, "updated_at": _now()}
    pool = await _pool()
    if pool is not None:
        sets, args, n = [], [], 1
        for key, value in patch.items():
            if key in _JSON:
                sets.append(f"{key} = ${n}::jsonb"); args.append(json.dumps(value) if value is not None else None)
            else:
                sets.append(f"{key} = ${n}"); args.append(value)
            n += 1
        wheres = [f"id = ${n}"]; args.append(uuid.UUID(job_id)); n += 1
        for key, value in expected.items():
            wheres.append(f"{key} IS NOT DISTINCT FROM ${n}"); args.append(value); n += 1
        r = await pool.fetchrow(f"UPDATE jobs SET {', '.join(sets)} WHERE {' AND '.join(wheres)} RETURNING *", *args)
        return _from_db(r) if r else None
    with _lock:
        row = _memory.get(job_id)
        if row is None or any(row.get(k) != v for k, v in expected.items()):
            return None
        for key, value in patch.items():
            row[key] = value.isoformat() if isinstance(value, datetime) else (json.loads(json.dumps(value)) if key in _JSON and value is not None else value)
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

    async def guard(self) -> None:
        """Raise LostLease unless this lease still owns a running job whose
        owner still exists."""
        if self.signal.is_set():
            raise LostLease("cancelled")
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

    async def checkpoint(self, **patch) -> dict:
        if self.signal.is_set():
            raise LostLease("cancelled")
        fresh = await cas(self.id, {"lease_id": self.lease_id, "status": "running"}, patch)
        if fresh is None:
            self.signal.set()
            raise LostLease("lease lost at checkpoint")
        self.job = fresh
        return fresh

    async def event(self, kind: str, title: str, detail: str | None = None) -> None:
        await self.guard()
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
        operations = {**(self.job.get("operations") or {}), key: {"name": name, "result": _jsonable(result), "at": _now().isoformat(),
                                                                  "cost_usd": cost_usd, "volatile": volatile}}
        await self.checkpoint(operations=operations)
        return operations[key]["result"]

    async def add_evidence(self, item: dict) -> None:
        await self.checkpoint(evidence=[*(self.job.get("evidence") or []), item])

    async def add_signal(self, signal: Any) -> None:
        dumped = signal.model_dump(mode="json") if hasattr(signal, "model_dump") else signal
        await self.checkpoint(signals=[*(self.job.get("signals") or []), dumped])

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
    try:
        result = await handler(job, ctx)
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
    fresh = await cas(ctx.id, {"lease_id": ctx.lease_id, "status": "running"}, patch)
    if fresh is None:
        return await get(ctx.id)
    await _event(ctx.id, "settled", status, error)
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
    if job.get("status") not in TERMINAL or job.get("delivered") or job.get("attached", True):
        return
    from app import credits, sessions

    answer = ((job.get("result") or {}).get("answer") if isinstance(job.get("result"), dict) else None) or (
        f"The {job['kind'].replace('_', ' ')} could not be completed: {job.get('error') or job['status']}.")
    if job.get("session_id"):
        try:
            await sessions.append_turn(job["session_id"], "assistant", answer, {"job_id": job["id"], "kind": job["kind"]})
        except Exception:
            logger.warning("job %s: could not append its answer to session", job["id"], exc_info=True)
            return
    if job.get("account_id") and job.get("status") == "succeeded":
        cost = _cost_of(job["kind"])
        if cost:
            try:
                await credits.charge_once(job["account_id"], cost, f"job:{job['kind']}", "job", job["id"], {"kind": job["kind"]})
            except Exception:
                logger.warning("job %s: credit charge failed", job["id"], exc_info=True)
    await _set(job["id"], delivered=True)


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
    finally:
        _settled.pop(job_id, None)


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
    if pool is not None:
        rows = [_from_db(r) for r in await pool.fetch(
            "SELECT * FROM jobs WHERE status IN ('succeeded', 'failed') AND attached = FALSE AND delivered = FALSE LIMIT 20")]
    else:
        with _lock:
            rows = [json.loads(json.dumps(r)) for r in _memory.values()
                    if r["status"] in ("succeeded", "failed") and not r.get("attached", True) and not r.get("delivered")]
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
        status = await pool.execute(
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
