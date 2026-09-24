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
    "Any Latest Best Top Compare Explain Check Find List Get Swap Trade Market Markets News Today Now Research Analyze Analyse Deep Dive Hi Hello Hey Thanks Ok Okay Yes No "
    # market jargon that is written in capitals (UI run 2026-09-23: CLMM and DLMM became tokens)
    "CLMM DLMM AMM PDA PDAS LP LPS DAO KYC ICO IDO FDV MCAP OTC PNL ROI OI RWA L1 L2 EVM SPL ERC EUR KOL KOLS MEV TWAP VWAP LTV FAQ API MCP OTC PNL "
    "January February March April May June July August September October November December Jan Feb Mar Apr Jun Jul Aug Sep Sept Oct Nov Dec "
    "Monday Tuesday Wednesday Thursday Friday Saturday Sunday Q1 Q2 Q3 Q4 UTC EST PST"
).split()}

# A capitalised word that opens a sentence and is followed by a determiner,
# a pronoun or "token" is the sentence's verb, not a name: "Save this
# investigation", "Separate token price movement", "Show my watchlist" all
# reached the web as SAVE / SEPARATE / MY (UI run, 2026-09-23). A ticker
# opening a sentence ("Bonk price?", "ANSEM holders") is still a subject.
_SENTENCE_START = re.compile(r"(?:^|[.!?:;]\s+)([A-Z][a-z][A-Za-z0-9]{1,20})\s+([a-z]+)\b")
_INSTRUCTION_NEXT = {"this", "that", "these", "those", "the", "a", "an", "my", "me", "our", "your", "all", "each", "every", "it", "them",
                     "token", "tokens", "coin", "coins", "investigation", "report", "everything", "anything",
                     # "Switch topics:", "Then show 24h", "And which are paused?": a capitalised opener before a topic word,
                     # a verb or a question word is not a name (expanded UI review, 2026-09-24: Switch, Then and And became assets)
                     "topics", "topic", "subject", "which", "what", "how", "where", "when", "who", "why", "show", "list", "give", "tell",
                     "compare", "check", "find", "run", "please", "now", "again", "back", "only", "just", "also", "instead"}


# English imperatives and pronouns that open a sentence: "Build bull, base and
# bear cases", "Mark database-only claims", "Handle pools", "You showed" all
# reached the web as BUILD / MARK / Handle.fi / YOU (UI run, 2026-09-23).
_OPENERS = {"switch", "then", "and", "also", "now", "next", "ok", "okay", "back", "so", "but", "first", "second", "finally", "anyway",
            "meanwhile", "instead", "actually", "alternatively", "well", "right", "fine", "sure", "yes", "no", "wait", "hmm", "plus"}
_IMPERATIVES = {w.lower() for w in (
    "build mark handle treat separate save export show list give tell explain compare confirm include exclude inspect prepare revisit "
    "distinguish require use name say link rank sort filter summarize summarise describe outline draft write create make add remove "
    "keep drop ignore skip focus start stop continue run check verify audit review assess evaluate estimate calculate compute count "
    "map trace follow track watch monitor alert notify remind send email open close set update change fix note flag label classify "
    "identify detect determine decide recommend suggest propose argue defend attack challenge test try do does did go come look see read "
    "consider assume suppose imagine pretend act behave answer reply respond ask question clarify elaborate expand shorten simplify "
    "translate convert format render print display present report document record log store fetch pull get take put place hold "
    "you we they he she i it this that these those there here "
    "were was am be been being has have had may might must shall let please"
).split()}


def _is_sentence_starter(word: str, next_word: str) -> bool:
    return word.lower() in _IMPERATIVES or word.lower() in _OPENERS or next_word in _INSTRUCTION_NEXT
_INSTRUCTIONS = (
    "You identify what a name refers to in the context of markets, crypto and finance. Return ONLY strict JSON: "
    '{"kind": "token|protocol|equity|person|company|concept|other", "name": "...", "symbol": "... or null", "chain": "solana|ethereum|base|arbitrum|bsc|polygon|avalanche|other or null", '
    '"exchange": "... or null", "summary": "one sentence", "confidence": 0.0-1.0}. '
    "A crypto token is kind=token with its chain; a listed stock is kind=equity with its exchange; a DeFi protocol is kind=protocol. If several things share the name, pick the one a crypto/markets user most likely means and lower the confidence."
)


def sentence_starters(request: str) -> set[str]:
    return {m.group(1) for m in _SENTENCE_START.finditer(request or "") if _is_sentence_starter(m.group(1), m.group(2))}


