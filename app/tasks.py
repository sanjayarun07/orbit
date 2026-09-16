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
import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app import accounts, credits, emailer, home_highlights, market_overview
from app.billing_plans import get_plan
from app.db import get_pg_pool
from app.jupiter import jupiter
from app.settings import settings

logger = logging.getLogger(__name__)

KINDS = ("reminder", "price_alert", "brief")
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
    }


def public(task: dict) -> dict:
    return {**task, "schedule_text": describe_schedule(task["schedule"], task.get("tz_offset_min", 0))}


async def list_tasks(user_id: str, include_done: bool = True) -> list[dict]:
    pool = await get_pg_pool()
    if pool is not None:
        # Oldest first: the numbers in "pause task 2" must stay stable as tasks are added.
        rows = await pool.fetch("SELECT * FROM user_tasks WHERE user_id = $1 ORDER BY created_at ASC", user_id)
        tasks = [_row(r) for r in rows]
    else:
        tasks = sorted([dict(t) for t in _tasks.values() if t["user_id"] == user_id], key=lambda t: t["created_at"])
    return [t for t in tasks if include_done or t["status"] != "done"]


async def get_task(task_id: str) -> dict | None:
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow("SELECT * FROM user_tasks WHERE id = $1", task_id)
        return _row(row) if row else None
    task = _tasks.get(task_id)
    return dict(task) if task else None


async def create_task(user: dict, kind: str, spec: dict, schedule: dict, channel: str = "inapp", tz_offset_min: int = 0, title: str | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"Unknown task kind {kind!r}")
    plan = get_plan(user.get("plan_id"))
    limit = TASK_LIMITS.get(plan.id, 3)
    active = [t for t in await list_tasks(user["id"], include_done=False) if t["status"] == "active"]
    if len(active) >= limit:
        raise ValueError(f"The {plan.name} plan allows {limit} active tasks; pause or delete one first")
    when = next_run(schedule, tz_offset_min)
    if when is None:
        raise ValueError("That schedule has no future run (is the time in the past?)")
    task = {
        "id": str(uuid4()), "user_id": user["id"], "kind": kind, "title": (title or _default_title(kind, spec))[:140],
        "spec": dict(spec), "schedule": dict(schedule), "channel": channel if channel in ("inapp", "email") else "inapp",
        "status": "active", "tz_offset_min": int(tz_offset_min or 0), "created_at": _iso(_now()),
        "next_run_at": _iso(when), "last_run_at": None, "last_result": None, "fire_count": 0,
    }
    pool = await get_pg_pool()
    if pool is not None:
        await pool.execute(
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
        return f"{spec.get('symbol', '?')} {spec.get('op', '<')} ${float(spec.get('price', 0)):,.4g}"
    if kind == "brief":
        return "Morning brief"
    return kind


async def update_task(task_id: str, user_id: str, **fields) -> dict | None:
    allowed = {"status", "next_run_at", "last_run_at", "last_result", "fire_count", "channel", "schedule", "spec"}
    changes = {k: v for k, v in fields.items() if k in allowed}
    pool = await get_pg_pool()
    if pool is not None:
        current = await get_task(task_id)
        if current is None or current["user_id"] != user_id:
            return None
        values = []
        sets = []
        for index, (key, value) in enumerate(changes.items(), start=3):
            if key in ("schedule", "spec"):
                value = json.dumps(value)
            elif key in ("next_run_at", "last_run_at"):
                value = _parse_dt(value)
            sets.append(f"{key} = ${index}")
            values.append(value)
        if not sets:
            return current
        row = await pool.fetchrow(f"UPDATE user_tasks SET {', '.join(sets)} WHERE id = $1 AND user_id = $2 RETURNING *", task_id, user_id, *values)
        return _row(row) if row else None
    task = _tasks.get(task_id)
    if task is None or task["user_id"] != user_id:
        return None
    for key, value in changes.items():
        task[key] = _iso(_parse_dt(value)) if key in ("next_run_at", "last_run_at") and value is not None else value
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
            "SELECT * FROM user_tasks WHERE status = 'active' AND next_run_at IS NOT NULL AND next_run_at <= $1 ORDER BY next_run_at LIMIT $2", now, limit,
        )
        return [_row(r) for r in rows]
    due = [dict(t) for t in _tasks.values() if t["status"] == "active" and t.get("next_run_at") and _parse_dt(t["next_run_at"]) <= now]
    return sorted(due, key=lambda t: t["next_run_at"])[:limit]


# ----------------------------------------------------------------------------
# Inbox
# ----------------------------------------------------------------------------

