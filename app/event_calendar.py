"""Market event calendar: the dated things that move crypto and stocks over
the next week — Fed / CPI / jobs prints, mega-cap and crypto-adjacent
earnings, ETF decision deadlines, network upgrades, big token unlocks.

Perplexity web search is asked for strict JSON with dates; entries outside
the window or without a parseable date are dropped, so the card never shows
a made-up date. One build per EVENT_CALENDAR_TTL_SECONDS (6h) is shared by
the research card, GET /calendar, the home chips and the morning brief.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

from app.perplexity_tools import _invoke as perplexity_invoke, perplexity_available
from app.settings import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cached: tuple[float, int, dict] | None = None

TRIGGER = re.compile(
    r"\b(?:(?:market|economic|macro|crypto|earnings|events?)\s+calendar"
    r"|(?:events?|catalysts?|unlocks?|earnings|releases?)\b[^.?!]{0,60}\b(?:this|next|coming)\s+(?:week|few\s+days|days|month|fortnight)"
    r"|(?:this|next|coming)\s+(?:week|month)(?:'s)?\s+(?:events?|catalysts?|unlocks?|earnings|calendar)"
    r"|what(?:'s| is)\s+(?:coming\s+up|happening|scheduled|on\s+the\s+calendar)|upcoming\s+(?:events|catalysts|unlocks|earnings)"
    r"|when\s+is\s+(?:the\s+)?(?:next\s+)?(?:fomc|fed\s+(?:meeting|decision)|cpi|jobs\s+report|nfp|pce)"
    r"|market[- ]moving\s+events|events?\s+(?:that\s+)?(?:could|will|might|may)\s+move\s+the\s+market)\b",
    re.IGNORECASE,
)
CATEGORIES = ("macro", "earnings", "crypto", "unlock")

_INSTRUCTIONS = (
    "Return ONLY a JSON object (no prose, no code fences): "
    '{"events":[{"date":"YYYY-MM-DD","time":"HH:MM ET or empty","category":"macro|earnings|crypto|unlock","title":"","detail":"one sentence with the number or expectation","impact":"high|medium|low","source":"domain.tld"}]}. '
    "Include, for the given date window: scheduled US macro releases and Fed events (FOMC decision, CPI, PCE, jobs report, GDP), "
    "earnings from mega-caps and crypto-adjacent companies (e.g. NVDA, COIN, MSTR, HOOD, TSLA), and crypto catalysts "
    "(SEC/ETF decision deadlines, network upgrades, large token unlocks with amounts, major conferences, governance votes). "
    "Only include events with a known scheduled date inside the window; never guess dates. 8 to 16 events."
)


def _window(days: int) -> tuple[date, date]:
    start = datetime.now(timezone.utc).date()
    return start, start + timedelta(days=max(1, min(int(days), 30)))


def _parse(text: str, start: date, end: date) -> list[dict]:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return []
    events = []
    for item in (data.get("events") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        try:
            when = date.fromisoformat(str(item.get("date"))[:10])
        except ValueError:
            continue
        if when < start or when > end:
            continue
        category = str(item.get("category") or "crypto").lower()
        events.append({
            "date": when.isoformat(), "time": str(item.get("time") or "")[:20], "category": category if category in CATEGORIES else "crypto",
            "title": str(item["title"]).strip()[:120], "detail": str(item.get("detail") or "").strip()[:220],
            "impact": str(item.get("impact") or "medium").lower() if str(item.get("impact") or "").lower() in ("high", "medium", "low") else "medium",
            "source": str(item.get("source") or "").strip().lower()[:60],
        })
    events.sort(key=lambda e: (e["date"], {"high": 0, "medium": 1, "low": 2}[e["impact"]], e["time"]))
    return events


def build(days: int = 7) -> dict:
    start, end = _window(days)
    events: list[dict] = []
    source = "none"
    if perplexity_available():
        try:
            text = perplexity_invoke(
                "web_search",
                f"Scheduled market-moving events between {start.isoformat()} and {end.isoformat()}: Fed FOMC, CPI, PCE, jobs report, major earnings, crypto ETF deadlines, network upgrades, token unlocks",
                _INSTRUCTIONS,
            )
            events = _parse(text, start, end)
            source = "perplexity"
        except Exception:
            logger.warning("event_calendar: lookup failed", exc_info=True)
    return {"as_of": datetime.now(timezone.utc).isoformat(), "from": start.isoformat(), "to": end.isoformat(), "source": source, "events": events}


def get_calendar(days: int = 7, force: bool = False) -> dict:
    global _cached
    days = max(1, min(int(days), 30))
    ttl = max(300, int(settings.event_calendar_ttl_seconds))
    now = time.monotonic()
    if not force and _cached and _cached[0] > now and _cached[1] >= days:
        data = _cached[2]
        _, end = _window(days)
        return {**data, "to": end.isoformat(), "events": [e for e in data["events"] if e["date"] <= end.isoformat()]}
    with _lock:
        if not force and _cached and _cached[0] > time.monotonic() and _cached[1] >= days:
            return _cached[2]
        data = build(days)
        if data["events"] or _cached is None:
            _cached = (time.monotonic() + ttl, days, data)
        return data


def render(data: dict) -> str:
    events = data.get("events") or []
    lines = [f"# Market events · {data.get('from')} → {data.get('to')}", f"**As of** {str(data.get('as_of', ''))[:16].replace('T', ' ')} UTC · {len(events)} scheduled events", ""]
    if not events:
        lines.append("No scheduled events were found for this window" + ("" if data.get("source") != "none" else " (no news source configured)") + ".")
        return "\n".join(lines)
    icons = {"macro": "🏛", "earnings": "📊", "crypto": "🪙", "unlock": "🔓"}
    current = None
    for event in events:
        if event["date"] != current:
            current = event["date"]
            lines += ["", f"## {datetime.fromisoformat(current).strftime('%a %d %b')}"]
        impact = "**HIGH**" if event["impact"] == "high" else event["impact"]
        when = f" · {event['time']}" if event.get("time") else ""
        src = f" · {event['source']}" if event.get("source") else ""
        lines.append(f"- {icons.get(event['category'], '•')} **{event['title']}**{when} · {impact}{src}" + (f"\n  {event['detail']}" if event.get("detail") else ""))
    lines += ["", "*Dates come from published schedules found on the web; confirm the time zone before trading around them. Not advice.*"]
    return "\n".join(lines)


def today_events(data: dict, limit: int = 3) -> list[dict]:
    today = datetime.now(timezone.utc).date().isoformat()
    return [e for e in data.get("events") or [] if e["date"] == today][:limit]


def reset() -> None:
    global _cached
    _cached = None
