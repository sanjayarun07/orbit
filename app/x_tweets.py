"""Tweets about a token from TwitterAPI.io, cached by id.

User decision (2026-09-21): use TwitterAPI.io as the tweet source for the
sentiment analyst. The pattern comes from brainstormity/Jev-X-Sentiment-
Analysis: page the advanced search newest-first and stop the moment a tweet
already in the store appears, since everything older is already there --
so an intraday re-ask pays only for the tweets posted since the last one.

What is kept per tweet: id, author, follower count, verified flag, text,
likes, retweets, replies, created time. Deterministic statistics over the
sample live here too (`stats`): author diversity (a bot farm is a few
authors posting a lot), engagement, the share of verified authors, and a
stratified sample of the 25 most engaged plus the 25 latest tweets -- what
the judge reads, so 1,000 tweets never go into one prompt.

No keyword polarity: "hold", "up" and "long" are not sentiment. The judge
reads the tweets (app/sentiment_analyst.py).
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

import httpx

from app.db import get_pg_pool
from app.metrics import increment
from app.settings import settings

logger = logging.getLogger(__name__)

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS x_tweets (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    author TEXT,
    followers INTEGER,
    verified BOOLEAN,
    text TEXT NOT NULL,
    likes INTEGER NOT NULL DEFAULT 0,
    retweets INTEGER NOT NULL DEFAULT 0,
    replies INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS x_tweets_symbol_time ON x_tweets (symbol, created_at DESC);
"""
_ready = False
_memory: dict[str, dict[str, dict]] = {}       # symbol -> id -> tweet
_lock = threading.Lock()

PAGE_SIZE = 20
TOP_ENGAGED = 25
LATEST = 25
_QUOTES = {"SOL", "USDC", "USDT", "ETH", "BTC", "BNB"}


def enabled() -> bool:
    return bool(settings.twitterapi_io_key)


def reset_for_test() -> None:
    global _ready
    with _lock:
        _memory.clear()
    _ready = False


def query_for(symbol: str, name: str | None = None) -> str:
    """The advanced-search query: the cashtag (and the name when it is a
    real word), English, no retweets, at least two likes so pure spam bots
    fall out before we pay for them."""
    sym = symbol.strip().lstrip("$").upper()
    if name and re.fullmatch(r"[A-Za-z][A-Za-z0-9 ]{2,24}", name) and name.upper() != sym:
        return f"(${sym} OR \"{name}\") lang:en -is:retweet min_faves:2"
    return f"${sym} lang:en -is:retweet min_faves:2"


def _dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(str(value), fmt).astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize(raw: dict) -> dict | None:
    """One TwitterAPI.io tweet as the row we keep. None without an id or text."""
    tid = str(raw.get("id") or "")
    text = str(raw.get("text") or "").strip()
    if not tid or not text:
        return None
    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    return {
        "id": tid, "text": text[:2000],
        "author": str(author.get("userName") or author.get("username") or raw.get("author_username") or "") or None,
        "followers": int(author.get("followers") or author.get("followersCount") or 0),
        "verified": bool(author.get("isBlueVerified") or author.get("verified") or False),
        "likes": int(raw.get("likeCount") or raw.get("likes") or 0),
        "retweets": int(raw.get("retweetCount") or raw.get("retweets") or 0),
        "replies": int(raw.get("replyCount") or raw.get("replies") or 0),
        "created_at": _dt(raw.get("createdAt") or raw.get("created_at")),
    }


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

async def _pool():
    global _ready
    try:
        pool = await get_pg_pool()
    except Exception:
        return None
    if pool is not None and not _ready:
        try:
            async with pool.acquire() as conn:
                await conn.execute(_TABLE_SQL)
            _ready = True
        except Exception:
            logger.warning("x_tweets: schema not ready; using memory store", exc_info=True)
            return None
    return pool


async def known_ids(symbol: str, hours: float) -> set[str]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    pool = await _pool()
    if pool is not None:
        rows = await pool.fetch("SELECT id FROM x_tweets WHERE symbol = $1 AND fetched_at >= $2", symbol, since)
        return {r["id"] for r in rows}
    with _lock:
        return {tid for tid, t in _memory.get(symbol, {}).items() if t.get("fetched_at", since) >= since}


