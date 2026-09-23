"""Long-running per-user tasks: reminders, price alerts and the morning brief.

A task is a row (owner, kind, spec, schedule) that a background worker
evaluates on its due time with the tools Orbit already has; when it fires it
writes an inbox notification and, if the user chose it, an email. Kinds:

- reminder    spec {"message"}                      one-shot or recurring
- price_alert spec {"symbol", "op": "<"|">", "price", "chain"?, "repeat"?}
- brief       spec {}  -- a daily morning brief: today's headlines, the core
                          quotes, and the user's wallet total when one is linked

Schedules: {"at": iso} once · {"daily": "HH:MM"} · {"weekly": {"day": 0-6, "time": "HH:MM"}}
· {"every_minutes": n}. Times are the user's local time via `tz_offset_min`
(minutes east of UTC, as the browser reports it negated).

Storage follows the repo contract: Postgres (`user_tasks`, `user_inbox`) with
in-memory fallbacks. Evaluation is free; a brief delivery costs
CREDIT_COST_BRIEF from the owner's pool, and plans cap active tasks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app import accounts, credits, emailer, home_highlights, market_overview, notifications
from app.billing_plans import get_plan
from app.db import get_pg_pool
from app.jupiter import jupiter
from app.settings import settings

# Per-account activation mutexes for the no-Postgres path, keyed by (loop, user).
_account_locks: dict[tuple[int, str], asyncio.Lock] = {}

logger = logging.getLogger(__name__)

KINDS = ("reminder", "price_alert", "movers_alert", "brief")
# Where a fired task is delivered. "inapp" is always written (the inbox row is
# the delivery of record and the idempotency key); the other two are
# best-effort pushes on top of it.
CHANNELS = ("inapp", "email", "telegram")
TASK_LIMITS = {"anonymous": 0, "free": 3, "pro": 25, "max": 100}
_MAJORS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin", "XRP": "ripple", "DOGE": "dogecoin", "ADA": "cardano", "AVAX": "avalanche-2", "LINK": "chainlink", "TRX": "tron", "TON": "the-open-network", "SUI": "sui"}

_tasks: dict[str, dict] = {}
_inbox: dict[str, list[dict]] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# Scheduling
# ----------------------------------------------------------------------------

def next_run(schedule: dict, tz_offset_min: int = 0, after: datetime | None = None) -> datetime | None:
    """The next UTC instant a schedule is due after `after` (default now)."""
    after = after or _now()
    offset = timedelta(minutes=int(tz_offset_min or 0))
    if "at" in schedule:
        at = _parse_dt(schedule["at"])
        return at if at and at > after else None
    if "every_minutes" in schedule:
        return after + timedelta(minutes=max(1, int(schedule["every_minutes"])))
    local_after = after + offset
    if "daily" in schedule:
        hour, minute = _hm(schedule["daily"])
        candidate = local_after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_after:
            candidate += timedelta(days=1)
        return candidate - offset
    if "weekly" in schedule:
        spec = schedule["weekly"] or {}
        hour, minute = _hm(spec.get("time", "09:00"))
        day = int(spec.get("day", 0)) % 7
        candidate = local_after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = (day - candidate.weekday()) % 7
        candidate += timedelta(days=delta)
        if candidate <= local_after:
            candidate += timedelta(days=7)
        return candidate - offset
    return None


def _hm(value: str) -> tuple[int, int]:
    parts = str(value).split(":")
    hour = max(0, min(23, int(parts[0])))
    minute = max(0, min(59, int(parts[1]))) if len(parts) > 1 else 0
    return hour, minute


def describe_schedule(schedule: dict, tz_offset_min: int = 0) -> str:
    if "at" in schedule:
        at = _parse_dt(schedule["at"])
        local = at + timedelta(minutes=tz_offset_min or 0) if at else None
        return f"once, {local.strftime('%a %d %b %H:%M')}" if local else "once"
    if "every_minutes" in schedule:
        return f"every {int(schedule['every_minutes'])} min"
    if "daily" in schedule:
        return f"daily at {schedule['daily']}"
    if "weekly" in schedule:
        spec = schedule["weekly"] or {}
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return f"every {days[int(spec.get('day', 0)) % 7]} at {spec.get('time', '09:00')}"
    return "unscheduled"


# ----------------------------------------------------------------------------
# Task storage
# ----------------------------------------------------------------------------

def _row(row) -> dict:
    spec = row["spec"]
    schedule = row["schedule"]
    return {
        "id": str(row["id"]), "user_id": str(row["user_id"]), "kind": row["kind"], "title": row["title"],
        "spec": json.loads(spec) if isinstance(spec, str) else dict(spec or {}),
        "schedule": json.loads(schedule) if isinstance(schedule, str) else dict(schedule or {}),
        "channel": row["channel"], "status": row["status"], "tz_offset_min": int(row["tz_offset_min"] or 0),
        "created_at": _iso(row["created_at"]), "next_run_at": _iso(row["next_run_at"]), "last_run_at": _iso(row["last_run_at"]),
        "last_result": row["last_result"], "fire_count": int(row["fire_count"] or 0),
        "claimed_until": _iso(row["claimed_until"]) if "claimed_until" in row.keys() else None,
        "claimed_occurrence": row["claimed_occurrence"] if "claimed_occurrence" in row.keys() else None,
        # Read back, or every attempt at an occurrence is attempt one and the
        # retry limit is never reached (it was written but never hydrated).
        "retry_count": int(row["retry_count"] or 0) if "retry_count" in row.keys() else 0,
    }


def public(task: dict) -> dict:
    return {**task, "schedule_text": describe_schedule(task["schedule"], task.get("tz_offset_min", 0))}


async def list_tasks(user_id: str, include_done: bool = True, db=None) -> list[dict]:
    """`db` is the connection an activation gate is already holding; inside
    that window every query must ride it rather than ask the pool for another."""
    pool = await get_pg_pool()
    if pool is not None:
        # Oldest first: the numbers in "pause task 2" must stay stable as tasks are added.
        rows = await (db or pool).fetch("SELECT * FROM user_tasks WHERE user_id = $1 ORDER BY created_at ASC", user_id)
        tasks = [_row(r) for r in rows]
    else:
        tasks = sorted([dict(t) for t in _tasks.values() if t["user_id"] == user_id], key=lambda t: t["created_at"])
    return [t for t in tasks if include_done or t["status"] != "done"]


async def get_task(task_id: str, db=None) -> dict | None:
    pool = await get_pg_pool()
    if pool is not None:
        row = await (db or pool).fetchrow("SELECT * FROM user_tasks WHERE id = $1", task_id)
        return _row(row) if row else None
    task = _tasks.get(task_id)
    return dict(task) if task else None


@asynccontextmanager
async def account_task_gate(user_id: str):
    """Serialise task activation per account, across processes.

    Counting the active tasks and then activating one are two operations, so
    two concurrent resumptions both saw a free slot and both took it. This is
    the mutex that makes the pair atomic. A Postgres session advisory lock does
    it across every worker; the lock is held on its own connection, which is
    fine because it is only a mutex -- every writer takes it, so the work can
    happen on any connection.

    Without Postgres there is one process, so an asyncio lock is equivalent. It
    is keyed by running loop as well as account: a lock object belongs to the
    loop that created it, and this repo has been bitten before by reusing one
    across loops.
    """
    pool = await get_pg_pool()
    if pool is not None:
        # One connection for the lock AND the work, in one transaction. The
        # first version held a connection for the lock and then asked the pool
        # for another to count and write: with as many concurrent activations
        # as the pool has connections, every one held a lock-connection and
        # none could get a work-connection. A transaction-scoped advisory lock
        # is released with the commit, so there is no separate unlock to miss.
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock($1)", _advisory_key(user_id))
                yield connection
        return
    loop_id = id(asyncio.get_running_loop())
    lock = _account_locks.setdefault((loop_id, user_id), asyncio.Lock())
    async with lock:
        yield None


def _advisory_key(user_id: str) -> int:
    """A stable 63-bit key for pg_advisory_lock, namespaced to task limits."""
    digest = hashlib.blake2b(f"tasks:{user_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


async def assert_can_activate(user: dict, exclude_task_id: str | None = None, db=None, plan=None) -> None:
    """The plan's active-task limit, checked wherever a task BECOMES active.

    Creation was the only place this ran, so pausing tasks and resuming them
    walked straight past it: a Free account could hold any number of paused
    tasks and switch them all on. The limit is about how many tasks run, not
    how many were created, so it belongs at every transition into "active".
    """
    # The effective plan -- a team member's owner's -- is what the UI shows
    # as the limit; enforcing the personal plan here stopped a Max member at
    # the Free count.
    if plan is None:
        # Resolving the effective plan reads the owner's account from the
        # pool. Inside the gate that is a second connection while holding one
        # -- the deadlock class the gate exists to avoid -- so callers resolve
        # it BEFORE entering and pass it in; this fallback is for callers that
        # are not inside a gate.
        from app.identity import resolve_billing

        _user, _owner, plan = await resolve_billing(user)
    limit = TASK_LIMITS.get(plan.id, 3)
    active = [
        t for t in await list_tasks(user["id"], include_done=False, db=db)
        if t["status"] == "active" and t["id"] != exclude_task_id
    ]
    if len(active) >= limit:
        raise ValueError(f"The {plan.name} plan allows {limit} active tasks; pause or delete one first")


def validate_spec(kind: str, spec: dict) -> dict:
    """The spec a task may be created or updated with. A price alert needs a
    symbol, an op and a finite positive price: "SOL < $-1" was accepted from
    the form (UI run, 2026-09-23)."""
    import math

    spec = dict(spec or {})
    if kind == "price_alert":
        symbol = str(spec.get("symbol") or "").strip()
        if not symbol or len(symbol) > 32:
            raise ValueError("A price alert needs a token symbol")
        op = spec.get("op", "<")
        if op not in ("<", ">"):
            raise ValueError("A price alert compares with < or >")
        try:
            price = float(spec.get("price"))
        except (TypeError, ValueError):
            raise ValueError("A price alert needs a numeric price") from None
        if not math.isfinite(price) or price <= 0:
            raise ValueError("The alert price must be a positive number")
        venue = str(spec.get("venue") or "").strip().lower() or None
        if venue not in (None, "aster", "hyperliquid"):
            raise ValueError("A venue must be aster or hyperliquid")
        spec.update({"symbol": symbol, "op": op, "price": price, "venue": venue})
    elif kind == "movers_alert":
        venue = str(spec.get("venue") or "").strip().lower()
        if venue not in ("aster", "hyperliquid"):
            raise ValueError("A movers alert needs a venue: aster or hyperliquid")
        try:
            threshold = float(spec.get("threshold_pct"))
        except (TypeError, ValueError):
            raise ValueError("A movers alert needs a percentage threshold") from None
        if not math.isfinite(threshold) or threshold <= 0 or threshold > 1000:
            raise ValueError("The threshold must be a positive percentage")
        window = int(spec.get("window_minutes") or 60)
        if window < 10 or window > 7 * 24 * 60:
            raise ValueError("The window must be between 10 minutes and 7 days")
        spec.update({"venue": venue, "threshold_pct": threshold, "window_minutes": window, "stocks_only": bool(spec.get("stocks_only")),
                     "repeat": True, "last_fired": dict(spec.get("last_fired") or {})})
    elif kind == "reminder":
        if not str(spec.get("message") or "").strip():
            raise ValueError("A reminder needs a message")
    return spec


async def create_task(user: dict, kind: str, spec: dict, schedule: dict, channel: str = "inapp", tz_offset_min: int = 0, title: str | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"Unknown task kind {kind!r}")
    spec = validate_spec(kind, spec)
    when = next_run(schedule, tz_offset_min)
    if when is None:
        raise ValueError("That schedule has no future run (is the time in the past?)")
    from app.identity import resolve_billing

    _user, _owner, plan = await resolve_billing(user)   # outside the gate: it reads the pool
    async with account_task_gate(user["id"]) as db:
        return await _create_task_locked(user, kind, spec, schedule, channel, tz_offset_min, title, when, db, plan)


async def _create_task_locked(user: dict, kind: str, spec: dict, schedule: dict, channel: str,
                              tz_offset_min: int, title: str | None, when, db=None, plan=None) -> dict:
    await assert_can_activate(user, db=db, plan=plan)
    task = {
        "id": str(uuid4()), "user_id": user["id"], "kind": kind, "title": (title or _default_title(kind, spec))[:140],
        "spec": dict(spec), "schedule": dict(schedule), "channel": channel if channel in CHANNELS else "inapp",
        "status": "active", "tz_offset_min": int(tz_offset_min or 0), "created_at": _iso(_now()),
        "next_run_at": _iso(when), "last_run_at": None, "last_result": None, "fire_count": 0, "retry_count": 0,
    }
    pool = await get_pg_pool()
    if pool is not None:
        await (db or pool).execute(
            """
            INSERT INTO user_tasks (id, user_id, kind, title, spec, schedule, channel, status, tz_offset_min, next_run_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'active', $8, $9)
            """,
            task["id"], user["id"], kind, task["title"], json.dumps(task["spec"]), json.dumps(task["schedule"]), task["channel"], task["tz_offset_min"], when,
        )
    else:
        _tasks[task["id"]] = task
    return dict(task)


def _default_title(kind: str, spec: dict) -> str:
    if kind == "reminder":
        return f"Reminder: {spec.get('message', '')}".strip()
    if kind == "price_alert":
        return f"{spec.get('symbol', '?')} {spec.get('op', '<')} ${float(spec.get('price', 0)):,.4g}" + (f" on {spec['venue']}" if spec.get("venue") else "")
    if kind == "movers_alert":
        return f"{'Stocks' if spec.get('stocks_only') else 'Pairs'} moving over {float(spec.get('threshold_pct', 0)):g}% on {spec.get('venue', '?')} within {int(spec.get('window_minutes') or 60)} min"
    if kind == "brief":
        return "Morning brief"
    return kind


async def update_task(task_id: str, user_id: str, *, db=None, **fields) -> dict | None:
    allowed = {"status", "next_run_at", "last_run_at", "last_result", "fire_count", "channel", "schedule", "spec", "claimed_until", "claimed_occurrence", "retry_count"}
    changes = {k: v for k, v in fields.items() if k in allowed}
    if "spec" in changes:
        current = await get_task(task_id, db=db)
        if current is not None:
            changes["spec"] = validate_spec(current["kind"], changes["spec"])
    pool = await get_pg_pool()
    if pool is not None:
        current = await get_task(task_id, db=db)
        if current is None or current["user_id"] != user_id:
            return None
        values = []
        sets = []
        for index, (key, value) in enumerate(changes.items(), start=3):
            if key in ("schedule", "spec"):
                value = json.dumps(value)
            elif key in ("next_run_at", "last_run_at", "claimed_until"):
                value = _parse_dt(value)
            sets.append(f"{key} = ${index}")
            values.append(value)
        if not sets:
            return current
        row = await (db or pool).fetchrow(f"UPDATE user_tasks SET {', '.join(sets)} WHERE id = $1 AND user_id = $2 RETURNING *", task_id, user_id, *values)
        return _row(row) if row else None
    task = _tasks.get(task_id)
    if task is None or task["user_id"] != user_id:
        return None
    for key, value in changes.items():
        task[key] = _iso(_parse_dt(value)) if key in ("next_run_at", "last_run_at", "claimed_until") and value is not None else value
    return dict(task)


async def delete_task(task_id: str, user_id: str) -> bool:
    pool = await get_pg_pool()
    if pool is not None:
        result = await pool.execute("DELETE FROM user_tasks WHERE id = $1 AND user_id = $2", task_id, user_id)
        return result.endswith("1")
    task = _tasks.get(task_id)
    if task is None or task["user_id"] != user_id:
        return False
    _tasks.pop(task_id, None)
    return True


async def due_tasks(now: datetime | None = None, limit: int = 50) -> list[dict]:
    now = now or _now()
    pool = await get_pg_pool()
    if pool is not None:
        rows = await pool.fetch(
            """
            SELECT * FROM user_tasks WHERE status = 'active'
              AND ((next_run_at IS NOT NULL AND next_run_at <= $1) OR (next_run_at IS NULL AND claimed_until IS NOT NULL AND claimed_until < $1))
            ORDER BY next_run_at NULLS FIRST LIMIT $2
            """, now, limit,
        )
        return [_row(r) for r in rows]

    def _due(t: dict) -> bool:
        if t["status"] != "active":
            return False
        if t.get("next_run_at"):
            return _parse_dt(t["next_run_at"]) <= now
        lease = _parse_dt(t.get("claimed_until"))
        return lease is not None and lease < now      # stale claim: the worker died mid-run
    due = [dict(t) for t in _tasks.values() if _due(t)]
    return sorted(due, key=lambda t: t.get("next_run_at") or "")[:limit]


# ----------------------------------------------------------------------------
# Inbox
# ----------------------------------------------------------------------------

async def notify(user_id: str, title: str, body: str, kind: str = "task", task_id: str | None = None,
                 occurrence: str | None = None) -> tuple[dict, bool]:
    """Deliver to the inbox. Returns (item, new).

    Keyed by the task occurrence when there is one: the inbox row IS the
    durable delivery record, so a retry of an occurrence that already
    delivered finds its row and does nothing, and email -- which cannot be
    de-duplicated after the fact -- is sent only when the row was new."""
    item = {"id": str(uuid4()), "user_id": user_id, "title": title[:200], "body": body[:4000], "kind": kind,
            "task_id": task_id, "occurrence": occurrence, "created_at": _iso(_now()), "read_at": None}
    pool = await get_pg_pool()
    if pool is not None:
        status = await pool.execute(
            "INSERT INTO user_inbox (id, user_id, title, body, kind, task_id, occurrence) VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "ON CONFLICT DO NOTHING",
            item["id"], user_id, item["title"], item["body"], kind, task_id, occurrence,
        )
        return item, status.endswith("1")
    if occurrence and any(i.get("task_id") == task_id and i.get("occurrence") == occurrence for i in _inbox.get(user_id, [])):
        return item, False
    _inbox.setdefault(user_id, []).append(item)
    del _inbox[user_id][:-200]
    return item, True


async def _occurrence_delivered(user_id: str, task_id: str, occurrence: str) -> bool:
    """Whether this occurrence reached the inbox. The inbox row is the durable
    delivery record, and it is what a refund decision has to consult: a debit
    alone cannot tell "never delivered" from "delivered, then failed to record
    it", and refunding the second case pays the customer for a brief they
    have."""
    pool = await get_pg_pool()
    if pool is not None:
        return bool(await pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM user_inbox WHERE task_id = $1 AND occurrence = $2)", task_id, occurrence))
    return any(i.get("task_id") == task_id and i.get("occurrence") == occurrence for i in _inbox.get(user_id, []))


async def inbox(user_id: str, limit: int = 50) -> list[dict]:
    pool = await get_pg_pool()
    if pool is not None:
        rows = await pool.fetch("SELECT * FROM user_inbox WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2", user_id, limit)
        return [{"id": str(r["id"]), "title": r["title"], "body": r["body"], "kind": r["kind"], "task_id": str(r["task_id"]) if r["task_id"] else None,
                 "created_at": _iso(r["created_at"]), "read_at": _iso(r["read_at"])} for r in rows]
    items = list(reversed(_inbox.get(user_id, [])))[:limit]
    return [{k: v for k, v in item.items() if k != "user_id"} for item in items]


async def mark_read(user_id: str, ids: list[str] | None = None) -> int:
    pool = await get_pg_pool()
    if pool is not None:
        if ids is None:
            result = await pool.execute("UPDATE user_inbox SET read_at = NOW() WHERE user_id = $1 AND read_at IS NULL", user_id)
        else:
            result = await pool.execute("UPDATE user_inbox SET read_at = NOW() WHERE user_id = $1 AND read_at IS NULL AND id = ANY($2::uuid[])", user_id, ids)
        return int(result.rsplit(" ", 1)[-1] or 0)
    count = 0
    for item in _inbox.get(user_id, []):
        if item["read_at"] is None and (ids is None or item["id"] in ids):
            item["read_at"] = _iso(_now())
            count += 1
    return count


async def unread_count(user_id: str) -> int:
    pool = await get_pg_pool()
    if pool is not None:
        return int(await pool.fetchval("SELECT COUNT(*) FROM user_inbox WHERE user_id = $1 AND read_at IS NULL", user_id) or 0)
    return sum(1 for item in _inbox.get(user_id, []) if item["read_at"] is None)


# ----------------------------------------------------------------------------
# Evaluation
# ----------------------------------------------------------------------------

async def price_for(symbol: str, venue: str | None = None) -> float | None:
    """USD price for a ticker: the venue's feed when an alert names Aster or
    Hyperliquid (tokenized stocks live there), else CoinGecko for majors and
    Jupiter's verified registry for Solana tokens. None when nothing
    trustworthy matches."""
    symbol = symbol.upper().lstrip("$")
    if venue:
        from app import tequity

        snap = await tequity.snapshot(tequity.MOVERS.get(venue, ""))
        rows = ((snap or {}).get("data") or {}).get("data", {}).get("tokens") or []
        for row in rows:
            if isinstance(row, dict) and (str(row.get("base_asset") or "").upper() == symbol or str(row.get("symbol") or "").upper() == symbol) and row.get("last_price") is not None:
                return float(row["last_price"])
        return None
    coingecko_id = _MAJORS.get(symbol)
    if coingecko_id:
        try:
            data = await asyncio.to_thread(market_overview._get_json, f"{market_overview._CG}/simple/price?ids={coingecko_id}&vs_currencies=usd")
            price = (data or {}).get(coingecko_id, {}).get("usd")
            if price is not None:
                return float(price)
        except Exception:
            logger.debug("tasks: coingecko price failed for %s", symbol, exc_info=True)
    try:
        matches = await jupiter.search_tokens(symbol)
    except Exception:
        return None
    for item in matches or []:
        if str(item.get("symbol") or "").upper() == symbol and "verified" in (item.get("tags") or []) and item.get("usdPrice"):
            return float(item["usdPrice"])
    return None


async def evaluate(task: dict) -> tuple[bool, str, str | None]:
    """(fire?, result text, notification body). Never raises."""
    kind, spec = task["kind"], task["spec"]
    try:
        if kind == "reminder":
            return True, "fired", spec.get("message") or task["title"]
        if kind == "movers_alert":
            return await _evaluate_movers_alert(task)
        if kind == "price_alert":
            price = await price_for(spec["symbol"], spec.get("venue"))
            if price is None:
                return False, "price unavailable", None
            target = float(spec["price"])
            hit = price < target if spec.get("op", "<") == "<" else price > target
            text = f"{spec['symbol'].upper()} is ${price:,.4g} (target {spec.get('op', '<')} ${target:,.4g})"
            return hit, text, (f"{spec['symbol'].upper()} crossed your alert: ${price:,.4g} is {'below' if spec.get('op', '<') == '<' else 'above'} ${target:,.4g}." if hit else None)
        if kind == "brief":
            return True, "delivered", await compose_brief(task)
    except Exception:
        # An evaluation that FAILED is not a condition that did not fire. The
        # old return of (False, "error: ...") was indistinguishable from "not
        # due", so a one-shot brief whose provider was offline was marked done
        # and the promised brief was lost. Raising lets run_task's recovery
        # handler record the error and reschedule, occurrence intact.
        raise
    return False, "unknown kind", None


async def _evaluate_movers_alert(task: dict) -> tuple[bool, str, str | None]:
    """Pairs whose stored price moved more than the threshold within the
    window, each reported once per window (spec.last_fired)."""
    from app import tequity_ledger

    spec = dict(task["spec"])
    now = _now()
    window = timedelta(minutes=int(spec.get("window_minutes") or 60))
    out = await tequity_ledger.movers_between(spec["venue"], now - window, now, stocks_only=bool(spec.get("stocks_only")), limit=500)
    rows = [r for r in (out.get("gainers") or []) if abs(r["change_between_pct"]) >= float(spec["threshold_pct"])]
    if not out.get("from"):
        return False, "no stored ticks in the window yet", None
    fired = dict(spec.get("last_fired") or {})
    fresh = []
    for r in rows:
        last = fired.get(r["symbol"])
        if last and (now - datetime.fromisoformat(last)) < window:
            continue
        fresh.append(r)
        fired[r["symbol"]] = now.isoformat()
    fired = {k: v for k, v in fired.items() if (now - datetime.fromisoformat(v)) < window * 2}
    await update_task(task["id"], task["user_id"], spec={**spec, "last_fired": fired})
    if not fresh:
        return False, f"{len(rows)} over threshold, none new", None
    fresh.sort(key=lambda r: abs(r["change_between_pct"]), reverse=True)
    label = "tokenized stock" if spec.get("stocks_only") else "pair"
    moves = ", ".join(f"{r.get('base_asset') or r['symbol']} {r['change_between_pct']:+.1f}% (${r['price_then']:,.4g} → ${r['last_price']:,.4g})" for r in fresh[:6])
    text = f"{len(fresh)} {label}{'s' if len(fresh) != 1 else ''} moved over {float(spec['threshold_pct']):g}% on {spec['venue']} within {int(window.total_seconds() // 60)} min: {moves}"
    return True, text, text + ". Measured between stored 5-minute ticks of the venue's feed; not a recommendation."


async def _venue_brief_lines() -> list[str]:
    """One line per venue for the morning brief: top movers and the stock share of volume."""
    from app import tequity

    if not tequity.enabled():
        return []
    lines = []
    for venue, channel in tequity.MOVERS.items():
        try:
            snap = await asyncio.wait_for(tequity.snapshot(channel), timeout=10)
        except Exception:
            snap = None
        rows = [r for r in (((snap or {}).get("data") or {}).get("data", {}).get("tokens") or []) if isinstance(r, dict) and r.get("price_change_percent") is not None]
        if not rows:
            continue
        rows.sort(key=lambda r: float(r["price_change_percent"]), reverse=True)
        top = ", ".join(f"{r.get('base_asset') or r['symbol']} {float(r['price_change_percent']):+.1f}%" for r in rows[:3])
        stocks = [r for r in rows if r.get("is_stock")]
        volume = sum(float(r.get("quote_volume") or 0) for r in rows) or 1.0
        stock_share = sum(float(r.get("quote_volume") or 0) for r in stocks) / volume * 100
        best_stock = f"; stocks: {stocks[0].get('base_asset')} {float(stocks[0]['price_change_percent']):+.1f}%" if stocks else ""
        lines.append(f"- **{venue.title()}**: {top}{best_stock} · tokenized stocks {stock_share:.0f}% of 24h quote volume")
    return ["", "**Perp venues (24h, company feed)**", *lines] if lines else []


async def compose_brief(task: dict) -> str:
    """Today's headlines + the core quotes + the user's wallet total (if linked)."""
    lines = [f"**Morning brief** · {_now().strftime('%a %d %b')}", ""]
    highlights = await asyncio.to_thread(home_highlights.get_highlights)
    for card in highlights.get("cards", [])[:4]:
        if card.get("kind") in ("crypto", "stocks"):
            lines.append(f"- **{card['title']}** — {card.get('summary') or ''}".rstrip(" —"))
    try:
        simple = await asyncio.to_thread(market_overview._get_json, market_overview._SIMPLE_URL)
        quotes = []
        for sym, cid in (("BTC", "bitcoin"), ("ETH", "ethereum"), ("SOL", "solana")):
            entry = (simple or {}).get(cid) or {}
            if entry.get("usd") is not None:
                quotes.append(f"{sym} ${float(entry['usd']):,.0f} ({float(entry.get('usd_24h_change') or 0):+.1f}%)")
        if quotes:
            lines += ["", " · ".join(quotes)]
    except Exception:
        logger.debug("tasks: quotes unavailable for brief", exc_info=True)
    try:
        lines += await _venue_brief_lines()
    except Exception:
        logger.debug("tasks: venue lines unavailable for brief", exc_info=True)
    user = await accounts.get_user(task["user_id"])
    wallet = ((user or {}).get("preferences") or {}).get("default_wallet")
    if not wallet:
        wallets = await accounts.list_wallets(task["user_id"]) if user else []
        wallet = wallets[0]["address"] if wallets else None
    if wallet and not str(wallet).startswith("0x"):
        try:
            from app.portfolio import build_portfolio_snapshot
            snapshot = await asyncio.wait_for(build_portfolio_snapshot(wallet), timeout=20)
            total = snapshot.get("total_usd_value")
            top = [h for h in snapshot.get("holdings", []) if h.get("usd_value")][:3]
            if total is not None:
                lines += ["", f"Your wallet: **${float(total):,.2f}**" + (" · " + ", ".join(f"{h['symbol']} ${float(h['usd_value']):,.0f}" for h in top) if top else "")]
        except Exception:
            logger.debug("tasks: wallet snapshot unavailable for brief", exc_info=True)
    try:
        from app import event_calendar
        todays = event_calendar.today_events(event_calendar.get_calendar(7))
        if todays:
            lines += ["", "**Today**: " + " · ".join(f"{e['title']}{' ' + e['time'] if e.get('time') else ''}" for e in todays)]
    except Exception:
        logger.debug("tasks: calendar unavailable for brief", exc_info=True)
    lines += ["", "Ask me about any of these — or say *why is X moving* for the full picture."]
    return "\n".join(lines)


_claim_lock = asyncio.Lock()
CLAIM_LEASE_SECONDS = 15 * 60   # a run that outlives this is presumed dead and its occurrence recoverable


async def claim_task(task: dict) -> dict | None:
    """Atomically take the scheduled occurrence `task['next_run_at']` for this
    run. Two workers (or a worker and "run now") racing on the same task get
    exactly one winner: the claim clears `next_run_at`, so the loser's claim
    matches nothing and it skips. The occurrence key is returned on the task
    as `occurrence` and later stamps the charge and the reschedule."""
    occurrence = task.get("next_run_at")
    now = _now()
    lease_until = now + timedelta(seconds=CLAIM_LEASE_SECONDS)
    # The occurrence key is persisted with the claim: recovering an expired
    # claim reuses it, so the ledger's unique reference keeps a run that died
    # after charging from charging again.
    # An unfinished occurrence comes first: the recovery handler keeps
    # `claimed_occurrence` and schedules a retry time, and that retry has to run
    # AS the occurrence it never completed, or its charge and delivery get a new
    # key and happen again.
    occurrence_key = task.get("claimed_occurrence") or occurrence or f"manual:{now.isoformat()}"
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow(
            """
            UPDATE user_tasks SET next_run_at = NULL, last_result = 'running', claimed_until = $4, claimed_occurrence = $5
            WHERE id = $1 AND status IN ('active', 'paused') AND next_run_at IS NOT DISTINCT FROM $2
              AND (claimed_until IS NULL OR claimed_until < $3)
            RETURNING *
            """,
            task["id"], _parse_dt(occurrence), now, lease_until, occurrence_key,
        )
        if row is None:
            return None
        claimed = _row(row)
    else:
        async with _claim_lock:
            current = _tasks.get(task["id"])
            lease = _parse_dt(current.get("claimed_until")) if current else None
            if (current is None or current["status"] not in ("active", "paused") or current.get("next_run_at") != occurrence
                    or (lease is not None and lease >= now)):
                return None
            current["next_run_at"] = None
            current["last_result"] = "running"
            current["claimed_until"] = _iso(lease_until)
            current["claimed_occurrence"] = occurrence_key
            claimed = dict(current)
    claimed["occurrence"] = occurrence_key
    return claimed


async def run_task(task: dict) -> dict:
    """Evaluate one due task, deliver if it fired, and reschedule or finish.
    The occurrence is claimed first; a task another worker already took is
    reported as skipped and never delivered or charged twice."""
    user = await accounts.get_user(task["user_id"])
    if user is None:
        await delete_task(task["id"], task["user_id"])
        return {"id": task["id"], "status": "deleted"}
    claimed = await claim_task(task)
    if claimed is None:
        return {"id": task["id"], "fired": False, "result": "skipped: already running", "status": task.get("status", "active")}
    task = claimed
    try:
        return await _run_claimed(task, user)
    except Exception as exc:
        # Never leave a failed task stranded with no schedule: record the error,
        # release the lease and put it back on its schedule (or retry soon).
        logger.warning("tasks: run failed for %s", task["id"], exc_info=True)
        now = _now()
        ref = f"{task['id']}:{task['occurrence']}"
        if await _occurrence_delivered(task["user_id"], task["id"], ref):
            # The brief reached the inbox; what failed was the bookkeeping
            # after it. The occurrence is finished here -- not re-composed,
            # and never refunded as undelivered -- and the records the failed
            # write should have made are made now.
            updates = {"last_run_at": now, "last_result": "delivered (completion recorded after a failed write)",
                       "claimed_until": None, "claimed_occurrence": None, "retry_count": 0,
                       "fire_count": int(task.get("fire_count") or 0) + 1, **_reschedule(task, now, True)}
            await update_task(task["id"], task["user_id"], **updates)
            return {"id": task["id"], "fired": True, "result": updates["last_result"],
                    "status": updates.get("status", "active"), "recovered": True}
        attempts = int(task.get("retry_count") or 0) + 1
        if attempts >= settings.task_retry_limit:
            # Enough. Refund the occurrence (idempotent on its key), drop its
            # identity so the next run is a new, separately paid occurrence,
            # and put the task back on its ordinary schedule. The inbox was
            # consulted above: this refund is for an occurrence that never
            # delivered.
            if task["kind"] == "brief" and settings.credit_cost_brief:
                account_id = await _billing_account_for(user)
                if await credits.has_ref(account_id, "task_run", ref) and not await credits.has_ref(account_id, "task_refund", ref):
                    await credits.append(account_id, settings.credit_cost_brief, "task:brief-refund", "task_refund", ref, {"kind": "brief", "attempts": attempts})
            nxt = next_run(task["schedule"], task.get("tz_offset_min", 0), after=now)
            await update_task(task["id"], task["user_id"], last_run_at=now,
                              last_result=f"failed after {attempts} attempts: {str(exc)[:120] or exc.__class__.__name__}",
                              next_run_at=nxt, claimed_until=None, claimed_occurrence=None, retry_count=0,
                              **({"status": "done"} if nxt is None else {}))
            return {"id": task["id"], "fired": False, "result": "failed", "status": "done" if nxt is None else "active"}
        nxt = now + timedelta(minutes=settings.task_alert_check_minutes)
        # The occurrence identity is KEPT across the retry. Clearing it gave the
        # retry a fresh key, so a run that had already delivered and charged --
        # and failed only on its final bookkeeping write -- delivered and
        # charged again. With the same key, the charge is idempotent and the
        # inbox row is a no-op.
        await update_task(task["id"], task["user_id"], last_run_at=now, last_result=f"error: {str(exc)[:160] or exc.__class__.__name__}",
                          next_run_at=nxt, claimed_until=None, retry_count=attempts)
        return {"id": task["id"], "fired": False, "result": "error", "status": "active", "attempt": attempts}


def _reschedule(task: dict, now: datetime, fired: bool) -> dict:
    """Where the task goes once an occurrence is over: done for a one-shot
    that fired, otherwise its next time (a price alert with no interval of
    its own polls on the check interval). Shared by the ordinary completion
    and the recovery of a delivered occurrence, so they cannot differ."""
    one_shot = "at" in task["schedule"] or (task["kind"] in ("price_alert", "movers_alert") and not task["spec"].get("repeat"))
    if fired and one_shot:
        return {"status": "done", "next_run_at": None}
    interval = task["schedule"] if not (task["kind"] in ("price_alert", "movers_alert") and "at" not in task["schedule"] and "every_minutes" not in task["schedule"]) else {"every_minutes": settings.task_alert_check_minutes}
    nxt = next_run(interval, task.get("tz_offset_min", 0), after=now)
    if nxt is None and task["kind"] in ("price_alert", "movers_alert"):
        nxt = now + timedelta(minutes=settings.task_alert_check_minutes)
    return {"next_run_at": nxt, **({"status": "done"} if nxt is None else {})}


async def _billing_account_for(user: dict) -> str:
    """The account a task's cost is charged to, by the same rule chat uses.

    A member's task used to charge whatever `team_owner_id` said, unchecked;
    identity resolution validates that the team still exists and still has
    seats, and clears a stale pointer. Charging must go through that rule too,
    or a task keeps billing an owner who dropped the member."""
    from app.identity import resolve_billing

    user, team_owner, _plan = await resolve_billing(user)
    return credits.user_account_id((team_owner or user)["id"])


async def _run_claimed(task: dict, user: dict) -> dict:
    charge = settings.credit_cost_brief if task["kind"] == "brief" else 0
    ref = f"{task['id']}:{task['occurrence']}"
    account_id = await _billing_account_for(user) if charge else None
    # Reserve BEFORE the cost-bearing work, as a chat turn does: composing a
    # brief calls the model, and a brief nobody can pay for should not be
    # composed. The charge is one atomic check-and-debit keyed by the
    # occurrence -- two occurrences can no longer both spend the same last
    # credit, and a re-run of an occurrence that already paid is free.
    if charge:
        outcome = await credits.charge_once(account_id, charge, "task:brief", "task_run", ref, {"kind": "brief"})
        if outcome is credits.ChargeOutcome.INSUFFICIENT:
            now = _now()
            updates = {"last_run_at": now, "last_result": "skipped: out of credits", "claimed_until": None, "claimed_occurrence": None}
            interval = task["schedule"]
            nxt = next_run(interval, task.get("tz_offset_min", 0), after=now)
            updates["next_run_at"] = nxt
            if nxt is None:
                updates["status"] = "done"
            await update_task(task["id"], task["user_id"], **updates)
            return {"id": task["id"], "fired": False, "result": updates["last_result"], "status": updates.get("status", "active")}
    try:
        fired, result, body = await evaluate(task)
    except Exception:
        # The charge STAYS with the occurrence across a retry. Refunding here
        # and then letting the retry see the original debit as "already paid"
        # delivered a brief with no net charge. The retry runs as the same
        # occurrence and the same payment covers it; only when the bounded
        # retries are exhausted (see run_task) is the occurrence refunded.
        raise
    now = _now()
    updates: dict = {"last_run_at": now, "last_result": result, "claimed_until": None, "claimed_occurrence": None, "retry_count": 0}
    if charge and not (fired and body):
        # Composed nothing deliverable (a condition that did not fire): refund.
        await credits.append(account_id, charge, "task:brief-refund", "task_refund", ref, {"kind": "brief"})
    if fired and body:
        _item, delivered_now = await notify(user["id"], task["title"], body, kind=task["kind"], task_id=task["id"], occurrence=ref)
        if delivered_now and task.get("channel") == "telegram":
            # Same contract as email below: best-effort on top of the inbox
            # row, recorded on failure, never retried.
            try:
                if not await notifications.send_telegram(user, task["title"], body):
                    updates["last_result"] = f"{result} (telegram not delivered)"
            except Exception:
                logger.warning("tasks: telegram delivery failed for %s", task["id"], exc_info=True)
                updates["last_result"] = f"{result} (telegram failed)"
        elif delivered_now and task.get("channel") == "email" and user.get("email"):
            # Email failure behaviour, stated: the inbox row already stands as
            # the delivery of record, so a failed send is recorded and NOT
            # retried -- a retry cannot tell a lost email from a late one, and
            # a duplicate brief is the worse outcome.
            try:
                sent = await emailer.send_email(user["email"], f"{settings.product_name}: {task['title']}", "<p>" + body.replace("\n", "<br>") + "</p>", text=body)
                if not sent:
                    updates["last_result"] = f"{result} (email not sent)"
            except Exception:
                logger.warning("tasks: email delivery failed for %s", task["id"], exc_info=True)
                updates["last_result"] = f"{result} (email failed)"
        if delivered_now:
            updates["fire_count"] = int(task.get("fire_count") or 0) + 1
    updates.update(_reschedule(task, now, fired))
    await update_task(task["id"], task["user_id"], **updates)
    return {"id": task["id"], "fired": fired, "result": updates["last_result"], "status": updates.get("status", "active")}


async def tick(now: datetime | None = None) -> list[dict]:
    results = []
    for task in await due_tasks(now):
        try:
            results.append(await run_task(task))
        except Exception:
            logger.warning("tasks: run failed for %s", task["id"], exc_info=True)
    return results


async def worker() -> None:
    while True:
        try:
            await tick()
        except Exception:
            logger.warning("tasks: worker tick failed", exc_info=True)
        await asyncio.sleep(max(10, settings.task_worker_interval_seconds))


def reset() -> None:
    _tasks.clear()
    _inbox.clear()
