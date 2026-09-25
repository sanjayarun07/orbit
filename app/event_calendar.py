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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from app.perplexity_tools import _invoke as perplexity_invoke, perplexity_available, perplexity_fetch_url
from app.settings import settings
from app import url_reader

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
    '{"events":[{"date":"YYYY-MM-DD","time":"HH:MM ET or empty","category":"macro|earnings|crypto|unlock","title":"","detail":"one sentence with the number or expectation","impact":"high|medium|low","source_url":"https://specific-page-with-the-schedule"}]}. '
    "Include, for the given date window: scheduled US macro releases and Fed events (FOMC decision, CPI, PCE, jobs report, GDP), "
    "earnings from mega-caps and crypto-adjacent companies (e.g. NVDA, COIN, MSTR, HOOD, TSLA), and crypto catalysts "
    "(SEC/ETF decision deadlines, network upgrades, large token unlocks with amounts, major conferences, governance votes). "
    "Use an official organizer, agency, issuer, or exchange schedule page for each event. Cite the exact public page URL, not a domain or a news summary. "
    "Only include events with a known scheduled date inside the window; never guess dates. Do not repeat a release under two dates. 8 to 16 events."
)

_OFFICIAL_MACRO_HOSTS = (
    "bea.gov", "bls.gov", "census.gov", "federalreserve.gov", "newyorkfed.org", "ismworld.org", "umich.edu",
)
_UNLOCK_CALENDAR_HOSTS = ("app.tokenomics.com", "tokenomist.ai", "defillama.com")
_BLS_EMPLOYMENT_CALENDAR = "https://www.bls.gov/cps/publications/release-calendar.htm"
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
_TITLE_STOP = {"the", "and", "for", "with", "from", "data", "report", "release", "final", "advance", "estimate", "quarter", "monthly", "september", "october", "august", "july", "2026"}


def _official_macro(host: str) -> bool:
    return any(host == official or host.endswith("." + official) for official in _OFFICIAL_MACRO_HOSTS)


def _date_patterns(day: date) -> tuple[re.Pattern, ...]:
    month = _MONTHS[day.month - 1]
    short = month[:3]
    return (
        re.compile(rf"\b{day.year}-{day.month:02d}-{day.day:02d}\b"),
        re.compile(rf"\b{day.month:02d}/{day.day:02d}/{day.year}\b"),
        re.compile(rf"\b(?:{month}|{short})\.?\s+0?{day.day}(?:st|nd|rd|th)?(?:,?\s+{day.year})?\b", re.I),
        re.compile(rf"\b0?{day.day}\s+(?:{month}|{short})\.?\s+{day.year}\b", re.I),
    )


def _source_supports(event: dict, page: str) -> bool:
    """Require the event's date and subject together in the cited page."""
    day = date.fromisoformat(event["date"])
    if str(day.year) not in page and str(day.year) not in event["source_url"]:
        return False
    title_terms = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", event["title"]) if w.lower() not in _TITLE_STOP]
    if not title_terms:
        return False
    for pattern in _date_patterns(day):
        for match in pattern.finditer(page):
            context = page[max(0, match.start() - 260):match.end() + 260].lower()
            if sum(term in context for term in set(title_terms)) >= min(2, len(set(title_terms))):
                return True
    return False