async def recent(symbol: str, hours: float, limit: int) -> list[dict]:
    """Stored tweets for the symbol from the last `hours`, newest first."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    pool = await _pool()
    if pool is not None:
        rows = await pool.fetch("SELECT id, author, followers, verified, text, likes, retweets, replies, created_at FROM x_tweets "
                                "WHERE symbol = $1 AND COALESCE(created_at, fetched_at) >= $2 ORDER BY COALESCE(created_at, fetched_at) DESC LIMIT $3",
                                symbol, since, limit)
        return [dict(r) for r in rows]
    with _lock:
        rows = [dict(t) for t in _memory.get(symbol, {}).values() if (t.get("created_at") or t.get("fetched_at") or since) >= since]
    rows.sort(key=lambda t: t.get("created_at") or t.get("fetched_at") or since, reverse=True)
    return rows[:limit]


async def save(symbol: str, tweets: list[dict]) -> None:
    now = datetime.now(timezone.utc)
    pool = await _pool()
    if pool is not None:
        await pool.executemany(
            "INSERT INTO x_tweets (id, symbol, author, followers, verified, text, likes, retweets, replies, created_at, fetched_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) ON CONFLICT (id) DO UPDATE SET likes = EXCLUDED.likes, retweets = EXCLUDED.retweets, replies = EXCLUDED.replies",
            [(t["id"], symbol, t.get("author"), t.get("followers"), t.get("verified"), t["text"], t["likes"], t["retweets"], t["replies"], t.get("created_at"), now)
             for t in tweets])
        return
    with _lock:
        bucket = _memory.setdefault(symbol, {})
        for t in tweets:
            bucket[t["id"]] = {**t, "fetched_at": now}


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------

def _page(query: str, cursor: str | None) -> dict:
    params = {"query": query, "queryType": "Latest"}
    if cursor:
        params["cursor"] = cursor
    increment("twitterapi_requests")
    with httpx.Client(timeout=settings.twitterapi_io_timeout_seconds) as client:
        response = client.get(f"{settings.twitterapi_io_base_url}/twitter/tweet/advanced_search", params=params,
                              headers={"X-API-Key": settings.twitterapi_io_key})
        response.raise_for_status()
        return response.json()


async def fetch(symbol: str, target: int | None = None, *, name: str | None = None) -> dict:
    """Up to `target` tweets about `symbol`: the newest from the API until a
    known tweet appears, then the store fills the rest. Returns the tweets
    (newest first) with how many were new and whether paging stopped early."""
    import asyncio

    sym = symbol.strip().lstrip("$").upper()
    target = max(10, min(int(target or settings.x_tweets_default_sample), 1000))
    if not enabled():
        raise RuntimeError("TWITTERAPI_IO_KEY is not configured")
    seen = await known_ids(sym, settings.x_tweets_cache_hours)
    query = query_for(sym, name)
    fresh: list[dict] = []
    cursor: str | None = None
    early_stop = False
    pages = 0
    max_pages = max(1, (target + PAGE_SIZE - 1) // PAGE_SIZE)
    while len(fresh) < target and pages < max_pages:
        pages += 1
        try:
            data = await asyncio.to_thread(_page, query, cursor)
        except Exception:
            logger.warning("x_tweets: page %d failed for %s", pages, sym, exc_info=True)
            break
        raw = data.get("tweets") or data.get("data") or []
        if not raw:
            break
        for item in raw:
            tweet = normalize(item) if isinstance(item, dict) else None
            if tweet is None:
                continue
            if tweet["id"] in seen:
                early_stop = True
                break
            fresh.append(tweet)
        if early_stop or not data.get("has_next_page", True):
            break
        cursor = data.get("next_cursor") or data.get("cursor")
        if not cursor:
            break
    if fresh:
        await save(sym, fresh)
    tweets = list(fresh)
    if len(tweets) < target:
        have = {t["id"] for t in tweets}
        for t in await recent(sym, settings.x_tweets_cache_hours, target * 2):
            if t["id"] not in have:
                tweets.append(t)
                have.add(t["id"])
            if len(tweets) >= target:
                break
    return {"symbol": sym, "query": query, "tweets": tweets, "count": len(tweets), "new": len(fresh), "pages": pages, "early_stop": early_stop}


# ---------------------------------------------------------------------------
# deterministic statistics
# ---------------------------------------------------------------------------

AUTHOR_CAP = 3            # tweets one account may contribute to the judged sample
# Promotion is not sentiment: whitelist drops, presales, giveaways and referral
# pushes around a ticker read as "bullish" to a judge and mean nothing about the
# crowd's view of the token (live: $ZEC read 93% bullish from NFT whitelist spam).
PROMO = re.compile(r"\b(?:whitelist|white\s*list|allowlist|allow\s*list|ghostlist|wl\s+spots?|wl\s+(?:is\s+)?open|airdrop|presale|pre-sale|public\s+(?:sale|auction|mint)|"
                   r"auction|giveaway|claim\s+(?:your|now)|mint(?:ing)?\s+(?:is\s+)?(?:live|open|now|soon)|free\s+mint|referral|link\s+in\s+bio|dm\s+me|"
                   r"join\s+(?:our|the)\s+(?:tg|telegram|discord)|launching\s+soon|early\s+access|spots?\s+(?:left|open|available))\b", re.I)


def promotional(tweet: dict) -> bool:
    return bool(PROMO.search(tweet.get("text") or ""))


def grounded(tweet: dict, symbol: str, name: str | None = None) -> bool:
    """Does the tweet actually talk about the token? The cashtag, the hashtag,
    or the name as a word. A post that merely contains the letters (BONK the
    verb, "open" the adjective) is not evidence about the token, and a viral
    one must not hijack the sample (last30days' entity grounding)."""
    text = (tweet.get("text") or "")
    sym = re.escape(symbol.strip().lstrip("$"))
    if re.search(rf"(?i)(?:\$|#){sym}\b", text):
        return True
    if name and re.search(rf"(?i)\b{re.escape(name.strip())}\b", text):
        return True
    return False


def stats(tweets: list[dict], symbol: str | None = None, name: str | None = None) -> dict:
    """Numbers computed in code before any model reads a tweet. With a symbol,
    ungrounded tweets are dropped first and one author is capped at
    AUTHOR_CAP tweets (the drops are counted, never hidden)."""
    dropped_ungrounded = 0
    capped = 0
    if symbol:
        kept = []
        for t in tweets:
            if grounded(t, symbol, name):
                kept.append(t)
            else:
                dropped_ungrounded += 1
        tweets = kept
        # The judged sample: one account contributes at most AUTHOR_CAP tweets
        # (its most engaged), so a loud account cannot be the crowd.
        per_author: dict[str, int] = {}
        limited = []
        for t in sorted(tweets, key=lambda t: int(t.get("likes") or 0) + 2 * int(t.get("retweets") or 0), reverse=True):
            a = (t.get("author") or "unknown").lower()
            if per_author.get(a, 0) >= AUTHOR_CAP:
                capped += 1
                continue
            per_author[a] = per_author.get(a, 0) + 1
            limited.append(t)
        tweets = limited
    n = len(tweets)
    if not n:
        return {"sample_size": 0, "unique_authors": 0, "author_diversity_pct": 0.0, "total_likes": 0, "total_retweets": 0, "avg_engagement": 0.0,
                "verified_share_pct": 0.0, "top_author_share_pct": 0.0, "span_hours": None, "sample": [],
                "dropped_ungrounded": dropped_ungrounded, "capped_by_author": capped, "promo_share_pct": 0.0}
    authors: dict[str, int] = {}
    likes = retweets = 0
    verified = 0
    for t in tweets:
        a = (t.get("author") or "unknown").lower()
        authors[a] = authors.get(a, 0) + 1
        likes += int(t.get("likes") or 0)
        retweets += int(t.get("retweets") or 0)
        verified += 1 if t.get("verified") else 0
    times = [t["created_at"] for t in tweets if t.get("created_at")]
    span = (max(times) - min(times)).total_seconds() / 3600.0 if len(times) >= 2 else None
    promo = sum(1 for t in tweets if promotional(t))
    by_engagement = sorted(tweets, key=lambda t: int(t.get("likes") or 0) + 2 * int(t.get("retweets") or 0), reverse=True)
    latest = sorted(tweets, key=lambda t: t.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    sample, seen = [], set()
    for kind, rows in (("high_engagement", by_engagement[:TOP_ENGAGED]), ("latest", latest[:LATEST])):
        for t in rows:
            if t["id"] in seen:
                continue
            seen.add(t["id"])
            sample.append({"author": t.get("author"), "followers": t.get("followers"), "verified": bool(t.get("verified")), "likes": int(t.get("likes") or 0),
                           "retweets": int(t.get("retweets") or 0), "when": t["created_at"].strftime("%Y-%m-%d %H:%M") if t.get("created_at") else None,
                           "text": (t.get("text") or "")[:400], "kind": kind})
    return {
        "sample_size": n, "unique_authors": len(authors), "author_diversity_pct": round(len(authors) / n * 100.0, 1),
        "total_likes": likes, "total_retweets": retweets, "avg_engagement": round((likes + 2 * retweets) / n, 1),
        "verified_share_pct": round(verified / n * 100.0, 1), "top_author_share_pct": round(max(authors.values()) / n * 100.0, 1),
        "span_hours": round(span, 1) if span is not None else None, "sample": sample,
        "dropped_ungrounded": dropped_ungrounded, "capped_by_author": capped, "promo_share_pct": round(promo / n * 100.0, 1),
    }
