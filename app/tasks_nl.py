"""Natural-language task controls in chat: "remind me tomorrow at 9am to
check SOL", "alert me when SOL drops below $90", "send me a morning brief at
8am", "show my tasks", "pause/delete task 2".

Deterministic on purpose: a reminder is a commitment with a time in it, and
the user should see exactly what was scheduled. Anything that doesn't match
these shapes falls through to the normal turn.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from app import tasks, task_scheduling
from app.settings import settings
from app.service_errors import ServiceError

_DAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
         "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_TIME = r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?"
_TIME_NC = r"\d{1,2}(?::\d{2})?\s*(?:am|pm)?"
_REMIND = re.compile(r"^\s*(?:please\s+)?remind\s+me\s+(?P<rest>.+?)\s*$", re.I)
_ALERT = re.compile(
    r"^\s*(?:please\s+)?(?:alert|notify|tell|ping|warn)\s+me\s+(?:when|if|once)\s+\$?(?P<sym>[A-Za-z]{2,10})(?:\s+on\s+(?P<venue>aster|hyperliquid))?\s+"
    r"(?:(?P<dir>drops?|falls?|goes|rises?|climbs?|breaks?|is|gets?|moves?|dips?|pumps?)\s+)?"
    r"(?P<cmp>below|under|beneath|above|over|past|to|at)\s+\$?(?P<price>\d[\d,]*(?:\.\d+)?)\s*(?:usd|dollars|\$)?\s*(?P<repeat>every\s+time|each\s+time|repeatedly)?\s*[.!]?\s*$",
    re.I,
)
_BRIEF = re.compile(r"^\s*(?:send|give|email)\s+me\s+(?:a\s+|the\s+)?(?:daily\s+|morning\s+)?(?:market\s+)?brief(?:ing)?(?:\s+(?:every\s+day|daily))?(?:\s+at\s+" + _TIME + r")?(?:\s+by\s+(?P<channel>email|mail))?\s*[.!]?\s*$", re.I)
_LIST = re.compile(r"^\s*(?:show|list|what\s+are)\s+(?:me\s+)?(?:my\s+)?(?:tasks|reminders|alerts|scheduled\s+tasks)\??\s*[.!]?\s*$", re.I)
# The inventory in any words ("Anything scheduled for me?", "What alerts are running?",
# "Did I set a morning brief?", "Which of those are paused?"): a task word with an
# asking verb and no creation verb (expanded UI review, 2026-09-24: "Show my tasks."
# with a full stop asked for a ticker; "anything scheduled for me" asked for a wallet).
_INVENTORY = re.compile(r"\b(?:tasks?|reminders?|alerts?|watches|scheduled|schedule|morning\s+brief|briefs?)\b", re.I)
_INVENTORY_ASK = re.compile(r"\b(?:show|list|what|which|any|anything|do\s+i\s+have|did\s+i\s+set|have\s+i\s+set|are\s+there|is\s+there|running|active|paused|pending|already)\b", re.I)
_CREATES = re.compile(r"^\s*(?:please\s+)?(?:remind\s+me|alert\s+me|notify\s+me|tell\s+me\s+when|send\s+me|set\s+(?:a|an|up)|create|schedule\s+a|watch\s+my)\b", re.I)   # an instruction opens the message; "did I set a brief?" asks
_STATUS_FILTER = re.compile(r"\b(paused|active|running|done|completed|finished|pending)\b", re.I)
_REFERENT_STATUS = re.compile(r"^\s*(?:and\s+|so\s+|ok\s+)?which\s+(?:of\s+(?:those|them|these)\s+|ones\s+)?(?:are|is)\s+(paused|active|running|done|completed|pending)\b", re.I)
# "tell me when any tokenized stock moves more than 10% on hyperliquid within an hour"
_MOVERS_ALERT = re.compile(
    r"^\s*(?:please\s+)?(?:alert|notify|tell|ping|warn)\s+me\s+(?:when|if|once)\s+(?:any|a|some)\s+(?P<what>tokeni[sz]ed\s+stocks?|stocks?|equit(?:y|ies)|pairs?|tokens?|coins?|perps?)"
    r"(?:\s+on\s+(?P<venue1>aster|hyperliquid))?\s+(?:moves?|jumps?|swings?|changes?|pumps?|dumps?)\s+(?:by\s+)?(?:more\s+than|over|above|\+/-|±)?\s*(?P<pct>\d+(?:\.\d+)?)\s*%"
    r"(?:\s+on\s+(?P<venue2>aster|hyperliquid))?(?:\s+(?:in|within|over|during)\s+(?:an?\s+|the\s+(?:last|past)\s+)?(?P<n>\d+)?\s*(?P<unit>hours?|hrs?|h|minutes?|mins?|m|day|days?))?\s*[.!]?\s*$", re.I)
_MUTATE = re.compile(r"^\s*(?P<verb>pause|resume|delete|remove|cancel|stop)\s+(?:my\s+)?(?:task|reminder|alert)\s*#?\s*(?P<n>\d+)\s*[.!]?\s*$", re.I)
_MUTATE_ALL = re.compile(r"^\s*(?P<verb>pause|resume|delete|cancel|stop)\s+all\s+(?:my\s+)?(?:tasks|reminders|alerts)\s*[.!]?\s*$", re.I)


def is_task_control(message: str) -> bool:
    return any(p.match(message or "") for p in (_REMIND, _ALERT, _MOVERS_ALERT, _BRIEF, _LIST, _MUTATE, _MUTATE_ALL)) or _inventory_ask(message or "")


def _clock(h: str | None, m: str | None, ap: str | None, default: tuple[int, int] = (9, 0)) -> tuple[int, int]:
    if h is None:
        return default
    hour = int(h)
    minute = int(m or 0)
    if ap:
        ap = ap.lower()
        if ap == "pm" and hour < 12:
            hour += 12
        if ap == "am" and hour == 12:
            hour = 0
    return max(0, min(23, hour)), max(0, min(59, minute))


_NAME_IT = re.compile(r"\s*[.;,]?\s*(?:name|call|title)\s+it\s+[\"'“”‘’]?(?P<title>[^.;\"'“”‘’]+?)[\"'“”‘’]?(?=\s*[.;]|\s*$)", re.I)
# An affirmative delivery clause only, at the end of the text: "use the in-app
# inbox only", "by email", "inbox only". "Do not email me" and "to email
# Alice" are not delivery instructions (recheck of 2910d5e8).
_CHANNEL_WORDS = re.compile(
    r"(?:^|[.;,]\s*|\s)(?:(?:please\s+)?(?:use|via|by|through|send\s+(?:it\s+)?(?:to|via|by))\s+(?:the\s+)?(?:my\s+)?(?P<channel>in[- ]app(?:\s+inbox)?|inbox|email|e-mail)(?:\s+only)?"
    r"|(?P<channel2>in[- ]app(?:\s+inbox)?|inbox|email|e-mail)\s+only)\s*[.;!]?\s*$", re.I)
_NO_EMAIL = re.compile(r"(?:^|[.;,]\s*|\s)(?:do\s+not|don'?t|never|no)\s+(?:email|e-mail)(?:\s+me)?\s*[.;!]?\s*$", re.I)


def _reminder_fields(rest: str) -> tuple[str, str | None, str]:
    """Strip "Name it X" and "use the in-app inbox only / by email" from the
    reminder text: they are the task's title and channel, not its message
    (UI run, 2026-09-23: they were copied into the reminder body)."""
    title, channel = None, "inapp"
    m = _NAME_IT.search(rest)
    if m:
        title = m.group("title").strip()
        rest = rest[:m.start()] + rest[m.end():]
    m = _NO_EMAIL.search(rest)
    if m:
        rest = rest[:m.start()] + rest[m.end():]                          # an explicit no: in-app, and the sentence is not the message
    m = _CHANNEL_WORDS.search(rest)
    if m:
        word = (m.group("channel") or m.group("channel2") or "").lower()
        channel = "email" if word.startswith("e") else "inapp"
        rest = rest[:m.start()] + rest[m.end():]
    return rest.strip(" .;,"), title, channel


def parse_reminder(rest: str, tz_offset_min: int, now: datetime | None = None) -> tuple[dict, str] | None:
    """Returns (schedule, message) for the text after "remind me"."""
    now = now or datetime.now(timezone.utc)
    local_now = now + timedelta(minutes=tz_offset_min)
    text = rest.strip().rstrip(".!")

    # "in 20 minutes / 2 hours / 3 days to ..."
    m = re.match(r"^in\s+(?P<n>\d+)\s*(?P<unit>min(?:ute)?s?|h(?:ou)?rs?|days?|weeks?)\s*(?:to\s+)?(?P<msg>.*)$", text, re.I)
    if m:
        n = int(m.group("n"))
        unit = m.group("unit").lower()
        delta = timedelta(minutes=n) if unit.startswith("m") else timedelta(hours=n) if unit.startswith("h") else timedelta(days=n) if unit.startswith("d") else timedelta(weeks=n)
        return {"at": (now + delta).isoformat()}, (m.group("msg") or "").strip() or "Reminder"

    # "every day at 8am to ..." / "daily at 8 ..." / "every monday at 9am to ..."
    m = re.match(r"^(?:every\s+day|daily|each\s+day)(?:\s+at\s+" + _TIME + r")?\s*(?:to\s+)?(?P<msg>.*)$", text, re.I)
    if m:
        h, mi = _clock(m.group("h"), m.group("m"), m.group("ap"))
        return {"daily": f"{h:02d}:{mi:02d}"}, (m.group("msg") or "").strip() or "Daily reminder"
    m = re.match(r"^(?:every|each)\s+(?P<day>" + "|".join(_DAYS) + r")(?:\s+at\s+" + _TIME + r")?\s*(?:to\s+)?(?P<msg>.*)$", text, re.I)
    if m:
        h, mi = _clock(m.group("h"), m.group("m"), m.group("ap"))
        return {"weekly": {"day": _DAYS[m.group("day").lower()], "time": f"{h:02d}:{mi:02d}"}}, (m.group("msg") or "").strip() or "Weekly reminder"

    # "tomorrow at 9am to ..." / "on friday at 5pm to ..." / "at 5pm to ..." / "tonight" / "today at 3"
    m = re.match(r"^(?:(?P<when>tomorrow|today|tonight|on\s+(?P<day>" + "|".join(_DAYS) + r")|(?:this|next)\s+(?P<day2>" + "|".join(_DAYS) + r"))\s*)?(?:at\s+" + _TIME + r")?\s*(?:to\s+)?(?P<msg>.+)$", text, re.I)
    if m and (m.group("when") or m.group("h")):
        when = (m.group("when") or "").lower()
        h, mi = _clock(m.group("h"), m.group("m"), m.group("ap"), default=(20, 0) if when == "tonight" else (9, 0))
        target = local_now.replace(hour=h, minute=mi, second=0, microsecond=0)
        day = m.group("day") or m.group("day2")
        if when == "tomorrow":
            target += timedelta(days=1)
        elif day:
            delta = (_DAYS[day.lower()] - target.weekday()) % 7
            if delta == 0 and (target <= local_now or when.startswith("next")):
                delta = 7
            target += timedelta(days=delta)
        elif target <= local_now:
            target += timedelta(days=1)
        message = (m.group("msg") or "").strip()
        # "remind me at 5pm" with the message before the time: "remind me to call bob at 5pm"
        return {"at": (target - timedelta(minutes=tz_offset_min)).isoformat()}, message or "Reminder"

    # message first, time last: "to call bob at 5pm" / "to rebalance tomorrow"
    m = re.match(r"^(?:to\s+)?(?P<msg>.+?)\s+(?P<tail>(?:tomorrow|tonight|today|in\s+\d+\s*\w+|on\s+\w+|every\s+\w+|daily)(?:\s+at\s+" + _TIME_NC + r")?|at\s+" + _TIME_NC + r")\s*$", text, re.I)
    if m:
        parsed = parse_reminder(m.group("tail") + " to " + m.group("msg"), tz_offset_min, now)
        if parsed:
            return parsed
    return None


async def handle(message: str, user: dict, tz_offset_min: int = 0) -> str | None:
    """Apply a task control for a signed-in user. Returns the reply, or None
    when the message isn't a task control."""
    if not is_task_control(message):
        return None
    text = message.strip()
    if _inventory_ask(text):
        status = _STATUS_FILTER.search(text)
        return await _render_list(user, status=status.group(1).lower() if status else None)
    m = _MUTATE_ALL.match(text)
    if m:
        verb = m.group("verb").lower()
        count = 0
        for task in await tasks.list_tasks(user["id"], include_done=False):
            if verb in ("delete", "cancel", "stop"):
                count += int(await task_scheduling.delete_task(task["id"], user["id"]))
            else:
                status = "paused" if verb == "pause" else "active"
                if task["status"] != status:
                    await task_scheduling.update_task(task["id"], user["id"], user=user, status=status)
                    count += 1
        return f"Done — {count} task{'s' if count != 1 else ''} {'deleted' if verb in ('delete', 'cancel', 'stop') else verb + 'd'}."
    m = _MUTATE.match(text)
    if m:
        items = await tasks.list_tasks(user["id"], include_done=False)
        index = int(m.group("n")) - 1
        if index < 0 or index >= len(items):
            return f"I only see {len(items)} task{'s' if len(items) != 1 else ''}. Say *show my tasks* for the numbered list."
        task = items[index]
        verb = m.group("verb").lower()
        if verb in ("delete", "remove", "cancel", "stop"):
            await task_scheduling.delete_task(task["id"], user["id"])
            return f"Deleted task {index + 1}: **{task['title']}**."
        status = "paused" if verb == "pause" else "active"
        await task_scheduling.update_task(task["id"], user["id"], user=user, status=status)
        return f"Task {index + 1} **{task['title']}** is now {status}."
    m = _BRIEF.match(text)
    if m:
        h, mi = _clock(m.group("h"), m.group("m"), m.group("ap"), default=(8, 0))
        channel = task_scheduling.default_channel("email" if m.group("channel") else None)
        schedule = {"daily": f"{h:02d}:{mi:02d}"}
        # One brief per account: asking again reschedules and resumes the one
        # that exists (a paused brief plus a new active one, wallet run 2026-09-24).
        existing = next((t for t in await tasks.list_tasks(user["id"]) if t["kind"] == "brief"), None)
        if existing is not None:
            try:
                await tasks.update_task(existing["id"], user["id"], schedule=schedule, channel=channel,
                                        next_run_at=tasks.next_run(schedule, existing.get("tz_offset_min") or tz_offset_min or 0))
                task = await task_scheduling.update_task(existing["id"], user["id"], user=user, status="active")
            except ServiceError as exc:
                return f"I couldn't schedule that: {exc.detail}"
            return f"Morning brief rescheduled **daily at {h:02d}:{mi:02d}** ({_where(channel)}), the one you already had. Next: {_when(task)}. It costs 1 credit per delivery."
        task = await _create(user, "brief", {}, schedule, channel, tz_offset_min)
        return task if isinstance(task, str) else f"Morning brief scheduled **daily at {h:02d}:{mi:02d}** ({_where(channel)}). First one: {_when(task)}. It costs 1 credit per delivery."
    m = _ALERT.match(text)
    if m:
        cmp = m.group("cmp").lower()
        op = "<" if cmp in ("below", "under", "beneath") or (cmp in ("to", "at") and (m.group("dir") or "").lower().startswith(("drop", "fall", "dip"))) else ">"
        price = float(m.group("price").replace(",", ""))
        symbol = m.group("sym").upper()
        current = await tasks.price_for(symbol, (m.group("venue") or "").lower() or None)
        if current is None:
            return f"I couldn't find a trustworthy price for **{symbol}** (majors and Jupiter-verified Solana tokens are supported), so no alert was set."
        spec = {"symbol": symbol, "op": op, "price": price, "repeat": bool(m.group("repeat")), "venue": (m.group("venue") or "").lower() or None}
        task = await _create(user, "price_alert", spec, {"every_minutes": 5}, task_scheduling.default_channel(), tz_offset_min)
        if isinstance(task, str):
            return task
        return (f"Alert set: **{symbol} {op} ${price:,.4g}** (now ${current:,.4g}). I check every few minutes and send you a note"
                + (" each time it crosses." if spec["repeat"] else " the first time it crosses."))
    m = _MOVERS_ALERT.match(text)
    if m:
        venue = (m.group("venue1") or m.group("venue2") or "").lower()
        if not venue:
            return "Which venue: Aster or Hyperliquid? For example: *tell me when any tokenized stock moves more than 10% on hyperliquid within an hour*."
        unit = (m.group("unit") or "hour").lower()
        n = int(m.group("n") or 1)
        window = n * (1 if unit.startswith("m") and not unit.startswith("mo") else 1440 if unit.startswith("d") else 60)
        stocks_only = bool(re.match(r"(?i)tokeni|stock|equit", m.group("what")))
        spec = {"venue": venue, "threshold_pct": float(m.group("pct")), "window_minutes": window, "stocks_only": stocks_only}
        task = await _create(user, "movers_alert", spec, {"every_minutes": settings.task_alert_check_minutes}, "inapp", tz_offset_min)
        if isinstance(task, str):
            return task
        return (f"Movers alert set: any {'tokenized stock' if stocks_only else 'pair'} on {venue} that moves more than {float(m.group('pct')):g}% within {window} minutes "
                f"goes to your inbox, each pair at most once per window. Measured between stored 5-minute ticks of the venue's feed; never a trade.")
    m = _REMIND.match(text)
    if m:
        rest, title, channel = _reminder_fields(m.group("rest"))
        parsed = parse_reminder(rest, tz_offset_min)
        if not parsed:
            return ("Tell me when and what — e.g. *remind me tomorrow at 9am to check SOL*, *remind me in 2 hours to rebalance*, "
                    "or *remind me every Monday at 9am to review my portfolio*.")
        schedule, msg = parsed
        task = await _create(user, "reminder", {"message": msg}, schedule, task_scheduling.default_channel(channel), tz_offset_min, title=title or f"Reminder: {msg}")
        if isinstance(task, str):
            return task
        return (f"Reminder set — **{title or msg}** · {tasks.describe_schedule(schedule, tz_offset_min)} · {'email' if channel == 'email' else 'in-app inbox'}. "
                f"Next: {_when(task)}.")
    return None


