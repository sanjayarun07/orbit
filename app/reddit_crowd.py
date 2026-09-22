"""What Reddit is saying about a token, with real numbers.

From the last30days review (user decision, 2026-09-22): Reddit's crowd is a
different one from X's, and every thread carries real upvotes, an upvote
ratio, a comment count and its top comments. The keyless JSON and RSS
paths answered HTML block pages when tried live on 2026-09-22, so this uses
Reddit's official API with a free script app (client id + secret,
client-credentials grant, read-only), sixty requests a minute.

What happens per symbol: one search across the crypto subreddits for the
cashtag or the coin name in the last month, newest first; the top threads
by upvotes plus comments are enriched with their top comments (a bounded
number of thread fetches); deterministic statistics (threads, distinct
authors, subreddits, upvotes, comments, upvote ratio, span) and a sample of
the most-discussed threads with their best comments go to the judge and
the card. Cached per symbol for `social_sentiment_ttl_seconds`.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone

import httpx

from app.metrics import increment
from app.provider_router import NoData, ProviderRouter, ProviderTool
from app.settings import settings
from app.tool_catalog import TOOL_SPECS

logger = logging.getLogger(__name__)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"
ENRICH_SLOTS = 6            # threads whose comments are fetched (one call each)
TOP_COMMENTS = 4
SEARCH_LIMIT = 40
_TIMEOUT = 15.0

_token: tuple[float, str] | None = None
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()

REDDIT_ASK = re.compile(r"\breddit\b|\bsubreddit|\br/[A-Za-z]", re.I)


def enabled() -> bool:
    return bool(settings.reddit_client_id and settings.reddit_client_secret)


def reset_for_test() -> None:
    global _token
    with _lock:
        _cache.clear()
        _token = None


def _headers() -> dict:
    return {"User-Agent": settings.reddit_user_agent}


def _access_token() -> str:
    global _token
    now = time.monotonic()
    with _lock:
        if _token and _token[0] > now:
            return _token[1]
    with httpx.Client(timeout=_TIMEOUT, headers=_headers()) as client:
        response = client.post(TOKEN_URL, data={"grant_type": "client_credentials"}, auth=(settings.reddit_client_id or "", settings.reddit_client_secret or ""))
        response.raise_for_status()
        data = response.json()
    token = str(data.get("access_token") or "")
    if not token:
        raise RuntimeError("Reddit returned no access token")
    with _lock:
        _token = (now + float(data.get("expires_in") or 3600) - 60, token)
    return token


def _get(path: str, params: dict) -> dict | list:
    increment("reddit_requests")
    with httpx.Client(timeout=_TIMEOUT, headers={**_headers(), "Authorization": f"Bearer {_access_token()}"}) as client:
        response = client.get(f"{API}{path}", params=params)
        response.raise_for_status()
        return response.json()


def query_for(symbol: str, name: str | None = None) -> str:
    sym = symbol.strip().lstrip("$").upper()
    terms = [f'"${sym}"', f'"{sym}"']
    if name and re.fullmatch(r"[A-Za-z][A-Za-z0-9 ]{2,24}", name) and name.upper() != sym:
        terms.append(f'"{name}"')
    return " OR ".join(terms)


def _post(row: dict) -> dict | None:
    if not isinstance(row, dict) or not row.get("id"):
        return None
    created = row.get("created_utc")
    return {
        "id": str(row["id"]), "subreddit": str(row.get("subreddit") or ""), "title": str(row.get("title") or "")[:300],
        "text": str(row.get("selftext") or "")[:1500], "author": str(row.get("author") or "") or None,
        "score": int(row.get("score") or 0), "upvote_ratio": float(row.get("upvote_ratio") or 0.0), "comments": int(row.get("num_comments") or 0),
        "url": f"https://www.reddit.com{row.get('permalink')}" if row.get("permalink") else None, "permalink": row.get("permalink"),
        "created_at": datetime.fromtimestamp(float(created), tz=timezone.utc) if created else None,
        "top_comments": [],
    }


def _grounded(post: dict, symbol: str, name: str | None) -> bool:
    """The thread names the token: a cashtag or hashtag in any case, the
    ticker written in capitals as a word (Reddit rarely uses cashtags), or
    the coin name. "Bonk my head" in title case is the verb, not the token."""
    text = f"{post.get('title', '')} {post.get('text', '')}"
    sym = symbol.strip().lstrip("$").upper()
    if re.search(rf"(?i)(?:\$|#){re.escape(sym)}\b", text) or re.search(rf"\b{re.escape(sym)}\b", text):
        return True
    return bool(name and re.search(rf"(?i)\b{re.escape(name.strip())}\b", text))


def search(symbol: str, name: str | None = None) -> list[dict]:
    """Threads about the symbol from the crypto subreddits, last month, newest first."""
    subs = "+".join(s.strip() for s in settings.reddit_subreddits.split(",") if s.strip())
    data = _get(f"/r/{subs}/search", {"q": query_for(symbol, name), "restrict_sr": 1, "sort": "new", "t": "month", "limit": SEARCH_LIMIT, "raw_json": 1})
    children = ((data or {}).get("data") or {}).get("children") or [] if isinstance(data, dict) else []
    posts = [p for p in (_post(ch.get("data") or {}) for ch in children if isinstance(ch, dict)) if p]
    return [p for p in posts if _grounded(p, symbol, name)]


def enrich(post: dict) -> dict:
    """The thread's top comments (score and text), best first."""
    if not post.get("permalink"):
        return post
    try:
        data = _get(f"{post['permalink'].rstrip('/')}", {"limit": 12, "depth": 1, "sort": "top", "raw_json": 1})
    except Exception:
        logger.info("reddit_crowd: thread %s enrichment failed", post.get("id"), exc_info=True)
        return post
    comments = []
    listing = data[1] if isinstance(data, list) and len(data) > 1 else {}
    for ch in ((listing.get("data") or {}).get("children") or []):
        d = ch.get("data") or {} if isinstance(ch, dict) else {}
        body = str(d.get("body") or "").strip()
        if not body or d.get("stickied") or body in ("[removed]", "[deleted]"):
            continue
        comments.append({"score": int(d.get("score") or 0), "author": d.get("author"), "text": body[:400]})
    comments.sort(key=lambda c: -c["score"])
    return {**post, "top_comments": comments[:TOP_COMMENTS]}


