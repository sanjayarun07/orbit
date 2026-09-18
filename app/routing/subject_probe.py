"""When the router is not sure what a message is about, look it up first.

"Audit report on ANSEM" is unanswerable until you know that ANSEM is a
Solana memecoin; the classifier abstained, the rules had nothing to anchor
on, and the turn went to a web search that came back with French
public-sector audit reports. The user's rule (2026-09-18): if we are not
sure what the query is, start with a web search to get the context, then
route with it.

`probe(request)` asks the web search one strict-JSON question
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

from app.perplexity_tools import _invoke as perplexity_invoke, perplexity_available, perplexity_finance_search, perplexity_web_search

logger = logging.getLogger(__name__)

_TTL_SECONDS = 3600
_CONTEXT_TTL_SECONDS = 900
_cache: dict[str, tuple[float, dict | None]] = {}
_context_cache: dict[str, tuple[float, str | None]] = {}
_lock = threading.Lock()
# A message asking for a price or a financial figure goes to the finance-tuned
# search for its context (it insists on a live quote in its answer, which is
# right there and wrong everywhere else); anything else to the plain one.
_FINANCE_HINT = re.compile(r"\b(?:price|prices|quote|trading at|worth|market cap|mcap|valuation|earnings|revenue|eps|dividend|p/e|pe ratio|share price|stock price)\b", re.I)

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
        # Plain web search, not the finance-tuned one: measured on the same
        # seven subjects (2026-09-18) at the same price, web search named the
        # memecoins finance search called "other" (ANSEM) or left without a
        # symbol (PUMP) and typed Reliance as the equity it is.
        text = perplexity_invoke("web_search", f'In crypto and markets, what does "{subject}" refer to? Context: "{request}"', _INSTRUCTIONS)
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


def context_search(request: str) -> str | None:
    """The web's answer to the message itself, for a turn the router could
    not place: finance search for a market-shaped question, web search
    otherwise. User rule (2026-09-18): when we are not confident, search
    first, synthesize, and decide the tools from what came back. Cached per
    message for fifteen minutes; None when search is off or fails."""
    text = (request or "").strip()
    if not text or not perplexity_available():
        return None
    key = re.sub(r"\s+", " ", text.lower())
    now = time.monotonic()
    with _lock:
        hit = _context_cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    result = None
    try:
        search = perplexity_finance_search if _FINANCE_HINT.search(text) else perplexity_web_search
        result = search(text) or None
    except Exception:
        logger.info("context search failed for %r", text[:80], exc_info=True)
    with _lock:
        _context_cache[key] = (now + _CONTEXT_TTL_SECONDS, result)
    return result


def context_card(request: str, context: str) -> str:
    """The context search as an evidence card the research node can read with
    the tools' cards."""
    subject = subject_of(request)
    title = f"# Web context — {subject}" if subject else "# Web context"
    return f"{title}\n**Provider**: Perplexity search (the question as asked)\n\n{context.strip()}"


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
    if kind in {"person", "company"}:
        return {"intent": "research", "capabilities": ["web_research"], "chains": [], "route_source": "subject_probe"}
    # "concept" (Mercury retrograde, an acronym) and "other" do not settle a
    # market question; the resolver asks with the probe's reading as a hint.
    return None


def agrees(found: dict | None, context: str | None) -> bool:
    """Whether the web's answer to the question is about the same thing the
    probe identified. A token, equity or protocol needs market text; the
    TRUMP case (probe: the Solana memecoin; web: the politician) is the
    disagreement this catches. A person or company accepts any context."""
    from app.clarify import is_market_text

    if not found or not context:
        return False
    kind = str(found.get("kind") or "").lower()
    if kind in {"token", "equity", "protocol"}:
        return is_market_text(context)
    return True


def clarify_text(found: dict | None, request: str) -> str:
    """The question to ask when neither the probe nor the web settled the
    subject, naming what the web read it as so the user can redirect."""
    subject = (found or {}).get("subject") or subject_of(request)
    reading = str((found or {}).get("summary") or "").strip()
    reading = reading.split(" Sources:")[0].split(" sources:")[0].strip()
    lead = f"I'm not sure what **{subject}** refers to here." if subject else "I'm not sure what this refers to."
    hint = f" The web reads it as: {reading}" if reading else ""
    return (f"{lead}{hint}\n\nOrbit covers crypto and markets: if you mean a token, protocol or company, name it "
            "(a $ticker, the full project name, or a contract address) and say what you want to know -- price, security, "
            "unlocks, holders, news -- and I'll pull the data.")


def reset() -> None:
    with _lock:
        _cache.clear()
        _context_cache.clear()