async def notify(user_id: str, title: str, body: str, kind: str = "task", task_id: str | None = None) -> dict:
    item = {"id": str(uuid4()), "user_id": user_id, "title": title[:200], "body": body[:4000], "kind": kind,
            "task_id": task_id, "created_at": _iso(_now()), "read_at": None}
    pool = await get_pg_pool()
    if pool is not None:
        await pool.execute(
            "INSERT INTO user_inbox (id, user_id, title, body, kind, task_id) VALUES ($1, $2, $3, $4, $5, $6)",
            item["id"], user_id, item["title"], item["body"], kind, task_id,
        )
    else:
        _inbox.setdefault(user_id, []).append(item)
        del _inbox[user_id][:-200]
    return item


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

async def price_for(symbol: str) -> float | None:
    """USD price for a ticker: CoinGecko for majors, Jupiter's verified
    registry for Solana tokens. None when nothing trustworthy matches."""
    symbol = symbol.upper().lstrip("$")
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
        if kind == "price_alert":
            price = await price_for(spec["symbol"])
            if price is None:
                return False, "price unavailable", None
            target = float(spec["price"])
            hit = price < target if spec.get("op", "<") == "<" else price > target
            text = f"{spec['symbol'].upper()} is ${price:,.4g} (target {spec.get('op', '<')} ${target:,.4g})"
            return hit, text, (f"{spec['symbol'].upper()} crossed your alert: ${price:,.4g} is {'below' if spec.get('op', '<') == '<' else 'above'} ${target:,.4g}." if hit else None)
        if kind == "brief":
            return True, "delivered", await compose_brief(task)
    except Exception as exc:
        logger.warning("tasks: evaluation failed for %s", task["id"], exc_info=True)
        return False, f"error: {exc}"[:200], None
    return False, "unknown kind", None


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


async def claim_task(task: dict) -> dict | None:
    """Atomically take the scheduled occurrence `task['next_run_at']` for this
    run. Two workers (or a worker and "run now") racing on the same task get
    exactly one winner: the claim clears `next_run_at`, so the loser's claim
    matches nothing and it skips. The occurrence key is returned on the task
    as `occurrence` and later stamps the charge and the reschedule."""
    occurrence = task.get("next_run_at")
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow(
            "UPDATE user_tasks SET next_run_at = NULL, last_result = 'running' WHERE id = $1 AND status IN ('active', 'paused') AND next_run_at IS NOT DISTINCT FROM $2 RETURNING *",
            task["id"], _parse_dt(occurrence),
        )
        if row is None:
            return None
        claimed = _row(row)
    else:
        async with _claim_lock:
            current = _tasks.get(task["id"])
            if current is None or current["status"] not in ("active", "paused") or current.get("next_run_at") != occurrence:
                return None
            current["next_run_at"] = None
            current["last_result"] = "running"
            claimed = dict(current)
    claimed["occurrence"] = occurrence or f"manual:{_now().isoformat()}"
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
    fired, result, body = await evaluate(task)
    now = _now()
    updates: dict = {"last_run_at": now, "last_result": result}
    if fired and body:
        charge = settings.credit_cost_brief if task["kind"] == "brief" else 0
        if charge:
            account_id = credits.user_account_id(user.get("team_owner_id") or user["id"])
            if await credits.balance(account_id) < charge:
                updates["last_result"] = "skipped: out of credits"
                fired = False
            else:
                # Keyed by the scheduled occurrence, not the wall clock: the ledger's
                # unique reference makes a repeated run of the same occurrence free.
                await credits.append(account_id, -charge, "task:brief", "task_run", f"{task['id']}:{task['occurrence']}", {"kind": "brief"})
    if fired and body:
        await notify(user["id"], task["title"], body, kind=task["kind"], task_id=task["id"])
        if task.get("channel") == "email":
            await emailer.send_email(user["email"], f"{settings.product_name}: {task['title']}", "<p>" + body.replace("\n", "<br>") + "</p>", text=body)
        updates["fire_count"] = int(task.get("fire_count") or 0) + 1
    one_shot = "at" in task["schedule"] or (task["kind"] == "price_alert" and not task["spec"].get("repeat"))
    if fired and one_shot:
        updates["status"] = "done"
        updates["next_run_at"] = None
    else:
        interval = task["schedule"] if not (task["kind"] == "price_alert" and "at" not in task["schedule"] and "every_minutes" not in task["schedule"]) else {"every_minutes": settings.task_alert_check_minutes}
        nxt = next_run(interval, task.get("tz_offset_min", 0), after=now)
        if nxt is None and task["kind"] == "price_alert":
            nxt = now + timedelta(minutes=settings.task_alert_check_minutes)
        updates["next_run_at"] = nxt
        if nxt is None:
            updates["status"] = "done"
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