def stats(posts: list[dict]) -> dict:
    n = len(posts)
    if not n:
        return {"threads": 0, "unique_authors": 0, "subreddits": [], "total_upvotes": 0, "total_comments": 0, "avg_upvote_ratio": None, "span_days": None, "sample": []}
    authors = {p.get("author") for p in posts if p.get("author")}
    subs: dict[str, int] = {}
    for p in posts:
        subs[p["subreddit"]] = subs.get(p["subreddit"], 0) + 1
    ratios = [p["upvote_ratio"] for p in posts if p.get("upvote_ratio")]
    times = [p["created_at"] for p in posts if p.get("created_at")]
    span = (max(times) - min(times)).total_seconds() / 86400.0 if len(times) >= 2 else None
    ranked = sorted(posts, key=lambda p: p["score"] + 2 * p["comments"], reverse=True)
    sample = [{"subreddit": p["subreddit"], "title": p["title"], "score": p["score"], "comments": p["comments"], "upvote_ratio": p["upvote_ratio"],
               "when": p["created_at"].strftime("%Y-%m-%d") if p.get("created_at") else None, "url": p.get("url"),
               "excerpt": (p.get("text") or "")[:300], "top_comments": [{"score": c["score"], "text": c["text"][:240]} for c in p.get("top_comments") or []]}
              for p in ranked[:10]]
    return {"threads": n, "unique_authors": len(authors), "subreddits": sorted(subs, key=lambda s: -subs[s])[:6], "total_upvotes": sum(p["score"] for p in posts),
            "total_comments": sum(p["comments"] for p in posts), "avg_upvote_ratio": round(sum(ratios) / len(ratios), 2) if ratios else None,
            "span_days": round(span, 1) if span is not None else None, "sample": sample}