# A follow-up continues the subject when it talks about the analysis (buys,
# holders, cases, claims, funding rounds…) and does not open a new market-
# wide topic (trending, gainers, the market today).
_CONTINUES = re.compile(
    r"\b(?:buys?|bought|sells?|sold|price|prices|volume|liquidity|holders?|holdings?|supply|deployer|launch\w*|bundl\w+|snip\w+|evidence|snapshots?|claims?|"
    r"conclusions?|cases?|bull|bear|thesis|risks?|memo|diligence|funding|rounds?|revenue|fees|tvl|tokens?|protocol|chain|wallets?|exit|quotes?|"
    r"concentrat\w+|customers|operators|dependencies|incidents?|security|audit|metrics?|changes?|compare|comparison|valuation|exposure|ownership|"
    r"investors?|competitors?|adoption|unlocks?|sentiment|mint|contract|extensions?|program|pools?|lp|depth|slippage|sources?|timestamps?|data|"
    r"report|table|its|checks?|verify|verified|proven|inferred|disputed|unsupported|assumptions?|questions?|"
    # a time or window correction continues the previous ask ("I meant the past hour", "then show 24h separately", 2026-09-24)
    r"hours?|hourly|minutes?|24h|1h|daily|weekly|interval|window|period|separately|labell?ed|feed|support|meant|"
    # "I only want a read-only estimate", "restate the quote limits", "do not submit anything": the exit ask continues (2026-09-24)
    r"estimate|estimates|read[- ]only|hypothetical|restate|submit|limits|research\s+mode|sell\s+it\s+now|denominator|percent|same)\b", re.I)
_NEW_TOPIC = re.compile(
    r"\b(?:trending|gainers|losers|movers|narratives?|metas?|market\s+(?:today|now|overview|update|brief)|crypto\s+market|new\s+(?:launches|pairs|tokens|listings)|"
    r"what'?s\s+(?:hot|trending|launching|new)|how\s+is\s+the\s+market|top\s+\d+\s+(?:coins|tokens)|fear\s+and\s+greed)\b", re.I)


# "Can I see a wallet without giving you control?", "is any token safe?": an
# indefinite noun is a generic subject, not the conversation's asset (the
# frozen trust run, 2026-09-23, carried a PEPE contract into a wallet read).
_INDEFINITE = re.compile(r"\b(?:a|an|any|some|every)\s+(?:wallet|address|token|coin|contract|portfolio|protocol|chain|position|account|exchange)\b", re.I)


# "Does that automatically authorize Robinhood Chain today?", "Which part was
# in the SEC order and which is your inference?": a referent pronoun at the
# head of the question points at the conversation's subject even when another
# name appears later in it (expanded UI review, 2026-09-24: the SEC order was
# lost to "Robinhood" and to "I'm not sure what this refers to").
_REFERENT = re.compile(r"^\s*(?:and\s+|so\s+|but\s+|ok,?\s+)?(?:(?:(?:what|which|how|when|where|why)\s+)?(?:does|do|is|was|did|will|would|can|could|has|have|which\s+part\s+of|what\s+part\s+of|how\s+much\s+of)\s+(?:that|this|it|those|these)\b"
                       r"|(?:which|what)\s+(?:parts?|portion|bits?|of\s+those|of\s+these|of\s+them|ones?)\b"
                       r"|(?:what|which|how|who)\b[^?]{0,40}?\b(?:since|after|before|from|about)\s+(?:then|that|this|it)\b"
                       r"|(?:what|anything)(?:'s|\s+is|\s+has)?\s+(?:changed|new|different|happened|moved)\s*[?.!]?\s*$)", re.I)     # a bare "What changed?" points at the previous subject; with none it is a question (2026-09-24: it explained DeFi yields)     # "What is different since then?" points at the previous answer     # "Which part was in the order and which is your inference?" partitions the previous answer


# "What does this mean for the market: Bitcoin and Ethereum ETFs see $592
# million outflows": the referent is explained after the colon, so the
# sentence carries its own subject even when no ticker in it is recognised
# (a Home tile tap asked "which token do you mean?", 2026-09-24).
_INLINE_REFERENT = re.compile(r"[:\u2014-]\s*\S+(?:\s+\S+){2,}")


def is_referent(text: str) -> bool:
    """A pronoun question that points at the previous answer, with nothing
    after a colon or dash that explains the pronoun itself."""
    return bool(_REFERENT.match(text or "")) and not _INLINE_REFERENT.search(text or "")


def continues_subject(request: str) -> bool:
    """Whether a message that names nothing of its own reads as a follow-up
    on the conversation's subject rather than a new topic or small talk."""
    text = request or ""
    if is_referent(text) and not _NEW_TOPIC.search(text):
        return True
    if len(text.split()) < 3 or has_own_subject(text) or _NEW_TOPIC.search(text) or _INDEFINITE.search(text):
        return False
    return bool(_CONTINUES.search(text))


def opens_new_topic(request: str) -> bool:
    return bool(_NEW_TOPIC.search(request or ""))


