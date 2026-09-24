"""The question contract: what a correct answer requires, decided before any tool runs.

Every review since 2026-09-18 found the same failure in a new coat: a
plausible tool response accepted as the answer to a different question
(CoinGecko's Base ecosystem list for "gainers on Base", a knowledge-base
passage for "recently", a memecoin meta bucket for "narratives"). The
contract names the subject, venue and scope, metric and unit, time window,
ranking and filters, freshness, and what evidence counts, so tool
eligibility (app/tool_catalog.eligible), fact normalisation (app/facts.py)
and the fact gate (app/fact_gate.py) all judge against one statement of
the ask instead of against each other's prose.

A planner model writes the contract; a rules planner covers tests, an
unavailable model, and the parts a model has no business inventing (the
venue words, the window). The planner may return an `ambiguity` question
instead of a contract when the ask cannot be pinned.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Literal

import dspy
from pydantic import BaseModel, Field

from app.routing import lexicon

logger = logging.getLogger(__name__)

Kind = Literal["market_ranking", "holders", "recent_events", "yields", "open_research", "portfolio", "transaction_intent", "other"]
# Kinds the nodes answer themselves and the gate then proves (the pipeline
# does not route them): the connected wallet's holdings, and a swap intent.
PROVED_KINDS: tuple[str, ...] = ("portfolio", "transaction_intent")
Scope = Literal["venue_trades", "global", "on_chain", "any"]

CONTRACT_KINDS: tuple[str, ...] = ("market_ranking", "holders", "recent_events", "yields")
OPEN_RESEARCH_KIND = "open_research"
# Exact state the web must never answer first: an address, a wallet, a quote, a price now, an exit, a position.
_EXACT_STATE = re.compile(r"(?<![A-Za-z0-9])(?:0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})(?![A-Za-z0-9])|\b(?:wallet|balance|balances|portfolio|position|exit|quote|swap|bridge|price\s+of|price\s+now|current\s+price|how\s+much\s+is|holders?|liquidity\s+of|tvl\s+of|apy|yield)\b", re.I)
_OPEN_RESEARCH = re.compile(r"\b(?:why|how|what|who|which|explain|compare|analy[sz]e|diligence|competitors?|investors?|backers?|revenue|risks?|outlook|history|background|roadmap|tokenomics|governance|research|deep\s+dive|overview|"
                            r"mean(?:s|ing)?|guarantee[sd]?|impl(?:y|ies)|does\s+that)\b", re.I)     # "does that mean I cannot get rugged?" asks what a concept means, not for a token check (2026-09-24)


def is_open_research(request: str) -> bool:
    """Open-ended research the web may lead: a question about a project, a
    move, a narrative or a mechanism, with no exact on-chain state in it."""
    text = request or ""
    return bool(_OPEN_RESEARCH.search(text)) and not _EXACT_STATE.search(text) and len(text.split()) >= 3


class Subject(BaseModel):
    kind: Literal["token", "protocol", "chain", "venue", "market", "topic", "wallet", "none"] = "none"
    id: str | None = None                      # address or mint when known
    symbol: str | None = None
    name: str | None = None
    chain: str | None = None


class QuestionContract(BaseModel):
    kind: Kind = "other"
    subject: Subject = Field(default_factory=Subject)
    venue: str | None = None                   # aster, hyperliquid, dexscreener, jupiter ...
    scope: Scope = "any"                       # what "on Base" means: trades on Base venues, or tokens associated with Base
    metric: str | None = None                  # price_change, volume, holders, apy, events
    unit: str | None = None                    # pct, usd, count
    window_hours: float | None = None
    direction: Literal["gainers", "losers", "top", "none"] = "none"
    limit: int = 10
    filters: dict = Field(default_factory=dict)     # stocks_only, crypto_only, single_asset, min_liquidity_usd, symbol
    freshness_seconds: int | None = None
    evidence_order: Literal["state_first", "discovery_first"] = "state_first"
    required_facts: list[str] = Field(default_factory=list)   # fact kinds the answer must rest on
    ambiguity: str | None = None               # the precise question to ask instead of answering
    confidence: float = 0.5
    planner: str = "rules"

    def label(self) -> str:
        parts = [self.kind]
        if self.subject.symbol or self.subject.name:
            parts.append(self.subject.symbol or self.subject.name)
        if self.subject.chain:
            parts.append(f"on {self.subject.chain}")
        if self.venue:
            parts.append(f"venue {self.venue}")
        if self.window_hours:
            parts.append(f"{self.window_hours:g}h")
        return " ".join(parts)


class QuestionPlan(dspy.Signature):
    """Write the contract a correct answer must satisfy, BEFORE any data is fetched.

    Read the user's exact words. Decide the kind: market_ranking (gainers,
    losers, most traded, trending tokens by a metric), holders (who holds a
    token, concentration), recent_events (news, votes, launches, what happened
    recently), yields (APY, where to earn), or other. Name the subject (a
    token with its chain when stated, a protocol, a chain, a venue, the whole
    market). Decide the scope of a chain word: "trades on Base" means
    venue_trades (only data from venues on Base can satisfy it); "tokens in
    the Base ecosystem" means global. When the words do not settle it, choose
    venue_trades for "on <chain>" with a ranking metric, and say so in
    `notes`. Give the metric and unit, the time window in hours (24 for "24h",
    168 for "this week", null when none), the direction and limit, filters the
    user stated (stocks only, excluding stocks, single-asset only, minimum
    liquidity), and the freshness in seconds an answer must have (3600 for
    live rankings, 86400*3 for "recently"). evidence_order is discovery_first
    for events, explanations and diligence, state_first for anything the user
    would act on (prices, rankings, holders, yields, positions). Never invent
    a subject; when the ask cannot be pinned (a bare symbol on several chains,
    a venue we do not know), set `ambiguity` to the one question that settles
    it and leave the rest empty. Output strict JSON for the contract fields.
    """

    request: str = dspy.InputField()
    conversation_context: str = dspy.InputField(desc="The resolved subject or focus from earlier turns, if any; empty otherwise")
    contract: str = dspy.OutputField(desc='JSON: {"kind","subject":{"kind","id","symbol","name","chain"},"venue","scope","metric","unit","window_hours","direction","limit","filters","freshness_seconds","evidence_order","required_facts","ambiguity","confidence","notes"}')


question_planner = dspy.Predict(QuestionPlan)

# ---------------------------------------------------------------------------
# the rules planner: deterministic, generic vocabulary, no per-prompt code
# ---------------------------------------------------------------------------

_VENUE = re.compile(r"\b(aster|hyperliquid|hl|dex\s*screener|dexscreener|jupiter|raydium|uniswap|binance|coinbase|pump\.?fun)\b", re.I)
_CHAIN = re.compile(r"\b(solana|base|ethereum|eth|arbitrum|optimism|polygon|bsc|bnb|avalanche|sui|hyperliquid|robinhood)\b", re.I)
_RANKING = re.compile(r"\b(?:gainers?|losers?|movers?|most\s+traded|top\s+(?:tokens?|coins?|pairs?|stocks?|volume)|by\s+volume|trending\s+(?:tokens?|coins?|pairs?)|pumping|dumping|biggest\s+(?:moves?|winners?|losers?)|volume\s+leaders?|rank\w*|(?:up|down)\s+the\s+most|best\s+performing|worst\s+performing|moving|moves?\s+(?:today|now|the\s+most))\b", re.I)
_HOLDERS = re.compile(r"\b(?:holders?|holding|concentration|who\s+(?:holds|owns)|top\s+wallets?|whales?)\b", re.I)
_YIELDS = re.compile(r"\b(?:yields?|apy|apr|earn|lending\s+rates?|staking\s+rates?|farm\w*)\b", re.I)
_EVENTS = re.compile(r"\b(?:news|headlines?|vote[ds]?|voting|proposal|governance|recently|latest|what\s+happened|announce\w*|launch(?:ed|es)?|hack(?:ed|s)?|exploit\w*|incidents?|this\s+week|today)\b", re.I)
_LOSERS = re.compile(r"\b(?:losers?|dumping|down\s+the\s+most|worst|laggards?)\b", re.I)
# A token's pools or pairs, listed: "BONK pools with at least $1M liquidity",
# "pairs for PEPE", "where does WIF trade" -- state read from the DEX
# aggregators, never from the web.
_POOL_LISTING = re.compile(r"\b(?:pools?|pairs?|liquidity\s+pools?|markets?\s+for|where\s+does\s+\S+\s+trade)\b", re.I)
# The connected wallet's own holdings, in the user's words and in the Home
# cards' words ("Analyze my portfolio", "Wallet health check", "What if my
# portfolio drops 20%?"): a portfolio contract, proved against the wallet's card.
_PORTFOLIO_ASK = re.compile(r"\b(?:my|connected)\b.{0,30}\b(?:portfolio|wallet|holdings?|balances?|allocation|positions?|assets?|health)\b"
                            r"|\bwallet\s+health(?:\s+check)?\b|\banaly[sz]e\s+(?:my\s+)?portfolio\b", re.I)
# A swap, sell, buy or bridge with an amount or a pair: a transaction intent,
# quoted for the exact size or refused with the exact reason, never a
# guess ("Start a cross-chain swap" got a generic clarification, 2026-09-23).
_TRANSACTION = re.compile(r"\b(?:swap(?:ped|ping)?|sell(?:ing)?|sold|buy(?:ing)?|bought|bridg(?:e|ed|ing)|convert(?:ed|ing)?|exchang(?:e|ed|ing))\b.{0,60}\b(?:\d+(?:\.\d+)?\s*[A-Za-z$][A-Za-z0-9]{1,9}|[A-Z]{2,10}\s+(?:to|into|for)\s+[A-Z]{2,10})"
                          r"|\b(?:start|begin|prepare|quote)\s+(?:a\s+|an\s+|the\s+)?(?:cross[- ]chain\s+)?(?:swap|bridge|trade|quote)\b|\bquote\s+me\b"
                          r"|\bexit\b.{0,40}\bposition\b|\bexit\s+(?:quotes?|analysis)\b", re.I)
_EXIT_ASK = re.compile(r"\bexit\b.{0,40}\bposition\b|\bexit\s+(?:quotes?|analysis)\b", re.I)
_EXPLANATION_ASK = re.compile(r"^\s*(?:how\s+(?:does|do|is|to)|what\s+is|what\s+are|explain|why)\b", re.I)
_TRADES_ON = re.compile(r"\b(?:trades?|trading|traded|volume|pairs?|pools?|dex(?:es)?)\s+on\b|\bon[- ]venue\b|\bvenue\b", re.I)
_ECOSYSTEM = re.compile(r"\becosystem\b|\bassociated\s+with\b|\bglobal\b", re.I)
_WINDOW = re.compile(r"\b(?:last|past|previous)\s+(\d+)\s*(h(?:ours?)?|d(?:ays?)?|w(?:eeks?)?)\b|\b(\d+)\s*(h|d|w)\b|\b(24\s*h(?:ours)?|this\s+week|past\s+week|last\s+week|today|this\s+morning|this\s+month|7d|30d)\b", re.I)
_STOCKS = re.compile(r"\b(?:tokeni[sz]ed\s+)?(?:stocks?|equit(?:y|ies)|shares)\b", re.I)
_NO_STOCKS = re.compile(r"\b(?:excluding|exclude|without|no|not|minus)\s+(?:the\s+)?(?:tokeni[sz]ed\s+)?(?:stocks?|equit(?:y|ies))\b|\bcrypto\s+only\b", re.I)
_SINGLE = re.compile(r"\b(?:without\s+(?:exposing\s+me\s+to\s+)?(?:another|other|any|a)\s+(?:volatile\s+)?(?:token|asset|coin)s?|single[- ]asset|no\s+(?:il|impermanent\s+loss))\b", re.I)
_MIN_LIQ = re.compile(r"(?:at\s+least|over|above|minimum(?:\s+of)?|min)\s+\$?\s*([\d,]+(?:\.\d+)?)\s*([kKmM]?)\s*(?:in\s+)?(?:liquidity|tvl)", re.I)
_LIMIT = re.compile(r"\btop\s+(\d{1,3})\b", re.I)
_ADDRESS = re.compile(r"(?<![A-Za-z0-9])(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})(?![A-Za-z0-9])")


def _window_hours(text: str) -> float | None:
    m = _WINDOW.search(text or "")
    if not m:
        return None
    if m.group(1):
        n, u = int(m.group(1)), m.group(2).lower()[0]
    elif m.group(3):
        n, u = int(m.group(3)), m.group(4).lower()[0]
    else:
        word = m.group(5).lower()
        if "24" in word:
            return 24.0
        if "week" in word or word == "7d":
            return 168.0
        if "month" in word or word == "30d":
            return 720.0
        if word == "today":
            # A ranking "today" is the day's change: the daily sources' 24h
            # figure, not the hours since midnight UTC (at 02:43 UTC "today"
            # was 2.7 hours and no source was eligible, fourth frozen run
            # 2026-09-24). The tick ledger reads "since midnight" from the
            # words themselves (tequity.period_start), not from this window.
            return 24.0
        if word == "this morning":
            now = datetime.now(timezone.utc)
            return max(1.0, (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 3600)
        return None
    return float(n * (1 if u == "h" else 24 if u == "d" else 168))


def _chain_of(text: str) -> str | None:
    m = _CHAIN.search(text or "")
    if not m:
        return None
    word = m.group(1).lower()
    return {"eth": "ethereum", "bnb": "bsc"}.get(word, word)


def _venue_of(text: str) -> str | None:
    m = _VENUE.search(text or "")
    if not m:
        from app.tequity import fuzzy_venue
        for word in re.findall(r"[A-Za-z]{5,}", text or ""):
            venue = fuzzy_venue(word)
            if venue:
                return venue
        return None
    word = m.group(1).lower().replace(" ", "")
    return {"hl": "hyperliquid", "pumpfun": "pump.fun", "pump.fun": "pump.fun"}.get(word, word)


def plan_by_rules(request: str, context: str = "") -> QuestionContract:
    """A contract from generic vocabulary alone. The kinds it names are the
    four the pipeline serves; anything else is `other` and the legacy path."""
    text = request or ""
    chain, venue = _chain_of(text), _venue_of(text)
    if venue in ("hyperliquid",) and chain == "hyperliquid":
        chain = None
    symbol = None
    m = re.search(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b", text) or re.search(r"\b([A-Z][A-Z0-9]{2,9})\b", re.sub(r"\b(?:USD|USDC|USDT|APY|APR|TVL|LP|DEX|AI|ETF|NFT|CLMM|DLMM)\b", " ", text))
    if m:
        symbol = m.group(1).upper()
    address = _ADDRESS.search(text)
    filters: dict = {}
    if _NO_STOCKS.search(text):
        filters["crypto_only"] = True
    elif _STOCKS.search(text):
        filters["stocks_only"] = True
    if _SINGLE.search(text):
        filters["single_asset"] = True
    liq = _MIN_LIQ.search(text)
    if liq:
        value = float(liq.group(1).replace(",", "")) * {"k": 1e3, "m": 1e6}.get(liq.group(2).lower(), 1)
        filters["min_liquidity_usd"] = value
    limit = int(_LIMIT.search(text).group(1)) if _LIMIT.search(text) else 10
    window = _window_hours(text)
    explanation = bool(re.match(r"\s*(?:why|what(?:'s| is)\s+(?:driving|behind|moving))\b", text, re.I))
    if _TRANSACTION.search(text) and not _EXPLANATION_ASK.search(text) and (not _PORTFOLIO_ASK.search(text) or _EXIT_ASK.search(text)):
        # The fields the message evidences, and the ones a quote still needs.
        # "quote me 1 SOL to USDC" and "if I sold 0.05 SOL for USDC" are swap
        # intents in other words; an exit ask takes its size from the wallet.
        from app.routing.trade_parser import parse_execution_draft
        normalised = re.sub(r"\bquote\s+me\b", "swap", re.sub(r"\b(?:sold|sell(?:ing)?)\b", "sell", text, flags=re.I), flags=re.I)
        draft = parse_execution_draft(normalised, (chain,) if chain else ())
        if draft.destination_chain is None and draft.source_chain is not None and not re.search(r"\b(?:cross[- ]chain|bridge)\b", text, re.I):
            import dataclasses
            draft = dataclasses.replace(draft, destination_chain=draft.source_chain)          # a same-chain swap names one chain
        missing = () if _EXIT_ASK.search(text) else draft.missing()
        words = {"amount": "the amount", "input_token": "the token to sell", "output_token": "the token to buy", "source_chain": "the chain", "destination_chain": "the destination chain"}
        ambiguity = None
        if missing:
            need = [words[m] for m in missing if not (m == "destination_chain" and "source_chain" in missing)]
            ambiguity = "To quote this I need " + ", ".join(need[:-1]) + (" and " if len(need) > 1 else "") + need[-1] + ". Nothing is prepared until then."
        return QuestionContract(kind="transaction_intent", subject=Subject(kind="token", symbol=(draft.input_token or draft.output_token or symbol or "").upper() or None, chain=draft.source_chain or chain),
                                scope="on_chain", metric="quote", unit="usd", filters={k: v for k, v in draft.as_dict().items() if v is not None},
                                freshness_seconds=120, evidence_order="state_first", required_facts=["quote_row"], ambiguity=ambiguity, confidence=0.7, planner="rules")
    if _PORTFOLIO_ASK.search(text) and not _HOLDERS.search(text) and not _TRANSACTION.search(text):
        return QuestionContract(kind="portfolio", subject=Subject(kind="wallet", chain=chain), scope="on_chain", metric="holdings", unit="usd",
                                filters={"scenario_pct": float(m2.group(1))} if (m2 := re.search(r"(?:drops?|falls?|rises?|up|down|gains?|loses?)\s+(?:by\s+)?([+-]?\d+(?:\.\d+)?)\s*%", text, re.I)) else {},
                                freshness_seconds=3600, evidence_order="state_first", required_facts=["holding_row"], confidence=0.7, planner="rules")
    if _POOL_LISTING.search(text) and (symbol or address) and not _HOLDERS.search(text) and not _YIELDS.search(text.split(".")[0]):
        # "Find BONK pools on Solana with at least $1,000,000 liquidity": the
        # token's pools by liquidity, exact on-chain state (the frozen trust
        # run, 2026-09-23, sent it to the web and repeated a $239.5M pool
        # from a tracker page).
        return QuestionContract(kind="market_ranking", subject=Subject(kind="token", id=address.group(1) if address else None, symbol=symbol, chain=chain),
                                venue=venue, scope="venue_trades", metric="liquidity", unit="usd", window_hours=window or 24.0, direction="top",
                                limit=limit, filters=filters, freshness_seconds=3600, evidence_order="state_first",
                                required_facts=["ranking_row"], confidence=0.6, planner="rules")
    if _RANKING.search(text) and not _HOLDERS.search(text) and not explanation:
        metric = "volume" if re.search(r"\b(?:most\s+traded|by\s+volume|volume\s+leaders?|top\s+volume)\b", text, re.I) else "price_change"
        scope: Scope = "any"
        if chain or venue:
            scope = "global" if _ECOSYSTEM.search(text) else "venue_trades"
        return QuestionContract(kind="market_ranking", subject=Subject(kind="venue" if venue else "chain" if chain else "market", name=venue or chain, chain=chain),
                                venue=venue, scope=scope, metric=metric, unit="pct" if metric == "price_change" else "usd",
                                window_hours=window or 24.0, direction="losers" if _LOSERS.search(text) else "gainers" if metric == "price_change" else "top",
                                limit=limit, filters=filters, freshness_seconds=3600, evidence_order="state_first",
                                required_facts=["ranking_row"], confidence=0.6, planner="rules")
    if _HOLDERS.search(text) and (symbol or address):
        return QuestionContract(kind="holders", subject=Subject(kind="token", id=address.group(1) if address else None, symbol=symbol, chain=chain),
                                scope="on_chain", metric="holders", unit="pct", limit=max(limit, 10), filters=filters, freshness_seconds=3600,
                                evidence_order="state_first", required_facts=["holder_row"], confidence=0.6, planner="rules")
    if _YIELDS.search(text) and not _EVENTS.search(text):
        return QuestionContract(kind="yields", subject=Subject(kind="token", symbol=symbol, chain=chain), scope="global", metric="apy", unit="pct",
                                filters=filters, freshness_seconds=3600, evidence_order="state_first", required_facts=["yield_row"], confidence=0.6, planner="rules")
    if _EVENTS.search(text) and not _RANKING.search(text):
        return QuestionContract(kind="recent_events", subject=Subject(kind="topic", symbol=symbol, name=None, chain=chain), scope="any", metric="events",
                                window_hours=window or 24 * 30, freshness_seconds=3 * 86400, evidence_order="discovery_first",
                                required_facts=["event"], confidence=0.5, planner="rules")
    if is_open_research(text):
        return QuestionContract(kind="open_research", subject=Subject(kind="topic", symbol=symbol, chain=chain), scope="any", metric="events",
                                window_hours=window, freshness_seconds=7 * 86400, evidence_order="discovery_first",
                                required_facts=["source"], confidence=0.5, planner="rules")
    return QuestionContract(kind="other", planner="rules", confidence=0.3)


def _parse_model_contract(raw: str) -> QuestionContract | None:
    import json

    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    try:
        data = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    data.pop("notes", None)
    subject = data.get("subject") or {}
    if not isinstance(subject, dict):
        subject = {}
    data["subject"] = {k: v for k, v in subject.items() if k in Subject.model_fields}
    data = {k: v for k, v in data.items() if k in QuestionContract.model_fields}
    try:
        return QuestionContract(**data, planner="model") if "planner" not in data else QuestionContract(**data)
    except Exception:
        logger.info("planner contract did not validate: %r", text[:200])
        return None


async def plan(request: str, context: str = "") -> QuestionContract:
    """The contract for a request: the planner model's when available and
    valid, with the rules planner's venue, window and filter readings kept
    where the model left them empty; the rules planner alone otherwise."""
    rules = plan_by_rules(request, context)
    from app.nodes import runtime

    if not runtime.planner_available():
        return rules
    try:
        result = await runtime._call_planner_lm(question_planner, request=request, conversation_context=context or "")
        modelled = _parse_model_contract(getattr(result, "contract", "") or "")
    except Exception:
        logger.info("planner model failed; rules contract", exc_info=True)
        modelled = None
    if modelled is None:
        return rules
    # The words that carry a venue, a window or a filter are read deterministically too:
    # a model that dropped them does not get to widen the ask.
    if rules.venue and not modelled.venue:
        modelled.venue = rules.venue
    if rules.window_hours and not modelled.window_hours:
        modelled.window_hours = rules.window_hours
    for key, value in rules.filters.items():
        modelled.filters.setdefault(key, value)
    if modelled.kind == "other" and rules.kind != "other" and modelled.confidence < 0.7:
        modelled.kind = rules.kind
        modelled.required_facts = modelled.required_facts or rules.required_facts
    modelled.planner = "model"
    return modelled
