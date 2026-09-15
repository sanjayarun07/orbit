"""X / KOL sentiment: what accounts with real reach are saying about a token
in the last day or two, as a ranked card rather than a vibe.

Three data paths, chosen by configuration and stated on the card:
- LunarCrush API v4 (`LUNARCRUSH_API_KEY`): measured social metrics -- galaxy
  score, alt rank, sentiment, 24h interactions and contributors, the top
  creators by reach and the top posts -- the most complete and reproducible.
- X API v2 recent search (`X_BEARER_TOKEN`): real posts with public metrics;
  stance is a transparent lexicon score weighted by reach (followers) and
  engagement (likes + reposts), so the numbers are reproducible.
- Perplexity web search (default): asked for strict JSON over posts on X by
  large accounts, analysts and the project itself, with handles and URLs.

Registered as the `x_kol_sentiment` provider tool (capability
market_sentiment), so "what is crypto twitter saying about BONK" reaches it
through the normal router; cached per token for SOCIAL_SENTIMENT_TTL_SECONDS.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

import httpx

from app.perplexity_tools import _invoke as perplexity_invoke, perplexity_available
from app.settings import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[str, tuple[float, str]] = {}

TRIGGER = re.compile(
    r"\b(?:kols?|crypto\s+twitter|\bct\b|twitter|on\s+x\b|x\.com|x\s+sentiment|social\s+sentiment|"
    r"what\s+(?:are|is)\s+(?:people|traders|influencers|kols?|twitter|ct|x)\s+saying|"
    r"sentiment\s+(?:on|for|about|around)|(?:buzz|chatter|talking)\s+about)\b",
    re.IGNORECASE,
)
_SYMBOL = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b|\b(?:about|on|for|around|regarding)\s+\$?([A-Za-z][A-Za-z0-9]{1,9})\b|\b([A-Z]{2,10})\b")
_STOP = {"X", "CT", "KOL", "KOLS", "THE", "AND", "FOR", "ON", "ABOUT", "WHAT", "TWITTER", "CRYPTO", "SENTIMENT", "SAYING", "PEOPLE", "TODAY", "NOW", "IS", "ARE", "SOCIAL"}
_BULL = ("bullish", "buy", "long", "breakout", "moon", "accumulate", "undervalued", "send it", "ath", "pump", "strong", "up only", "rally")
_BEAR = ("bearish", "sell", "short", "dump", "rug", "scam", "overvalued", "exit", "crash", "bleed", "dead", "avoid", "down bad", "liquidat")


def extract_symbol(request: str) -> str | None:
    for match in _SYMBOL.finditer(request or ""):
        sym = next((g for g in match.groups() if g), None)
        if sym and sym.upper() not in _STOP:
            return sym.upper()
    return None


def matches(request: str) -> bool:
    return bool(TRIGGER.search(request or "")) and extract_symbol(request) is not None


def x_kol_sentiment(request: str) -> str:
    symbol = extract_symbol(request)
    if not symbol:
        raise ValueError("Name the token, e.g. 'what is crypto twitter saying about BONK'")
    now = time.monotonic()
    with _lock:
        cached = _cache.get(symbol)
        if cached and cached[0] > now:
            return cached[1]
    card = _from_lunarcrush(symbol) if settings.lunarcrush_api_key else None
    if card is None and settings.x_bearer_token:
        card = _from_x_api(symbol)
    if card is None:
        card = _from_perplexity(symbol)
    if card is None:
        raise RuntimeError("No social data source is configured (set LUNARCRUSH_API_KEY, X_BEARER_TOKEN or PERPLEXITY_API_KEY)")
    with _lock:
        _cache[symbol] = (now + max(60, settings.social_sentiment_ttl_seconds), card)
    return card


# ----------------------------------------------------------------------------
# LunarCrush path (measured social data)
# ----------------------------------------------------------------------------

_LC = "https://lunarcrush.com/api4/public"


def _lc_get(client: "httpx.Client", path: str) -> dict | list | None:
    resp = client.get(f"{_LC}{path}", headers={"Authorization": f"Bearer {settings.lunarcrush_api_key}"})
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    payload = resp.json()
    if isinstance(payload, dict) and payload.get("error"):
        return None
    return payload.get("data") if isinstance(payload, dict) else payload


def _from_lunarcrush(symbol: str) -> str | None:
    try:
        with httpx.Client(timeout=12) as client:
            coin = _lc_get(client, f"/coins/{symbol.lower()}/v1") or {}
            topics = [str(coin.get("name") or "").lower(), f"${symbol.lower()}", symbol.lower()]
            topic = posts = creators = None
            for candidate in [t for t in dict.fromkeys(topics) if t]:
                safe = re.sub(r"[^a-z0-9 #$]", "", candidate)
                topic = _lc_get(client, f"/topic/{safe}/v1")
                if topic:
                    posts = _lc_get(client, f"/topic/{safe}/posts/v1") or []
                    creators = _lc_get(client, f"/topic/{safe}/creators/v1") or []
                    break
    except Exception:
        logger.warning("social_sentiment: LunarCrush failed for %s; falling back", symbol, exc_info=True)
        return None
    if not coin and not topic:
        return None
    topic = topic or {}
    sentiment = topic.get("sentiment") if topic.get("sentiment") is not None else coin.get("sentiment")
    read = "bullish" if sentiment is not None and float(sentiment) >= 65 else "bearish" if sentiment is not None and float(sentiment) <= 40 else "mixed"
    lines = [
        f"# Social sentiment — {symbol}",
        f"**Provider**: LunarCrush API v4 · **As of**: {_utc()}" + (f" · topic `{topic.get('topic')}`" if topic.get("topic") else ""),
        "",
        f"**Read**: **{read}**" + (f" — sentiment {float(sentiment):.0f}/100" if sentiment is not None else "") + (f", trend {topic['trend']}" if topic.get("trend") else ""),
        "",
        "## Measured",
        "| Metric | Value |", "|---|---:|",
    ]
    metrics = [
        ("Galaxy Score™", coin.get("galaxy_score")), ("AltRank™", coin.get("alt_rank")), ("Topic rank", topic.get("topic_rank")),
        ("Interactions (24h)", topic.get("interactions_24h") or coin.get("interactions_24h")), ("Posts (24h)", topic.get("num_posts")),
        ("Contributors (24h)", topic.get("num_contributors")), ("Social dominance", coin.get("social_dominance")),
        ("Price", coin.get("price")), ("24h change", coin.get("percent_change_24h")),
    ]
    for label, value in metrics:
        if value is None:
            continue
        if label in ("Price",):
            cell = f"${float(value):,.6g}"
        elif label in ("24h change", "Social dominance"):
            cell = f"{float(value):+.2f}%" if label == "24h change" else f"{float(value):.2f}%"
        elif isinstance(value, (int, float)):
            cell = f"{value:,.0f}" if float(value) >= 100 else f"{value}"
        else:
            cell = str(value)
        lines.append(f"| {label} | {cell} |")
    types = topic.get("types_sentiment") or {}
    if types:
        lines += ["", "**Sentiment by network**: " + " · ".join(f"{k}: {float(v):.0f}" for k, v in list(types.items())[:6])]
    if creators:
        lines += ["", "## Top creators (24h reach)", "| Creator | Followers | Interactions |", "|---|---:|---:|"]
        for c in sorted(creators, key=lambda c: -(c.get("interactions_24h") or 0))[:6]:
            lines.append(f"| {c.get('creator_name') or '?'} | {int(c.get('creator_followers') or 0):,} | {int(c.get('interactions_24h') or 0):,} |")
    if posts:
        lines += ["", "## Top posts", "| Source | Creator | Followers | Interactions | Post |", "|---|---|---:|---:|---|"]
        for p in sorted(posts, key=lambda p: -(p.get("interactions_24h") or 0))[:6]:
            title = str(p.get("post_title") or "").replace("|", "/").replace("\n", " ")[:110]
            link = p.get("post_link")
            cell = f"[{title}]({link})" if link and title else (title or "(untitled)")
            lines.append(f"| {p.get('post_type') or '?'} | {p.get('creator_name') or '?'} | {int(p.get('creator_followers') or 0):,} | {int(p.get('interactions_24h') or 0):,} | {cell} |")
    lines += ["", "*LunarCrush measures social volume, engagement and sentiment across X, Reddit, YouTube and TikTok; a high score means attention, not a price call. Loud accounts can be paid or positioned.*"]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# X API path
# ----------------------------------------------------------------------------

def _stance(text: str) -> int:
    lowered = (text or "").lower()
    score = sum(1 for w in _BULL if w in lowered) - sum(1 for w in _BEAR if w in lowered)
    return max(-1, min(1, score))


def _from_x_api(symbol: str) -> str | None:
    try:
        with httpx.Client(timeout=12) as client:
            resp = client.get(
                "https://api.x.com/2/tweets/search/recent",
                params={
                    "query": f'(${symbol} OR #{symbol} OR "{symbol}") -is:retweet lang:en',
                    "max_results": 50, "tweet.fields": "public_metrics,created_at,author_id",
                    "expansions": "author_id", "user.fields": "public_metrics,username,verified",
                },
                headers={"Authorization": f"Bearer {settings.x_bearer_token}"},
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception:
        logger.warning("social_sentiment: X API failed for %s; falling back", symbol, exc_info=True)
        return None
    users = {u["id"]: u for u in (payload.get("includes") or {}).get("users") or []}
    posts = []
    for tweet in payload.get("data") or []:
        user = users.get(tweet.get("author_id")) or {}
        followers = int(((user.get("public_metrics") or {}).get("followers_count")) or 0)
        metrics = tweet.get("public_metrics") or {}
        engagement = int(metrics.get("like_count") or 0) + 2 * int(metrics.get("retweet_count") or 0)
        posts.append({
            "author": f"@{user.get('username', '?')}", "followers": followers, "engagement": engagement,
            "stance": _stance(tweet.get("text")), "text": (tweet.get("text") or "").replace("\n", " ")[:200],
            "url": f"https://x.com/{user.get('username', 'i')}/status/{tweet.get('id')}", "reach": followers + 20 * engagement,
        })
    if not posts:
        return None
    posts.sort(key=lambda p: -p["reach"])
    total = sum(p["reach"] for p in posts) or 1
    score = sum(p["stance"] * p["reach"] for p in posts) / total
    label = "bullish" if score > 0.2 else "bearish" if score < -0.2 else "mixed"
    lines = [
        f"# X / KOL sentiment — {symbol}",
        f"**Provider**: X API v2 (recent search, last 7 days) · **As of**: {_utc()} · {len(posts)} posts",
        "",
        f"**Read**: **{label}** (reach-weighted stance {score:+.2f}; {sum(p['stance'] > 0 for p in posts)} bullish · {sum(p['stance'] < 0 for p in posts)} bearish · {sum(p['stance'] == 0 for p in posts)} neutral)",
        "",
        "## Highest-reach posts",
        "| Account | Followers | Engagement | Stance | Post |",
        "|---|---:|---:|---|---|",
    ]
    for p in posts[:8]:
        stance = "bullish" if p["stance"] > 0 else "bearish" if p["stance"] < 0 else "neutral"
        lines.append(f"| [{p['author']}]({p['url']}) | {p['followers']:,} | {p['engagement']:,} | {stance} | {p['text'][:110].replace('|', '/')} |")
    lines += ["", "*Stance is a lexicon score weighted by followers and engagement — a reproducible read of what loud accounts are saying, not a forecast. Large accounts can be paid or positioned; treat calls as marketing until verified.*"]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Perplexity path
# ----------------------------------------------------------------------------

_INSTRUCTIONS = (
    "Return ONLY a JSON object (no prose, no code fences): "
    '{"sentiment":"bullish|bearish|mixed|neutral","score":-1.0..1.0,"summary":"one sentence",'
    '"posts":[{"author":"@handle","reach":"e.g. 250k followers or project account","stance":"bullish|bearish|neutral","summary":"what they said, one sentence","url":"https://x.com/..."}],'
    '"narratives":["..."],"risks":["..."]}. '
    "Cover posts on X (Twitter) from the last 24-48 hours by accounts with large followings (KOLs), analysts, funds and the project itself. "
    "Give 4-6 posts, real handles only, and never invent a URL: leave url empty if unsure. If you find nothing recent, set sentiment to \"neutral\" and posts to []."
)


def _from_perplexity(symbol: str) -> str | None:
    if not perplexity_available():
        return None
    try:
        text = perplexity_invoke("web_search", f"what are crypto twitter KOLs and analysts saying about ${symbol} on X in the last 48 hours", _INSTRUCTIONS)
    except Exception:
        logger.warning("social_sentiment: perplexity failed for %s", symbol, exc_info=True)
        return None
    data = _parse(text)
    if data is None:
        return None
    posts = data.get("posts") or []
    lines = [
        f"# X / KOL sentiment — {symbol}",
        f"**Provider**: Perplexity web search over X posts (last 24–48h) · **As of**: {_utc()}",
        "",
        f"**Read**: **{data.get('sentiment', 'neutral')}** (score {float(data.get('score') or 0):+.2f}) — {data.get('summary') or 'no summary'}",
    ]
    if posts:
        lines += ["", "## Who's saying what", "| Account | Reach | Stance | What they said |", "|---|---|---|---|"]
        for p in posts[:6]:
            author = p.get("author") or "?"
            cell = f"[{author}]({p['url']})" if p.get("url") else author
            lines.append(f"| {cell} | {p.get('reach') or '—'} | {p.get('stance') or '—'} | {(p.get('summary') or '').replace('|', '/')} |")
    else:
        lines += ["", "No recent high-reach posts were found for this symbol."]
    if data.get("narratives"):
        lines += ["", "**Narratives**: " + " · ".join(str(n) for n in data["narratives"][:5])]
    if data.get("risks"):
        lines += ["", "**Risks flagged**: " + " · ".join(str(r) for r in data["risks"][:5])]
    lines += ["", "*Social reads are reported, not measured: handles and reach come from search results, and loud accounts can be paid or positioned. Verify before acting.*"]
    return "\n".join(lines)


def _parse(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or "sentiment" not in data:
        return None
    clean_posts = []
    for p in data.get("posts") or []:
        if isinstance(p, dict) and p.get("author"):
            url = str(p.get("url") or "")
            clean_posts.append({**p, "url": url if url.startswith("https://x.com/") or url.startswith("https://twitter.com/") else ""})
    data["posts"] = clean_posts
    return data


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def reset() -> None:
    _cache.clear()