def _where(channel: str) -> str:
    return {"email": "email + inbox", "telegram": "Telegram + inbox"}.get(channel, "inbox")


async def _create(user, kind, spec, schedule, channel, tz, title=None):
    try:
        return await task_scheduling.create_task(user, kind, spec, schedule, channel, tz, title)
    except ServiceError as exc:
        return f"I couldn't schedule that: {exc.detail}"


def _when(task: dict) -> str:
    at = tasks._parse_dt(task.get("next_run_at"))
    if not at:
        return "—"
    local = at + timedelta(minutes=int(task.get("tz_offset_min") or 0))
    return local.strftime("%a %d %b, %H:%M")


def _inventory_ask(text: str) -> bool:
    """An ask to see the tasks, in any words, with no creation verb."""
    return bool(_LIST.match(text) or _REFERENT_STATUS.match(text)
                or (_INVENTORY.search(text) and _INVENTORY_ASK.search(text) and not _CREATES.search(text) and len(text.split()) <= 14))


async def _render_list(user: dict, status: str | None = None) -> str:
    want_done = status in ("done", "completed", "finished")
    items = await tasks.list_tasks(user["id"], include_done=want_done)
    # A task's number is its place in the full list, in every listing: a
    # filtered list renumbered from 1 made "resume task 1" act on the
    # unfiltered first task (review of bc40f724, 2026-09-24).
    numbered = list(enumerate(items, start=1))
    if status:
        wanted = {"running": "active", "completed": "done", "finished": "done", "pending": "active"}.get(status, status)
        numbered = [(n, t) for n, t in numbered if t.get("status") == wanted]
        if not numbered:
            return f"None of your tasks is {status}. Say *show my tasks* for the full list."
    if not numbered:
        return "No tasks yet. Try *remind me tomorrow at 9am to check SOL*, *alert me when SOL drops below $90*, or *send me a morning brief at 8am*."
    lines = [f"**Your {status} tasks**" if status else "**Your tasks**", ""]
    for index, task in numbered:
        lines.append(f"{index}. **{task['title']}** · {tasks.describe_schedule(task['schedule'], task.get('tz_offset_min', 0))} · {task['status']} · next {_when(task)}")
    first = numbered[0][0]
    lines += ["", (f"Say `pause task {first}`, `delete task {first}`, or open Settings → Tasks." if len(numbered) == 1
                   else f"Say `pause task {numbered[1][0]}`, `delete task {first}` (the numbers are the task's place in the full list), or open Settings → Tasks.")]
    return "\n".join(lines)
