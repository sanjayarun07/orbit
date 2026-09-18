"""Token unlock schedules, as a router tool.

"OPEN token unlock schedule" (2026-09-18) was answered with a DEX pair
search: the deep dive knew about DefiLlama's emissions data, but no tool
claimed an unlock question on its own. This one does. It reads the same
source, verifies the slug against the resolved contract when the request
carries one (so a namesake never lends its schedule), and lists what is
coming: date, amount, unlock type and who it goes to.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

# DefiLlama emissions: the slug list and one schedule per slug. These live
# here (not in token_deepdive) because the router registers this tool and
# token_deepdive imports the router.
_UNLOCK_LIST_URL = "https://defillama-datasets.llama.fi/emissionsProtocolsList"
_UNLOCK_SLUG_URL = "https://defillama-datasets.llama.fi/emissions/{slug}"
_UNLOCK_INDEX_URL = "https://defillama-datasets.llama.fi/emissionsIndex"
_unlock_list_cache: tuple[float, list[str]] | None = None
_unlock_index_cache: tuple[float, list[dict]] | None = None
_UNLOCK_TTL = 3600.0


def _http_json(url: str, timeout: float = 20.0):
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url, headers={"User-Agent": "Orbit-Web3-Copilot/0.2"})
        resp.raise_for_status()
        return resp.json()


def _unlock_slug_list() -> list[str]:
    global _unlock_list_cache
    if _unlock_list_cache and time.monotonic() - _unlock_list_cache[0] < _UNLOCK_TTL:
        return _unlock_list_cache[1]
    try:
        data = _http_json(_UNLOCK_LIST_URL)
        slugs = [str(s) for s in data] if isinstance(data, list) else []
    except Exception:
        slugs = []
    _unlock_list_cache = (time.monotonic(), slugs)
    return slugs

def _unlock_index() -> list[dict]:
    """DefiLlama's emissions index: one row per tracked token with its slug,
    name, ticker and token reference. The slug list alone cannot answer
    "which slug is ARB" (arbitrum-exchange is a namesake); the ticker can."""
    global _unlock_index_cache
    if _unlock_index_cache and time.monotonic() - _unlock_index_cache[0] < _UNLOCK_TTL:
        return _unlock_index_cache[1]
    rows: list[dict] = []
    try:
        data = _http_json(_UNLOCK_INDEX_URL, timeout=30.0)
        for row in (data.get("data") if isinstance(data, dict) else data) or []:
            if not isinstance(row, dict) or not row.get("protocolSlug"):
                continue
            prices = row.get("tokenPrice") or []
            symbol = str((prices[0] or {}).get("symbol") or "") if prices and isinstance(prices[0], dict) else ""
            rows.append({"slug": str(row["protocolSlug"]), "name": str(row.get("name") or ""), "symbol": symbol.upper(), "token": str(row.get("token") or "").lower()})
    except Exception:
        logger.debug("unlocks: index unavailable", exc_info=True)
    _unlock_index_cache = (time.monotonic(), rows)
    return rows


UNLOCK_ASK = re.compile(
    r"\b(?:unlocks?|unlocking|vesting|vest(?:s|ed)?|emissions?\s+schedule|emissions?|cliff|token\s+release|release\s+schedule|supply\s+schedule|unlock\s+calendar)\b",
    re.IGNORECASE,
)
_ADDRESS = re.compile(r"(?<![0-9a-fA-F])(0x[0-9a-fA-F]{40})(?![0-9a-fA-F])|(?<![A-Za-z0-9])([1-9A-HJ-NP-Za-km-z]{32,44})(?![A-Za-z0-9])")
_CHAIN = re.compile(r"\bon\s+(solana|ethereum|base|arbitrum|bsc|bnb|polygon|avalanche|optimism|sui)\b", re.IGNORECASE)
_TICKER = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b|\b([A-Z][A-Z0-9]{1,9})\b")
_STOP = {"TOKEN", "COIN", "UNLOCK", "UNLOCKS", "SCHEDULE", "VESTING", "THE", "A", "AN", "OF", "FOR", "ON", "IS", "ARE", "WHEN", "WHAT", "NEXT", "USD", "USDT", "USDC", "TGE"}
_cache: dict[str, tuple[float, str]] = {}
_TTL = 3600


def ticker_in(request: str) -> str | None:
    for dollar, upper in _TICKER.findall(request or ""):
        tick = (dollar or upper).upper()
        if tick not in _STOP and len(tick) >= 2:
            return tick
    return None


def matches(request: str) -> bool:
    text = request or ""
    return bool(UNLOCK_ASK.search(text)) and (bool(_ADDRESS.search(text)) or ticker_in(text) is not None)


def _events(slug: str) -> tuple[dict, list[dict]]:
    data = _http_json(_UNLOCK_SLUG_URL.format(slug=slug))
    meta = (data.get("metadata") or {}) if isinstance(data, dict) else {}
    events = [e for e in (meta.get("events") or []) if isinstance(e, dict) and e.get("timestamp")]
    return meta, sorted(events, key=lambda e: e["timestamp"])


def _amount(event: dict) -> str:
    toks = (event.get("noOfTokens") or [None])[0]
    return f"{float(toks):,.0f} tokens" if isinstance(toks, (int, float)) else "amount not stated"


def _card(symbol: str, slug: str, meta: dict, events: list[dict], verified_address: str | None) -> str:
    now = time.time()
    upcoming = [e for e in events if e["timestamp"] > now]
    past = [e for e in events if e["timestamp"] <= now]
    token_ref = str(meta.get("token") or "")
    lines = [
        f"# Unlock schedule — {symbol}",
        f"**Provider**: DefiLlama emissions (`{slug}`) · **As of**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        + (f" · **Contract**: `{verified_address}` (verified)" if verified_address else (f" · **Token**: `{token_ref}`" if token_ref else "")),
        "",
    ]
    if upcoming:
        lines += ["## Upcoming unlocks", "| Date | Amount | Type | To |", "|---|---:|---|---|"]
        for e in upcoming[:12]:
            when = datetime.fromtimestamp(e["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d")
            lines.append(f"| {when} | {_amount(e)} | {e.get('unlockType') or '—'} | {e.get('category') or '—'} |")
        if len(upcoming) > 12:
            lines.append(f"| … | {len(upcoming) - 12} more events | | |")
    else:
        lines += ["No further unlock events are scheduled in DefiLlama's data for this token."]
    if past:
        lines += ["", f"## Recent unlocks ({min(3, len(past))} of {len(past)} past events)", "| Date | Amount | Type | To |", "|---|---:|---|---|"]
        for e in past[-3:]:
            when = datetime.fromtimestamp(e["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d")
            lines.append(f"| {when} | {_amount(e)} | {e.get('unlockType') or '—'} | {e.get('category') or '—'} |")
    lines += ["", "Source: DefiLlama emissions · https://defillama.com/unlocks/" + slug,
              "Schedules come from projects' published vesting terms; amounts are as documented, not observed on-chain."]
    return "\n".join(lines)


def _web_lookup(symbol: str, request: str) -> str | None:
    """The vesting terms from the web when DefiLlama does not list the token:
    what Perplexity answers for "OPEN token unlock schedule" (the project's
    tokenomics docs and unlock calendars), source-linked. None when web
    search is not configured."""
    from app.perplexity_tools import perplexity_available, perplexity_web_search

    if not perplexity_available():
        return None
    # Plain web search: the finance-tuned one insists on a live quote in its
    # answer, which a vesting table has no reason to contain (live, 2026-09-18:
    # it rejected its own OpenLedger answer as "ungrounded").
    return perplexity_web_search(
        f"{symbol} token unlock and vesting schedule. Give the allocation table (category, amount, vesting terms), "
        f"total supply, what was liquid at TGE, the next unlock dates and amounts, and cite the project's official "
        f"tokenomics documentation and an unlock calendar. If several tokens use the ticker {symbol}, say which one "
        f"you mean and why. Report figures exactly as each source states them; where sources disagree, show both figures "
        f"with their sources and do not reconcile, average or derive a monthly amount from them. Original question: {request}"
    )


def _candidate_slugs(symbol: str, address: str | None) -> list[str]:
    """Slugs worth fetching for this ask. By ticker from the index (exact
    symbol, so ARB is arbitrum and never arbitrum-exchange), by token
    reference when the request carries a contract, and the ticker itself as
    a slug as a last resort. Every candidate is still verified by the caller."""
    sym = symbol.lower().lstrip("$")
    index = _unlock_index()
    slugs: list[str] = []
    if address:
        slugs += [row["slug"] for row in index if row["token"].endswith(address.lower())]
    slugs += [row["slug"] for row in index if row["symbol"] == sym.upper()]
    slugs += [row["slug"] for row in index if row["name"].lower() == sym]
    if not index:
        listed = _unlock_slug_list()
        slugs += [s for s in (sym, sym.replace(" ", "-")) if s in listed]
    return list(dict.fromkeys(slugs))[:4]


def unlock_schedule(request: str) -> str:
    """The unlock schedule for the token the request names: DefiLlama's dated
    events when it tracks the token (verified against the contract when the
    request carries one), otherwise the vesting terms from the web."""
    address_match = _ADDRESS.search(request or "")
    address = (address_match.group(1) or address_match.group(2)) if address_match else None
    symbol = ticker_in(request) or (address[:6] + "…" if address else None)
    if not symbol:
        raise ValueError("Name the token, e.g. 'OPEN token unlock schedule'")
    key = (address or symbol).lower()
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < _TTL:
        return cached[1]
    matched: list[tuple[str, dict, list[dict]]] = []
    for slug in _candidate_slugs(symbol, address):
        try:
            meta, events = _events(slug)
        except Exception:
            logger.debug("unlocks: slug %s failed", slug, exc_info=True)
            continue
        token_ref = str(meta.get("token") or "").lower()
        if address and not token_ref.endswith(address.lower()):
            continue                      # a namesake: never attribute its schedule
        matched.append((slug, meta, events))
    if matched and len(matched) > 1 and not address:
        card = "\n\n".join(_card(symbol, slug, meta, events, None) for slug, meta, events in matched[:2]) + \
            "\n\n*Several DefiLlama entries match this ticker; each is shown with the token it refers to. Paste the contract address to pin one.*"
    elif matched:
        slug, meta, events = matched[0]
        card = _card(symbol, slug, meta, events, address)
    else:
        untracked = (f"DefiLlama does not track an unlock schedule for {symbol}" + (f" at `{address}`" if address else "") + ".")
        web = None
        try:
            web = _web_lookup(symbol, request)
        except Exception:
            logger.info("unlocks: web look-up failed for %s", symbol, exc_info=True)
        if web:
            card = (f"# Unlock schedule — {symbol}\n**Provider**: web search (Perplexity) · **As of**: "
                    f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n{web.strip()}\n\n"
                    f"*{untracked} The terms above come from the project's published documentation and unlock calendars "
                    f"found on the web; exact dates, amounts and USD values can differ between sources.*")
        else:
            card = (f"# Unlock schedule — {symbol}\n**Provider**: DefiLlama emissions\n\n{untracked} "
                    "Most memecoins and fully circulating tokens have none; a token with a vesting schedule that is not listed there has to be checked in its own docs.")
    _cache[key] = (time.monotonic(), card)
    return card


def warm() -> None:
    """Fetch the emissions index ahead of the first unlock question: live it
    took ~31s cold, which is the whole latency budget of a turn."""
    try:
        _unlock_index()
    except Exception:
        logger.debug("unlocks: warm-up failed", exc_info=True)


def reset() -> None:
    global _unlock_index_cache
    _cache.clear()
    _unlock_index_cache = None