def gather(symbol: str, name: str | None = None) -> dict:
    """Search, enrich the most-discussed threads, measure. Cached per symbol."""
    sym = symbol.strip().lstrip("$").upper()
    now = time.monotonic()
    with _lock:
        hit = _cache.get(sym)
        if hit and hit[0] > now:
            return hit[1]
    if not enabled():
        raise RuntimeError("REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are not configured")
    posts = search(sym, name)
    ranked = sorted(posts, key=lambda p: p["score"] + 2 * p["comments"], reverse=True)
    enriched = {p["id"]: enrich(p) for p in ranked[:ENRICH_SLOTS]}
    posts = [enriched.get(p["id"], p) for p in posts]
    out = {"symbol": sym, "query": query_for(sym, name), "posts": posts, "stats": stats(posts)}
    with _lock:
        _cache[sym] = (now + max(60, settings.social_sentiment_ttl_seconds), out)
    return out


def render_card(symbol: str, gathered: dict) -> str:
    s = gathered["stats"]
    lines = ["# Reddit", f"**Source**: Reddit API (crypto subreddits, last 30 days) · **Token**: ${symbol} · **Checked**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", ""]
    if not s["threads"]:
        lines += [f"No threads mentioning ${symbol} in r/{', r/'.join(x.strip() for x in settings.reddit_subreddits.split(',')[:4])} and the other crypto subreddits this month.", ""]
        lines += [f"Query: `{gathered.get('query', '')}`."]
        return "\n".join(lines)
    lines += [f"**{s['threads']} threads** by {s['unique_authors']} authors in r/{', r/'.join(s['subreddits'])}"
              + (f" over {s['span_days']} days" if s.get("span_days") is not None else "") +
              f" · {s['total_upvotes']:,} upvotes, {s['total_comments']:,} comments · avg upvote ratio {s['avg_upvote_ratio'] if s['avg_upvote_ratio'] is not None else '—'}", ""]
    lines += ["| Thread | Sub | Upvotes | Comments | Top comment |", "|---|---|---:|---:|---|"]
    for t in s["sample"][:6]:
        top = (t["top_comments"][0]["text"].replace("\n", " ").replace("|", "/")[:120] + (f" (+{t['top_comments'][0]['score']})" if t["top_comments"] else "")) if t["top_comments"] else "—"
        title = t["title"].replace("|", "/")[:80]
        link = f"[{title}]({t['url']})" if t.get("url") else title
        lines.append(f"| {link} | r/{t['subreddit']} | {t['score']:,} | {t['comments']:,} | {top} |")
    lines += ["", f"Query: `{gathered.get('query', '')}`. Upvotes and comments are Reddit's own counts; the crowd here skews to holders and hobbyists, and a thread's score says how many agreed, not whether it is right."]
    return "\n".join(lines)


def reddit_card(request: str) -> str:
    """Router handler for "what is reddit saying about X"."""
    from app.social_sentiment import extract_symbol

    symbol = extract_symbol(request)
    if not symbol:
        raise ValueError("Name the token, e.g. 'what is reddit saying about BONK'")
    gathered = gather(symbol)
    if not gathered["stats"]["threads"]:
        raise NoData(f"no Reddit threads mention ${symbol} this month")
    return render_card(symbol, gathered)


class RedditCrowdProvider:
    name = "reddit"

    def enabled(self) -> bool:
        return enabled()

    def register(self, router: ProviderRouter) -> None:
        from app.social_sentiment import extract_symbol

        router.register(ProviderTool(
            "reddit_crowd", self.name, ("market_sentiment",), reddit_card,
            enabled=enabled,
            matches=lambda request: bool(REDDIT_ASK.search(request or "")) and extract_symbol(request) is not None,
            keywords=("reddit", "subreddit", "what is reddit saying"),
            chains=(), cache_ttl_seconds=600, priority=14, spec=TOOL_SPECS.get("reddit_crowd"),
            description="Reddit threads about a token from the crypto subreddits with real upvotes, comment counts and top comments",
        ))
