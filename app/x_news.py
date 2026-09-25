"""Bounded X-post discovery for current news and market context.

Posts show what an account said, not whether its claim is true. Callers must
keep them separate from measured market data and checked reporting.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import re
import threading
import time

import httpx

from app import x_tweets

logger = logging.getLogger(__name__)

_disabled_until = 0.0
_lock = threading.Lock()
_PAYMENT_RETRY_SECONDS = 1800
_MAX_POSTS = 3
_QUERY_STOP = {"about", "actually", "after", "amid", "before", "breaking", "crypto", "from", "happened",
               "into", "latest", "market", "markets", "news", "new", "proposes", "says", "that", "their",
               "these", "today", "what", "when", "where", "which", "why", "with"}


def enabled() -> bool:
    with _lock:
        disabled = time.monotonic() < _disabled_until
    return x_tweets.enabled() and not disabled


def reset_for_test() -> None:
    global _disabled_until
    with _lock:
        _disabled_until = 0.0


def _expression(topic: str, since: datetime) -> str:
    """Keep caller-selected subjects; constrain recency and obvious spam."""
    subject = re.sub(r"\s+", " ", topic).strip()[:180]
    if not subject:
        return ""
    return f"{subject} since:{since.date().isoformat()} lang:en -is:retweet min_faves:5"


def topic_from_headline(headline: str) -> str:
    """Use a few subject terms instead of a brittle exact-title search."""
    words = [word for word in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", headline or "")
             if word.lower() not in _QUERY_STOP]
    return " ".join(dict.fromkeys(words[:3])) or "(crypto OR bitcoin OR ethereum)"


def _select(raw: list, since: datetime, limit: int) -> list[dict]:
    rows: list[dict] = []
    authors: set[str] = set()
    latest = datetime.now(timezone.utc) + timedelta(minutes=5)
    for item in raw:
        if not isinstance(item, dict):
            continue
        row = x_tweets.normalize(item)
        if not row or not row["created_at"] or not since <= row["created_at"] <= latest:
            continue
        author = str(row.get("author") or "").strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", author) or not row["id"].isdigit() or author.lower() in authors:
            continue
        authors.add(author.lower())
        rows.append({"id": row["id"], "author": author, "created_at": row["created_at"].isoformat(),
                     "text": row["text"][:280], "url": f"https://x.com/{author}/status/{row['id']}",
                     "likes": row["likes"]})
    rows.sort(key=lambda row: row["created_at"], reverse=True)
    return rows[:limit]


async def search(topic: str, *, hours: int = 48, limit: int = _MAX_POSTS) -> dict:
    """Read one latest page. A payment failure opens a short process breaker."""
    global _disabled_until
    if not enabled():
        return {"status": "unavailable", "posts": []}
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, min(hours, 168)))
    expression = _expression(topic, since)
    if not expression:
        return {"status": "empty_query", "posts": []}
    try:
        data = await asyncio.to_thread(x_tweets._page, expression, None)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 402, 403):
            with _lock:
                _disabled_until = time.monotonic() + _PAYMENT_RETRY_SECONDS
            logger.warning("X discovery unavailable (HTTP %s); pausing calls", exc.response.status_code)
        else:
            logger.info("X discovery failed (HTTP %s)", exc.response.status_code)
        return {"status": "unavailable", "posts": []}
    except Exception:
        logger.info("X discovery request failed", exc_info=True)
        return {"status": "unavailable", "posts": []}
    return {"status": "ok", "query": expression,
            "posts": _select(data.get("tweets") or data.get("data") or [], since, max(1, min(limit, _MAX_POSTS)))}


def render(result: dict) -> str:
    posts = result.get("posts") or []
    if not posts:
        return ""
    lines = ["## X posts (unverified)"]
    for post in posts:
        excerpt = re.sub(r"[\r\n]+", " ", post["text"]).strip()
        excerpt = re.sub(r"([\\`*_{}\[\]()#+.!|>])", r"\\\1", excerpt)
        lines.append(f"- [@{post['author']}]({post['url']}) · {post['created_at'][:16].replace('T', ' ')} UTC: {excerpt}")
    lines.append("Posts show what accounts said; claims and market impact require independent verification.")
    return "\n".join(lines)