# A lowercase name in a data-ask position is still a name: "price of pepe",
# "is bonk safe", "wif holders" (recheck of 2910d5e8: "what is the price of
# pepe?" inherited the previous token).
_LOWER_AFTER = re.compile(r"\b(?:price|prices|holders?|liquidity|volume|market\s*cap|mcap|fdv|chart|safety|security|supply|news|unlocks?|"
                          r"deep\s+dive|research|analy[sz]e|about|into|buy|sell|check)\s+(?:of|for|on|into)?\s*(?:the\s+)?\$?([a-z][a-z0-9]{2,9})\b", re.I)
_LOWER_BEFORE = re.compile(r"\b\$?([a-z][a-z0-9]{2,9})\s+(?:price|holders|token|coin|is\s+(?:safe|legit|a\s+rug))\b|\bis\s+\$?([a-z][a-z0-9]{2,9})\s+(?:safe|legit|a\s+rug|a\s+scam|bundled)\b", re.I)
_LOWER_STOP = {"the", "this", "that", "these", "those", "my", "our", "your", "its", "it", "them", "solana", "ethereum", "base", "bsc", "bnb", "arbitrum",
               "polygon", "avalanche", "crypto", "token", "tokens", "coin", "coins", "market", "markets", "chain", "wallet", "portfolio", "position",
               "each", "every", "any", "all", "some", "new", "top", "best", "next", "last", "first", "current", "latest", "today", "week", "month",
               "meme", "memes", "stable", "stables", "perps", "perp", "stock", "stocks", "usdc", "usdt", "sol", "eth", "btc", "you", "yourself",
               "one", "wrong", "right", "same", "other", "another", "here", "there", "now", "then", "what", "which", "how", "why", "when", "where",
               "risk", "risks", "data", "evidence", "exit", "exits", "entry", "supply", "holders", "price", "volume", "liquidity", "changes", "change",
               "are", "was", "were", "been", "being", "and", "but", "not", "for", "with", "from", "onto", "than", "then", "also", "very", "more",
               "most", "much", "many", "such", "like", "just", "only", "still", "again", "back", "over", "under", "about", "after", "before",
               "because", "while", "where", "whether", "should", "would", "could", "will", "shall", "can", "may", "might", "must", "have", "has", "had",
               "does", "did", "done", "doing", "get", "got", "give", "make", "made", "take", "show", "tell", "say", "said", "see", "know", "think",
               "want", "need", "help", "use", "used", "using", "please", "thanks", "buying", "selling", "trading", "holding", "moving", "going"}


def _lower_name(text: str) -> str | None:
    for pattern in (_LOWER_AFTER, _LOWER_BEFORE):
        for m in pattern.finditer(text or ""):
            word = next((g for g in m.groups() if g), "")
            if word and word.lower() not in _LOWER_STOP and word.islower():
                return word
    return None


def has_own_subject(request: str) -> bool:
    """Whether the message names something of its own: an address, a $ticker,
    a word the probe would look up, or a lowercase name in a data-ask
    position. A follow-up without one continues the conversation's subject."""
    text = request or ""
    if re.search(r"(?<![A-Za-z0-9])(?:0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})(?![A-Za-z0-9])", text):
        return True
    return subject_of(text) is not None or _lower_name(text) is not None


def subject_of(request: str) -> str | None:
    """The name worth looking up in this message, or None."""
    from app.tequity import fuzzy_venue

    starters = sentence_starters(request)
    for dollar, upper, capital in _SUBJECT.findall(request or ""):
        name = dollar or upper or capital
        if capital and capital in starters:
            continue
        if not dollar and fuzzy_venue(name) and name.lower() != fuzzy_venue(name):
            continue                                   # a venue typed a letter off is not a token ("hyperloquid", 2026-09-23); the venue itself is a subject
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


def market_scoped(request: str) -> str:
    """A question naming a ticker-like word, rewritten for a web search so it
    is read as a markets question. Live 2026-09-18: "What is OPEN?" came back
    as the English word, "Compare OPEN and MOVE" as two Nasdaq stocks. Idempotent."""
    marker = "(Context: this is a question to a crypto-first markets assistant."
    if marker in request:
        return request
    subject = subject_of(request)
    if not subject:
        return request
    return (f"{request}\n\n{marker} Read \"{subject}\" and any other ticker-like name in the question first as a crypto "
            f"token or protocol -- name each such token with its chain -- and then as a stock or company if one shares the "
            f"name; never as a dictionary word, a medicine, a civic organisation or a planet. If no token or company "
            f"exists, say so.)")


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
    probe identified -- and about markets at all. The web card is merged into
    the answer, so an off-subject one becomes the answer: TRUMP (probe: the
    Solana memecoin; web: the politician) and Mercury (probe: Mercury General
    on the NYSE; web: the planet's visibility) are both caught here. Every
    kind is held to the same test, because every kind routes a markets turn."""
    from app.clarify import is_market_text

    if not found or not context:
        return False
    return is_market_text(context)


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
