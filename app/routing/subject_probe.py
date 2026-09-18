"""When the router is not sure what a message is about, look it up first.

"Audit report on ANSEM" is unanswerable until you know that ANSEM is a
Solana memecoin; the classifier abstained, the rules had nothing to anchor
on, and the turn went to a web search that came back with French
public-sector audit reports. The user's rule (2026-09-18): if we are not
sure what the query is, start with a web search to get the context, then
route with it.

`probe(request)` asks the finance-tuned web search one strict-JSON question
-- what the proper noun in this message most likely refers to -- and the
resolver turns the answer into a route: a token becomes token research on
its chain, a stock becomes equity research, a protocol a knowledge or web
question, a person or company web research. Cached for an hour per subject;
never used when the model is confident or a rule anchors the turn.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

from app.perplexity_tools import _invoke as perplexity_invoke, perplexity_available

logger = logging.getLogger(__name__)

_TTL_SECONDS = 3600
_cache: dict[str, tuple[float, dict | None]] = {}
_lock = threading.Lock()

# Something that could be a name: $TICKER, an all-caps word, or a capitalised
# word. Sentence starters, market jargon and chain names are not subjects.
_SUBJECT = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,10})\b|\b([A-Z][A-Z0-9]{1,10})\b|\b([A-Z][a-z][A-Za-z0-9]{1,20})\b")
_STOP = {w.upper() for w in (
    "I A OK US UK EU AI ETF ETFS IPO CEO CFO SEC FED GDP CPI DEX CEX NFT APY APR TVL ATH USD USDT USDC BUY SELL LONG SHORT HOLD RSI MACD "
    "Solana Ethereum Base Bitcoin Arbitrum Polygon Avalanche Twitter Google Please Audit Report Token Tokens Coin Coins Crypto Stock Stocks Price "
    "What Whats How Is Are Can Could Should Would Will Do Does Did Tell Show Give Which Why When Where Who The An My Our Your It Its This That These "
    "Any Latest Best Top Compare Explain Check Find List Get Swap Trade Market Markets News Today Now Research Analyze Analyse Deep Dive Hi Hello Hey Thanks Ok Okay Yes No"
).split()}
_INSTRUCTIONS = (
    "You identify what a name refers to in the context of markets, crypto and finance. Return ONLY strict JSON: "
    '{"kind": "token|protocol|equity|person|company|concept|other", "name": "...", "symbol": "... or null", "chain": "solana|ethereum|base|arbitrum|bsc|polygon|avalanche|other or null", '
    '"exchange": "... or null", "summary": "one sentence", "confidence": 0.0-1.0}. '
    "A crypto token is kind=token with its chain; a listed stock is kind=equity with its exchange; a DeFi protocol is kind=protocol. If several things share the name, pick the one a crypto/markets user most likely means and lower the confidence."
)


def subject_of(request: str) -> str | None:
    """The name worth looking up in this message, or None."""
    for dollar, upper, capital in _SUBJECT.findall(request or ""):
        name = dollar or upper or capital
        if name and name.upper() not in _STOP:
            return name
    return None


def probe(request: str) -> dict | None:
    """What the message's subject most likely is, from a web search, or None
    when there is nothing to look up or nothing came back."""
    subject = subject_of(request)
    if not subject or not perplexity_available():
        return None
    key = subject.lower()
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    result = None
    try:
        text = perplexity_invoke("finance_search", f'In crypto and markets, what does "{subject}" refer to? Context: "{request}"', _INSTRUCTIONS)
        match = re.search(r"\{.*\}", text or "", re.S)
        data = json.loads(match.group(0)) if match else None
        if isinstance(data, dict) and data.get("kind"):
            data["subject"] = subject
            result = data
    except Exception:
        logger.info("subject probe failed for %r", subject, exc_info=True)
    with _lock:
        _cache[key] = (now + _TTL_SECONDS, result)
    return result


def route_from(found: dict, request: str) -> dict | None:
    """A route decision from a probe result, or None when it does not settle
    the question (low confidence, or a kind the router has no path for)."""
    if not found or float(found.get("confidence") or 0) < 0.6:
        return None
    kind = str(found.get("kind") or "").lower()
    chain = str(found.get("chain") or "").lower()
    chains = [chain] if chain and chain != "other" else []
    symbol = found.get("symbol") or found.get("subject")
    if kind == "token":
        # Name the subject as a token on its chain so the research node's
        # resolver and the security/market tools take it from here.
        contextual = f"{request} ({symbol} token on {chain})" if chain and chain != "other" else f"{request} ({symbol} token)"
        return {"intent": "research", "capabilities": ["token_security", "token_discovery", "market_data"], "chains": chains,
                "route_source": "subject_probe", "contextual_request": contextual}
    if kind == "equity":
        return {"intent": "research", "capabilities": ["equity_research"], "chains": [], "route_source": "subject_probe",
                "contextual_request": f"{request} ({symbol} stock" + (f" on {found['exchange']}" if found.get("exchange") else "") + ")"}
    if kind == "protocol":
        return {"intent": "research", "capabilities": ["knowledge", "defi_data", "web_research"], "chains": chains, "route_source": "subject_probe",
                "contextual_request": f"{request} ({found.get('name') or symbol} protocol)"}
    if kind in {"person", "company", "concept"}:
        return {"intent": "research", "capabilities": ["web_research"], "chains": [], "route_source": "subject_probe"}
    return None


def reset() -> None:
    with _lock:
        _cache.clear()
