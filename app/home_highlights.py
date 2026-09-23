"""Today's highlights for the welcome screen: the four suggestion tiles are
built from live crypto and stock-market news instead of fixed prompts.

Primary source is Perplexity's web search asked for strict JSON (two
crypto headlines, two stock-market headlines). Without a key, or when the
answer can't be parsed, the tiles fall back to keyless CoinGecko market data
(BTC move, top gainer, trending token) plus a stocks prompt that is answered
at click time. One composed result is cached per process for
HOME_HIGHLIGHTS_TTL_SECONDS with a single in-flight guard, so the welcome
screen never spends more than one paid call per window regardless of traffic.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

from app import market_overview
from app.perplexity_tools import _invoke as perplexity_invoke, perplexity_available
from app.settings import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cached: tuple[float, dict] | None = None

STATIC_CARDS = [
    {"id": "portfolio", "kind": "action", "tone": "teal", "title": "Analyze my portfolio and concentration risk", "summary": "Holdings, allocation and concentration risk from your connected wallet.", "action": "portfolio"},
    {"id": "balances", "kind": "action", "tone": "blue", "title": "Check my wallet balances", "summary": "SOL and SPL balances, read-only.", "prompt": "Check my SOL balance and list my SPL token balances. Do not prepare any trade."},
    {"id": "security", "kind": "action", "tone": "violet", "title": "Research a token and its safety", "summary": "Identity, verification and Jupiter Shield warnings.", "prompt": "Find the verified Jupiter token information and safety warnings for USDC mint EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v. Do not trade."},
    {"id": "swap", "kind": "action", "tone": "amber", "title": "Swap across chains with a reviewed quote", "summary": "Route, fees and output reviewed before you sign.", "action": "relay"},
]

_NEWS_INSTRUCTIONS = (
    "Return ONLY a JSON object (no prose, no code fences): "
    '{"crypto":[{"headline":"","summary":"","source":""},{"headline":"","summary":"","source":""}],'
    '"stocks":[{"headline":"","summary":"","source":""},{"headline":"","summary":"","source":""}],'
    '"memes":[{"headline":"","summary":"","source":"","date":""},{"headline":"","summary":"","source":"","date":""}]}. '
    "Fill it with the two most market-moving crypto stories, the two most market-moving US stock-market "
    "stories, and the two biggest memecoin stories (a launch, a rug, a listing, a whale, a viral token on "
    "Solana, Base or BNB Chain) from your search results (latest available: prices, ETFs, regulation, earnings, the Fed). "
    "Headline under 90 characters, summary one sentence with the key number, source a bare domain, and a date "
    "field (YYYY-MM-DD) for the day the event happened, not the day it was published. Copy every number exactly as "
    "the source prints it, never rounded, recomputed or recalled from memory; if the source gives no number, give none "
    "(a wrong index close or a mis-dated flow is worse than no figure, 2026-09-23). "
    "Never leave the arrays empty."
)
_NEWS_QUERY = "latest crypto market news, latest US stock market news, and latest memecoin news (pump.fun, Solana, Base, BNB Chain)"
# Perplexity answers with a placeholder rather than nothing when its search
# came back empty; those must not become tiles.
_PLACEHOLDER = re.compile(r"unavailable|no current|not available|could not retrieve|no verified", re.I)


def _parse_news(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for key in ("crypto", "stocks", "memes"):
        items = []
        for item in (data.get(key) or [])[:2]:
            if isinstance(item, dict) and item.get("headline") and not _PLACEHOLDER.search(str(item.get("headline")) + " " + str(item.get("summary") or "")):
                items.append({
                    "headline": str(item["headline"]).strip()[:120], **({"date": str(item["date"]).strip()[:10]} if item.get("date") else {}),
                    "summary": str(item.get("summary") or "").strip()[:200],
                    "source": str(item.get("source") or "").strip().lower()[:60],
                })
        out[key] = items
    return out if (out.get("crypto") or out.get("stocks") or out.get("memes")) else None


def _verified(parsed: dict) -> dict:
    """Every headline's figures checked against a dated, sourced read of the
    headline itself before it becomes a tile: a headline whose figure no
    source text carries is dropped (a Nasdaq close was off by one point and
    an ETF flow was dated a day late on the Home strip, 2026-09-23). The
    same claim check the contract pipeline runs on an answer, applied to the
    tile's own words; the read's first dated source fills a missing date."""
    from app import fact_gate
    from app.perplexity_tools import perplexity_search_with_sources
    out: dict = {}
    for kind, items in parsed.items():
        kept = []
        for item in items:
            try:
                found = perplexity_search_with_sources(item["headline"], recency_days=3)
            except Exception as exc:  # noqa: BLE001
                logger.warning("home highlights: verification read failed for %r (%s); tile kept unverified", item["headline"][:60], str(exc)[:80])
                kept.append(item)
                continue
            text = found.get("text") or ""
            # The headline's own figures must be printed at the same value in the
            # read (a close off by one point is a wrong close); the summary's
            # secondary figures pass the tolerant check the answers use.
            unsupported = fact_gate.missing_exact_figures(item["headline"], text)
            if unsupported:
                logger.warning("home highlights: dropped %r: figures %s not in its sources", item["headline"][:80], unsupported)
                continue
            loose = fact_gate.unsupported_figures(item.get("summary") or "", [], text)
            if loose:
                # The headline stands; its one-sentence summary carried figures
                # the read does not, so the read's own first sentence replaces it.
                plain = re.sub(r"\*\*|\[\d{1,2}\]", "", text.strip())
                sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", plain) if len(s.strip()) >= 40]
                first = next((s for s in sentences if not re.match(r"(?:That|This)\s+(?:statement|claim|headline)\b", s)), None)
                logger.info("home highlights: summary of %r replaced (figures %s not in its sources)", item["headline"][:60], loose)
                item = {**item, "summary": (first or "")[:200]}
            dated = [s for s in found.get("sources") or [] if s.get("date")]
            if not item.get("date") and dated:
                item = {**item, "date": dated[0]["date"][:10]}
            kept.append({**item, "verified": True})
        out[kind] = kept
    return out


def _news_cards() -> list[dict] | None:
    if not perplexity_available():
        return None
    try:
        # web_search, not finance_search: verified live, the finance tool answers
        # this ask with "no current results" placeholders while web search returns
        # dated headlines with sources.
        text = perplexity_invoke("web_search", _NEWS_QUERY, _NEWS_INSTRUCTIONS)
    except Exception:
        logger.warning("home highlights: news lookup failed", exc_info=True)
        return None
    parsed = _parse_news(text)
    if not parsed:
        logger.warning("home highlights: news answer was not the expected JSON")
        return None
    parsed = _verified(parsed)
    cards: list[dict] = []
    for kind, tone in (("crypto", "teal"), ("stocks", "blue"), ("memes", "violet")):
        for index, item in enumerate(parsed.get(kind) or []):
            cards.append({
                "id": f"{kind}-{index}", "kind": kind, "tone": tone if index == 0 else ("violet" if kind == "crypto" else "amber"),
                "title": item["headline"], "summary": item["summary"], "source": item["source"], "date": item.get("date"), "verified": bool(item.get("verified")),
                # The tile's own words; a headline tap is answered briefly by the
                # research node (composition.word_limit knows this prefix).
                "prompt": (f"What does this mean for memecoins: {item['headline']}" if kind == "memes"
                           else f"What does this mean for the market: {item['headline']}"),
            })
    return cards or None


def _market_cards() -> list[dict]:
    """Keyless fallback from CoinGecko: BTC move, top 24h gainer, trending token."""
    cards: list[dict] = []
    try:
        simple = market_overview._get_json(market_overview._SIMPLE_URL) or {}
        btc = simple.get("bitcoin") or {}
        if btc.get("usd"):
            change = float(btc.get("usd_24h_change") or 0)
            cards.append({"id": "btc", "kind": "crypto", "tone": "teal", "source": "coingecko.com",
                          "title": f"Bitcoin ${float(btc['usd']):,.0f} · {change:+.1f}% in 24h",
                          "summary": "Where the market leader is trading right now and what moved it.",
                          "prompt": "How's the crypto market today?"})
    except Exception:
        logger.debug("home highlights: simple price unavailable", exc_info=True)
    try:
        markets = market_overview._get_json(market_overview._MARKETS_URL) or []
        liquid = [m for m in markets if (m.get("total_volume") or 0) > 5_000_000 and m.get("price_change_percentage_24h") is not None]
        if liquid:
            top = max(liquid, key=lambda m: m["price_change_percentage_24h"])
            cards.append({"id": "gainer", "kind": "crypto", "tone": "violet", "source": "coingecko.com",
                          "title": f"{top.get('name')} ({str(top.get('symbol') or '').upper()}) +{float(top['price_change_percentage_24h']):.1f}% today",
                          "summary": "Top liquid gainer in the last 24 hours.",
                          "prompt": f"Deep dive on {str(top.get('symbol') or '').upper()}"})
    except Exception:
        logger.debug("home highlights: markets unavailable", exc_info=True)
    try:
        trending = market_overview._get_json(market_overview._TRENDING_URL) or {}
        coins = trending.get("coins") or []
        if coins:
            item = (coins[0].get("item") or {})
            symbol = str(item.get("symbol") or "").upper()
            if symbol:
                cards.append({"id": "trending", "kind": "crypto", "tone": "amber", "source": "coingecko.com",
                              "title": f"Trending now: {item.get('name')} ({symbol})",
                              "summary": "Most-searched token on CoinGecko right now.",
                              "prompt": f"Is {symbol} safe to ape into?"})
    except Exception:
        logger.debug("home highlights: trending unavailable", exc_info=True)
    cards.append({"id": "stocks", "kind": "stocks", "tone": "blue", "source": None,
                  "title": "How are US stocks doing today?",
                  "summary": "S&P 500, Nasdaq and the day's big earnings or Fed news.",
                  "prompt": "What is the top US stock market news today?"})
    return cards


def build() -> dict:
    cards = _news_cards()
    source = "news"
    if not cards:
        cards = _market_cards()
        source = "market"
    if len(cards) < 4:
        used = {c["id"] for c in cards}
        cards += [c for c in STATIC_CARDS if c["id"] not in used][: 4 - len(cards)]
    memes = [c for c in cards if c.get("kind") == "memes"]
    market = [c for c in cards if c.get("kind") != "memes"]
    return {"as_of": datetime.now(timezone.utc).isoformat(), "source": source, "cards": market[:4], "meme_cards": memes[:2]}


# ----------------------------------------------------------------------------
# Suggestion rows behind the category chips (fill the composer, don't send)
# ----------------------------------------------------------------------------

CURATED = {
    "crypto": [
        "What are the trending narratives right now in crypto?",
        "Which blue-chip crypto assets are still worth researching?",
        "Trending tokens on Solana",
        "Deep dive on JUP",
        "Is BONK safe to ape into?",
    ],
    "stocks": [
        "What's driving NVDA's recent move?",
        "Which earnings this week could move the market?",
        "How did US stocks close today and why?",
        "What are the most anticipated IPOs in the next 6 months?",
    ],
    # Memecoins (user focus 2026-09-18): every row is answerable by a live
    # tool -- Pulse, the deep dive, LP locks, holder positions, first buyers.
    "memes": [
        "What's bonding on pump.fun right now?",
        "New launches on Solana",
        "Deep dive on FARTCOIN",
        "Is BONK's liquidity locked?",
        "Top holders of WIF",
        "New launches on Robinhood chain",
    ],
    "macro": [
        "How will the next Fed decision affect risk assets?",
        "How do rising Treasury yields hit crypto and tech stocks?",
        "What's the read on this week's CPI print for markets?",
        "Is the dollar's move this week a risk-on or risk-off signal?",
    ],
}


def suggestions(wallet_holdings: list[str] | None = None) -> list[dict]:
    """Category rows for the home screen. Trending comes from today's
    highlights; My wallet from the caller's top holdings when known."""
    data = get_highlights()
    trending = []
    for card in data.get("cards", []):
        # The chip and the tile ask the same thing in the same words; no "?" after a headline.
        if card.get("prompt"):
            trending.append(card["prompt"])
        elif card.get("kind") in ("crypto", "stocks"):
            trending.append(f"What does this mean for the market: {card['title']}")
    for extra in ("How's the crypto market today?", "Why is BTC moving today?"):
        if extra not in trending:
            trending.append(extra)
    week_rows = ["What events could move the market this week?", "When is the next FOMC decision and what's expected?"]
    try:
        from app import event_calendar
        for event in (event_calendar.get_calendar(7).get("events") or [])[:3]:
            week_rows.insert(0, f"What does {event['title']} on {event['date']} mean for crypto and stocks?")
    except Exception:
        logger.debug("home highlights: calendar unavailable", exc_info=True)
    # Memes: today's two memecoin headlines first, then the live-tool prompts.
    meme_rows = [c["prompt"] for c in data.get("meme_cards", []) if c.get("prompt")]
    meme_rows += [row for row in CURATED["memes"] if row not in meme_rows]
    categories = [
        {"id": "trending", "label": "Trending", "rows": trending[:5]},
        {"id": "memes", "label": "Memes", "rows": meme_rows[:6]},
        {"id": "week", "label": "This week", "rows": week_rows[:5]},
        {"id": "crypto", "label": "Crypto", "rows": CURATED["crypto"]},
        {"id": "stocks", "label": "Stocks", "rows": CURATED["stocks"]},
        {"id": "macro", "label": "Macro", "rows": CURATED["macro"]},
    ]
    if wallet_holdings:
        top = [h for h in wallet_holdings if h and h.upper() != "UNKNOWN"][:2]
        rows = [f"Why is {sym} moving today?" for sym in top]
        rows += ["Analyze my portfolio and concentration risk", "What if my portfolio drops 20%?", "Wallet health check"]
        categories.append({"id": "wallet", "label": "My wallet", "rows": rows[:5]})
    return categories


def get_highlights(force: bool = False) -> dict:
    """Cached highlights; one build per TTL window, callers coalesce on the lock."""
    global _cached
    ttl = max(60, int(settings.home_highlights_ttl_seconds))
    now = time.monotonic()
    if not force and _cached and _cached[0] > now:
        return _cached[1]
    with _lock:
        if not force and _cached and _cached[0] > time.monotonic():
            return _cached[1]
        try:
            result = build()
        except Exception:
            logger.warning("home highlights: build failed; serving static cards", exc_info=True)
            result = {"as_of": datetime.now(timezone.utc).isoformat(), "source": "static", "cards": STATIC_CARDS}
        _cached = (time.monotonic() + ttl, result)
        return result


def reset() -> None:
    global _cached
    _cached = None
