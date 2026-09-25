"""A bounded, source-linked answer for a broad crypto-market status question.

The quantitative snapshot and two reporting axes run concurrently. Search
results contribute dated article leads, not unverified market figures or
claims that a headline caused a price move.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import re
from urllib.parse import urlsplit

from app import market_overview, perplexity_tools, streaming, x_news

logger = logging.getLogger(__name__)

_SEARCHES = (
    ("news", "What are the most material crypto-market developments reported in the past two days? Find dated direct articles or primary announcements; distinguish event time from publication time."),
    ("flows", "What is the latest dated reporting on Bitcoin or Ethereum spot ETF flows, crypto derivatives funding, or liquidations? Find a direct report or primary data source; do not infer a price cause."),
)
_ROUNDUP_TITLE = re.compile(r"\b(?:latest (?:crypto )?news|news update|market update|daily (?:market )?report|daily briefing)\b", re.I)


def _dated_lead(found: dict, *, now: datetime) -> dict | None:
    """Select a recent direct article from provider metadata, not its prose."""
    minimum = (now - timedelta(days=1)).date()
    candidates = []
    for position, source in enumerate(found.get("sources") or [], start=1):
        url = str(source.get("url") or "")
        title = str(source.get("title") or "").strip()
        published = str(source.get("date") or "")[:10]
        try:
            day = datetime.fromisoformat(published).date()
            host = urlsplit(url).hostname
        except ValueError:
            continue
        if not (host and url.startswith("https://") and title and minimum <= day <= now.date()):
            continue
        # Search metadata's date can describe when a rolling feed was updated.
        # An event-specific article slug or a publication date in its path is
        # needed; a live homepage, topic index or tracker is only a lead.
        path = urlsplit(url).path.lower()
        date_paths = (day.strftime("%Y/%m/%d"), day.strftime("%Y-%m-%d"), day.strftime("%Y%m%d"))
        slug_words = [word for word in re.split(r"[-_]", path.rstrip("/").rsplit("/", 1)[-1]) if word]
        specific_slug = len(slug_words) >= 5 and not any(word in slug_words for word in ("tracker", "chart", "table", "latest"))
        if not (any(date_path in path for date_path in date_paths) or specific_slug):
            continue
        candidates.append((position, day, bool(_ROUNDUP_TITLE.search(title)), {"title": title[:140], "url": url, "date": published, "host": host.removeprefix("www.")}))
    if not candidates:
        return None
    # Source records can start with an older page even when the answer cites
    # today's direct reporting. Read citation order, then prefer the freshest
    # dated article. Never treat an uncited older lead as today's news.
    cited = [int(n) for n in re.findall(r"\[(\d{1,2})\]", str(found.get("text") or ""))]
    citation_order = {number: index for index, number in enumerate(cited)}
    # A dated story about a specific development is more informative than an
    # updated roundup. The provider's prose/citation order can be inconsistent
    # with its source list, so it is only a final tie-breaker.
    candidates.sort(key=lambda row: (-row[1].toordinal(), row[2], citation_order.get(row[0], 999), row[0]))
    return candidates[0][3]


def _search(axis: str, query: str, now: datetime) -> dict | None:
    if not perplexity_tools.perplexity_available():
        return None
    try:
        found = perplexity_tools.perplexity_search_with_sources(query, recency_days=2)
        lead = _dated_lead(found, now=now)
        if lead is None:
            # One bounded convergence step: a broad search can return a live
            # index instead of a direct article. Ask for the missing source
            # shape once, then stop rather than recycling that index as news.
            retry_query = (f"{query} Find a direct article published {now.date().isoformat()} "
                           "or yesterday whose URL contains its publication date. Exclude rolling feeds, indexes and trackers.")
            found = perplexity_tools.perplexity_search_with_sources(retry_query, recency_days=2)
            lead = _dated_lead(found, now=now)
        return {"axis": axis, **lead} if lead else None
    except Exception:
        logger.info("market today: %s reporting search unavailable", axis, exc_info=True)
        return None


async def compose(query: str) -> tuple[str, dict]:
    """Return a compact dated brief and an auditable evidence trajectory."""
    now = datetime.now(timezone.utc)
    streaming.emit("status", text="Reading market data and today's reporting")
    web_available = perplexity_tools.perplexity_available()
    x_available = x_news.enabled()
    outcomes = await asyncio.gather(
        asyncio.to_thread(market_overview.crypto_market_overview, query),
        *(asyncio.to_thread(_search, axis, search, now) for axis, search in _SEARCHES) if web_available else (),
        *(x_news.search('(crypto OR bitcoin OR ethereum OR solana) (market OR ETF OR hack OR regulation)', hours=48),) if x_available else (),
        return_exceptions=True,
    )
    overview = outcomes[0] if isinstance(outcomes[0], str) and "temporarily unavailable" not in outcomes[0] else ""
    leads = [item for item in outcomes[1:1 + (len(_SEARCHES) if web_available else 0)] if isinstance(item, dict)]
    # The two searches may cite the same article. One citation is enough.
    unique = list({item["url"]: item for item in leads}.values())
    x_result = outcomes[-1] if x_available and isinstance(outcomes[-1], dict) else None
    x_card = x_news.render(x_result or {})
    lines = ["# Crypto market today", f"**Checked** {now.strftime('%Y-%m-%d %H:%M UTC')}"]
    if unique:
        lines.extend(["", "## Dated reporting"])
        for item in unique:
            label = "Market news" if item["axis"] == "news" else "Flows and positioning"
            safe_title = item["title"].replace("[", "(").replace("]", ")")
            lines.append(f"- **{label}** · Published {item['date']}: [{safe_title}]({item['url']}) ({item['host']}).")
        lines.extend(["", "These are reported developments; the market snapshot alone does not establish that they caused the price moves."])
    else:
        lines.extend(["", "No recent, dated reporting could be linked from the available search results."])
    if x_card:
        lines.extend(["", x_card])
    if overview:
        lines.extend(["", "---", "", overview])
    else:
        lines.extend(["", "Live market data is temporarily unavailable."])
    trajectory = {"thought_0": "Parallel quantitative market snapshot and two dated reporting searches."}
    if overview:
        trajectory.update(tool_name_0="crypto_market_overview", tool_args_0={"query": query}, observation_0=overview)
    for index, (axis, search) in enumerate(_SEARCHES, start=1):
        if not web_available:
            break
        item = outcomes[index] if isinstance(outcomes[index], dict) else None
        trajectory[f"tool_name_{index}"] = "perplexity_web_search"
        trajectory[f"tool_args_{index}"] = {"axis": axis, "query": search}
        trajectory[f"observation_{index}"] = item or "No dated direct article found in this search axis."
    if x_available:
        index = 1 + len(_SEARCHES) if web_available else 1
        trajectory[f"tool_name_{index}"] = "x_news_search"
        trajectory[f"tool_args_{index}"] = {"topic": "crypto market", "hours": 48}
        trajectory[f"observation_{index}"] = x_card or "No recent X posts available."
    return "\n".join(lines), trajectory
