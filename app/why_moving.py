"""The "why is X moving?" card: live market data + the news behind the move,
for a crypto token (Jupiter-verified or a major) or, failing that, a stock.

Deterministic composition, like the market-overview card: the market section
comes from the provider router (Birdeye / DexScreener / CoinGecko), the
narrative from Perplexity web search with sources, and the card states which
is which. Nothing here is advice; the closing line says so.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from app import market_overview, perplexity_tools
from app.jupiter import jupiter
from app.provider_registry import get_provider_router

logger = logging.getLogger(__name__)

# "why is SOL down today", "why did $BONK pump", "why USELESS token was pumping
# this week", "why has ETH been rallying": the verb may precede the symbol,
# follow it, or be missing; the direction word takes any tense. Anywhere in
# the message, not only at its start: "today market trend on crypto. why zec
# is pumping" carried the question at the end and, anchored, it was never
# seen -- the answer was a market brief that did not mention ZEC.
PATTERN = re.compile(
    r"(?<![A-Za-z])(?:so\s+)?why\s+(?:(?:is|did|has|have|are|was|were|does|do)\s+)?(?:the\s+)?\$?(?P<sym>[A-Za-z][A-Za-z0-9]{1,9})"
    r"(?:\s+(?:token|coin|stock|price|shares?))?(?:\s+(?:is|was|were|has\s+been|have\s+been|been|did|does|keeps?|kept))?\s+"
    r"(?P<dir>moving|up|down|pump(?:ing|ed|s)?|dump(?:ing|ed|s)?|rally(?:ing|ied|ies)?|falling|fell|crash(?:ing|ed)?|surg(?:ing|ed|es)?|"
    r"dropp?(?:ing|ed|s)?|rising|rose|tank(?:ing|ed)?|spik(?:ing|ed)?|jump(?:ing|ed)?|moon(?:ing|ed)?|soar(?:ing|ed)?|bleed(?:ing)?|"
    r"going\s+(?:up|down)|so\s+(?:high|low)|red|green)\b.*$",
    re.I,
)
_UP = ("up", "pump", "rally", "surg", "ris", "rose", "spik", "jump", "moon", "soar", "green", "going up", "so high")
_DOWN = ("down", "dump", "fall", "fell", "crash", "drop", "tank", "bleed", "red", "going down", "so low")
_MAJORS = {"BTC": ("bitcoin", "Bitcoin"), "ETH": ("ethereum", "Ethereum"), "SOL": ("solana", "Solana"), "BNB": ("binancecoin", "BNB"),
           "XRP": ("ripple", "XRP"), "DOGE": ("dogecoin", "Dogecoin"), "ADA": ("cardano", "Cardano"), "AVAX": ("avalanche-2", "Avalanche"),
           "LINK": ("chainlink", "Chainlink"), "SUI": ("sui", "Sui"), "TON": ("the-open-network", "Toncoin"), "TRX": ("tron", "TRON")}
_STOP = {"THE", "IT", "THIS", "THAT", "MARKET", "CRYPTO", "EVERYTHING"}


_STOCK_WORDS = re.compile(r"\b(?:stock|shares?|equity|equities|nasdaq|nyse|ticker)\b", re.I)
# A Jupiter match must look like a real market, not a namesake memecoin: verified
# AND either meaningful 24h volume or a decent organic score (verified live: a
# $256-volume "NVIDIA" token is in the verified list).
_MIN_VOLUME_USD = 25_000
_MIN_ORGANIC = 40


# "BTC fell sharply", "SOL is dumping hard", "ETH just pumped": the statement
# form of the same ask (live 2026-09-23: "no just now BTC fell sharply" went
# to the web with no tape). Only a real symbol, only a real move word.
STATEMENT = re.compile(
    r"(?<![A-Za-z$])\$?(?P<sym>[A-Z][A-Z0-9]{1,9})\s+(?:just\s+|is\s+|has\s+|was\s+)?(?P<dir>fell|dropped|dumped|dumping|pumped|pumping|crashed|crashing|tanked|surged|"
    r"rallied|spiked|jumped|mooned|soared|bleeding|down|up)\b(?:\s+(?:sharply|hard|fast|a\s+lot|big|\d+%))?", re.I)
MARKET = re.compile(r"\bwhy\s+(?:is|are|did|has|was|were)?\s*(?:the\s+)?(?:crypto\s+|whole\s+|entire\s+)?markets?\s+(?:is\s+|are\s+)?(?P<dir>falling|down|dumping|crashing|bleeding|red|up|rallying|pumping|green|rising|dropping|tanking)\b", re.I)


def _headline_tap(request: str) -> bool:
    """A Home headline tap quotes a headline; "BTC jumped past $87,000" inside
    it is the headline's words, not the user's statement about a move (a tap
    on an ETF-outflows headline answered "Why is BTC up?", UI review
    2026-09-24). The headline is answered as the question it is."""
    from app.composition import _HEADLINE_TAP
    return bool(_HEADLINE_TAP.match(request or ""))


def market_ask(request: str) -> str | None:
    """"Why is the market falling?": the crypto market, led by BTC, unless
    stocks are named (Orbit is a Web3 copilot; the stock market only on
    request). Returns the direction or None."""
    text = request or ""
    if _headline_tap(text):
        return None
    m = MARKET.search(text)
    if not m or prefers_stock(text):
        return None
    raw = m.group("dir").lower()
    return "up" if any(raw.startswith(w) for w in _UP) else "down"


def _is_ticker(raw: str) -> bool:
    """A statement's subject is a ticker when CoinGecko lists it: written as
    one ($BTC, BTC) any listing will do; written in lowercase ("btc fell
    sharply") one listed coin must clearly lead, so "tokens went up the most"
    is never a statement about a coin called WENT (live episode, 2026-09-23)."""
    from app import symbol_registry
    sym = raw.lstrip("$").upper()
    rows = symbol_registry.listed(sym)
    if raw.startswith("$") or raw.isupper():
        return bool(rows)
    return symbol_registry.leader(rows) is not None


def match(request: str) -> tuple[str, str] | None:
    if _headline_tap(request):
        return None
    m = PATTERN.search(request or "")
    if not m:
        m2 = STATEMENT.search(request or "")
        if m2 and m2.group("sym").upper() not in _STOP and m2.group("sym").upper() not in {"USD", "USDC", "USDT", "AI", "ETF", "NFT", "DEX", "CEX", "LP"} \
                and _is_ticker(m2.group(0).strip().split()[0]):
            raw = m2.group("dir").lower()
            direction = "up" if any(raw.startswith(w) for w in _UP) else "down" if any(raw.startswith(w) for w in _DOWN) else "moving"
            return m2.group("sym").upper(), direction
        return None
    sym = m.group("sym").upper()
    if sym in _STOP:
        return None
    raw = re.sub(r"\s+", " ", m.group("dir").lower())
    direction = "up" if any(raw.startswith(w) for w in _UP) else "down" if any(raw.startswith(w) for w in _DOWN) else "moving"
    return sym, direction


def prefers_stock(request: str) -> bool:
    return bool(_STOCK_WORDS.search(request or ""))


async def _crypto_identity(sym: str) -> dict | None:
    """{name, symbol, mint?, coingecko_id?, price, change_24h} or None."""
    if sym in _MAJORS:
        cid, name = _MAJORS[sym]
        try:
            data = await asyncio.to_thread(market_overview._get_json, f"{market_overview._CG}/simple/price?ids={cid}&vs_currencies=usd&include_24hr_change=true&include_24hr_vol=true")
            entry = (data or {}).get(cid) or {}
            return {"name": name, "symbol": sym, "coingecko_id": cid, "price": entry.get("usd"), "change_24h": entry.get("usd_24h_change"), "volume_24h": entry.get("usd_24h_vol")}
        except Exception:
            logger.debug("why_moving: coingecko failed for %s", sym, exc_info=True)
            return {"name": name, "symbol": sym, "coingecko_id": cid, "price": None, "change_24h": None}
    try:
        matches = await jupiter.search_tokens(sym)
    except Exception:
        return None
    for item in matches or []:
        if str(item.get("symbol") or "").upper() == sym and "verified" in (item.get("tags") or []):
            stats = item.get("stats24h") or {}
            volume = (stats.get("buyVolume") or 0) + (stats.get("sellVolume") or 0)
            if volume < _MIN_VOLUME_USD and (item.get("organicScore") or 0) < _MIN_ORGANIC:
                continue  # a namesake with no market is not "the token"
            return {"name": item.get("name") or sym, "symbol": sym, "mint": item.get("id"), "price": item.get("usdPrice"),
                    "change_24h": stats.get("priceChange"), "volume_24h": volume or None}
    # Not a major and not a Solana token: a coin on another chain (Zcash, say)
    # used to fall through to the stock path. CoinGecko's search is ranked, and
    # only a hit inside the top market-cap ranks counts: NVDA and TSLA also
    # have exact-symbol hits there, tokenized-stock wrappers ranked 796 and
    # below, and those must keep going to the stock path.
    return await _coingecko_identity(sym)


_MAX_COINGECKO_RANK = 250


async def _coingecko_identity(sym: str) -> dict | None:
    try:
        found = await asyncio.to_thread(market_overview._get_json, f"{market_overview._CG}/search?query={sym}")
        coin = next((c for c in (found or {}).get("coins", [])
                     if str(c.get("symbol") or "").upper() == sym and (c.get("market_cap_rank") or 10**6) <= _MAX_COINGECKO_RANK), None)
        if not coin:
            return None
        cid = coin["id"]
        data = await asyncio.to_thread(market_overview._get_json, f"{market_overview._CG}/simple/price?ids={cid}&vs_currencies=usd&include_24hr_change=true&include_24hr_vol=true")
        entry = (data or {}).get(cid) or {}
        return {"name": coin.get("name") or sym, "symbol": sym, "coingecko_id": cid, "price": entry.get("usd"),
                "change_24h": entry.get("usd_24h_change"), "volume_24h": entry.get("usd_24h_vol")}
    except Exception:
        logger.debug("why_moving: coingecko search failed for %s", sym, exc_info=True)
        return None


def _market_section(identity: dict) -> list[str]:
    lines = []
    price = identity.get("price")
    change = identity.get("change_24h")
    if price is not None:
        lines.append(f"- **Price**: ${float(price):,.6g}" + (f" · **24h**: {float(change):+.2f}%" if change is not None else ""))
    if identity.get("volume_24h"):
        lines.append(f"- **24h volume**: ${float(identity['volume_24h']):,.0f}")
    return lines


async def _crypto_market_detail(identity: dict) -> str | None:
    """Richer market data from the provider router for Solana tokens (Birdeye/DexScreener)."""
    mint = identity.get("mint")
    if not mint:
        return None
    try:
        result = await asyncio.to_thread(
            get_provider_router().try_route_across, f"price, volume and liquidity of {mint} on solana",
            ("market_data", "token_discovery"), ("solana",),
        )
    except Exception:
        logger.debug("why_moving: router detail failed", exc_info=True)
        return None
    return result.output if result else None


async def _news(query: str) -> str | None:
    """The reported reasons with their sources kept: the structured search
    (inline [n] markers, numbered dated sources) first, so a claim on the
    card can be traced; the plain search only when that fails. The plain
    adapter strips markers and adds a Sources block only when it has one,
    so a why-moving card could carry no source at all (live, 2026-09-23)."""
    if not perplexity_tools.perplexity_available():
        return None
    try:
        found = await asyncio.to_thread(perplexity_tools.perplexity_search_with_sources, query, recency_days=3)
        card = perplexity_tools.render_search_card(query, found)
        return card.split("\n", 1)[1] if card.startswith("# ") else card      # the card's own heading sits under this card's section
    except Exception:
        logger.debug("why_moving: structured news lookup failed; plain call", exc_info=True)
    try:
        return await asyncio.to_thread(perplexity_tools.perplexity_web_search, query)
    except Exception:
        logger.debug("why_moving: news lookup failed", exc_info=True)
        return None


def _trim(markdown: str, limit: int = 1800) -> str:
    """The narrative cut to `limit` on a line boundary; a trailing "Sources:"
    block always survives the cut, so a long news day never drops the
    citations (the why-SOL episode lost them 5 of 5 after 15:00 UTC, 2026-09-23)."""
    text = (markdown or "").strip()
    body, sources = text, ""
    marker = text.rfind("\nSources:")
    if marker >= 0:
        body, sources = text[:marker].rstrip(), text[marker:].strip()
    if len(body) > limit:
        body = body[:limit].rsplit("\n", 1)[0] + "\n…"
    return body + ("\n\n" + sources if sources else "")


async def compose(sym: str, direction: str, prefer_stock: bool = False) -> tuple[str, dict]:
    """(answer markdown, trajectory). Crypto first unless the ask says stock /
    shares (or routing already classed it as equity); a stock when the symbol
    isn't a real token."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    identity = None if prefer_stock else await _crypto_identity(sym)
    trajectory: dict = {}
    index = 0
    if identity:
        name = identity["name"]
        news_query = f"why is {name} ({sym}) {direction if direction not in ('moving', 'red', 'green') else 'moving'} today crypto news"
        detail_task = asyncio.create_task(_crypto_market_detail(identity))
        news_task = asyncio.create_task(_news(news_query))
        detail, news = await asyncio.gather(detail_task, news_task)
        lines = [f"# Why is {name} ({sym}) {direction}?", f"**As of** {now} · crypto", ""]
        market = _market_section(identity)
        if market or detail:
            lines.append("## Market")
            lines.extend(market)
            if detail:
                lines += ["", _trim(detail, 1400)]
            lines.append("")
            trajectory[f"tool_name_{index}"] = "market_data"
            trajectory[f"observation_{index}"] = "\n".join(market) + ("\n" + detail if detail else "")
            index += 1
        lines.append("## What's driving it")
        if news:
            lines.append(_trim(news))
            trajectory[f"tool_name_{index}"] = "perplexity_web_search"
            trajectory[f"observation_{index}"] = news
            index += 1
        else:
            lines.append("No news source is configured, so this card only shows the market data above.")
        lines += ["", "*Market data and reported news, not advice. Moves in crypto are often flow-driven; treat single-cause explanations with care.*"]
        return "\n".join(lines), trajectory

    # Stock (or unknown): one finance search with the numbers.
    if not perplexity_tools.perplexity_available():
        return (f"I couldn't identify **{sym}** as a verified crypto token, and no news source is configured to check it as a stock. "
                f"Try the token's mint or contract address, or ask *deep dive on {sym}*."), trajectory
    question = f"Why is {sym} stock {direction if direction not in ('moving', 'red', 'green') else 'moving'} today? Give the price, the day's move in percent, and the reported reasons with sources."
    answer, tool = None, "perplexity_finance_search"
    try:
        answer = await asyncio.to_thread(perplexity_tools.perplexity_finance_search, question)
    except Exception:
        logger.debug("why_moving: finance search failed for %s; trying web search", sym, exc_info=True)
    if not answer:
        tool = "perplexity_web_search"
        try:
            answer = await asyncio.to_thread(perplexity_tools.perplexity_web_search, question)
        except Exception as exc:
            return f"I couldn't check {sym} right now ({exc}).", trajectory
    trajectory["tool_name_0"] = tool
    trajectory["observation_0"] = answer
    return f"# Why is {sym} {direction}?\n**As of** {now} · stock (web sources)\n\n{_trim(answer, 2600)}\n\n*Reported news, not advice.*", trajectory