def _upcoming(event: dict, now: datetime) -> bool:
    """A release earlier today is no longer an upcoming catalyst."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*(?:ET|EST|EDT)\s*", event.get("time") or "", re.I)
    if not match:
        return event["date"] > now.astimezone(timezone.utc).date().isoformat()
    eastern = datetime.combine(date.fromisoformat(event["date"]), datetime.min.time(), tzinfo=ZoneInfo("America/New_York"))
    eastern = eastern.replace(hour=int(match.group(1)), minute=int(match.group(2)))
    return eastern.astimezone(timezone.utc) > now


def _verify_events(events: list[dict], now: datetime) -> list[dict]:
    """Fail closed on dates that cannot be read from their claimed source."""
    urls = list(dict.fromkeys(event["source_url"] for event in events))[:16]
    with ThreadPoolExecutor(max_workers=6) as pool:
        pages = dict(zip(urls, pool.map(url_reader.fetch, urls)))
    checked = []
    indexed_cache: dict[str, str] = {}
    for event in events:
        page = pages.get(event["source_url"])
        if not page and event["category"] == "macro" and _official_macro(event["source"]):
            # Some agency sites block direct readers. The provider's indexed
            # copy is a fallback only for an official schedule URL; it must
            # still contain the event and date together.
            url = event["source_url"]
            if url not in indexed_cache:
                try:
                    indexed_cache[url] = perplexity_fetch_url(url)
                except Exception:
                    indexed_cache[url] = ""
            indexed = indexed_cache[url]
            page = ("indexed official schedule", indexed) if indexed else None
        if page and _source_supports(event, page[1]) and _upcoming(event, now):
            event["verification"] = "indexed" if page[0] == "indexed official schedule" else "direct"
            checked.append(event)
    return checked


def _dedupe_events(events: list[dict]) -> list[dict]:
    """Merge differently worded entries for the same dated release."""
    selected: list[dict] = []
    for event in events:
        words = {w for w in re.findall(r"[a-z0-9]{3,}", event["title"].lower()) if w not in _TITLE_STOP}
        duplicate = False
        for prior in selected:
            if prior["date"] != event["date"] or prior["category"] != event["category"]:
                continue
            earlier = {w for w in re.findall(r"[a-z0-9]{3,}", prior["title"].lower()) if w not in _TITLE_STOP}
            shared = words & earlier
            if len(shared) >= 2 and len(shared) / max(1, min(len(words), len(earlier))) >= 0.6:
                detail = event.get("detail") or ""
                if detail and detail not in (prior.get("detail") or ""):
                    prior["detail"] = ((prior.get("detail") or "").rstrip(". ") + "; " + detail).strip("; ")[:440]
                duplicate = True
                break
        if not duplicate:
            selected.append(event)
    selected.sort(key=lambda e: (e["date"], {"high": 0, "medium": 1, "low": 2}[e["impact"]], e["time"]))
    return selected


def _employment_calendar_candidates(start: date, end: date) -> list[dict]:
    """Propose first-Friday jobs releases, then require BLS page verification.

    BLS can shift the release for holidays; a computed date is only a lead.
    The same source/date check as every other event decides whether it ships.
    """
    candidates = []
    day = start
    while day <= end:
        if day.weekday() == 4 and day.day <= 7:
            candidates.append({"date": day.isoformat(), "time": "08:30 ET", "category": "macro",
                               "title": "Employment Situation", "detail": "Monthly U.S. employment report.",
                               "impact": "high", "source": "www.bls.gov", "source_url": _BLS_EMPLOYMENT_CALENDAR})
        day += timedelta(days=1)
    return candidates


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
        if re.search(r"\bunlock\b", str(item["title"]), re.I):
            category = "unlock"
        url = str(item.get("source_url") or "").strip()
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host or not parsed.path.strip("/"):
            continue
        if category == "macro" and not _official_macro(host):
            continue
        if category == "unlock" and not any(host == provider or host.endswith("." + provider) for provider in _UNLOCK_CALENDAR_HOSTS):
            continue
        events.append({
            "date": when.isoformat(), "time": str(item.get("time") or "")[:20], "category": category if category in CATEGORIES else "crypto",
            "title": str(item["title"]).strip()[:120], "detail": str(item.get("detail") or "").strip()[:220],
            "impact": str(item.get("impact") or "medium").lower() if str(item.get("impact") or "").lower() in ("high", "medium", "low") else "medium",
            "source": host[:60], "source_url": url,
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
            candidates = _parse(text, start, end)
            if sum(event["category"] == "macro" for event in candidates) < 4:
                try:
                    macro_text = perplexity_invoke(
                        "web_search",
                        f"Official US economic release calendars from {start.isoformat()} through {end.isoformat()}: BEA, BLS, Census, Federal Reserve, ISM. Check each agency schedule for the release date and Eastern time.",
                        _INSTRUCTIONS,
                    )
                    candidates.extend(_parse(macro_text, start, end))
                except Exception:
                    logger.info("event_calendar: supplemental macro lookup failed", exc_info=True)
            unique: dict[tuple[str, str], dict] = {}
            for event in candidates + _employment_calendar_candidates(start, end):
                key = (event["date"], re.sub(r"\W+", " ", event["title"].lower()).strip())
                unique.setdefault(key, event)
            events = _dedupe_events(_verify_events(list(unique.values()), datetime.now(timezone.utc)))
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
        src = f" · [{event['source']}]({event['source_url']})" if event.get("source_url") else ""
        lines.append(f"- {icons.get(event['category'], '•')} **{event['title']}**{when} · {impact}{src}" + (f"\n  {event['detail']}" if event.get("detail") else ""))
    lines += ["", "*Dates were checked against the linked schedules" + (" (indexed copies where direct pages were unavailable)" if any(e.get("verification") == "indexed" for e in events) else "") + ". Macro release times are in U.S. Eastern Time; other times are shown as labelled. Not advice.*"]
    return "\n".join(lines)


def today_events(data: dict, limit: int = 3) -> list[dict]:
    today = datetime.now(timezone.utc).date().isoformat()
    return [e for e in data.get("events") or [] if e["date"] == today][:limit]


def reset() -> None:
    global _cached
    _cached = None
