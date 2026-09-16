"""The fall-through log: research turns that ended in web search because no
data tool covered the question. Each is tagged with the data topic it was
really asking for, so "should we pay for DefiLlama's API tier" (treasuries,
ETF flows, unlocks, RWA) or "which free source should we add next" is a
count, not a guess. Read it at GET /admin/research/gaps.

Only research turns are considered, only when a web/news search produced
the answer and no structured data tool did. Nothing here blocks or slows a
turn: recording is best-effort and swallows its own errors.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, deque
from datetime import datetime, timedelta, timezone

from app.db import get_pg_pool

logger = logging.getLogger(__name__)

WEB_TOOLS = {"perplexity_web_search", "perplexity_finance_search", "web_search", "finance_search"}
_IGNORED = {"semantic_cache", "finish", "search_verified_tokens", "sol_balance"}

# Topic -> what would answer it (used in the admin view to say what each gap needs).
TOPICS: list[tuple[str, re.Pattern, str]] = [
    # Order matters: the more specific topic first ("tokenized treasuries" is RWA, not a DAO treasury).
    ("rwa", re.compile(r"\b(?:rwa|real[- ]world assets?|tokeni[sz]ed (?:treasur|t-bill|bond|stock|equit)\w*|ondo|buidl)\b", re.I), "DefiLlama API tier: /rwa/*"),
    ("treasury", re.compile(r"\b(?:treasur(?:y|ies)|dao (?:funds|holdings|balance)|runway)\b", re.I), "DefiLlama API tier: /api/treasuries"),
    ("etf_flows", re.compile(r"\b(?:etfs?|spot (?:bitcoin|ether|btc|eth) fund|ibit|fbtc|gbtc|etha)\b", re.I), "DefiLlama API tier: /etfs/flows"),
    ("unlocks", re.compile(r"\b(?:unlocks?|vesting|cliff|emission schedule|token release)\b", re.I), "DefiLlama API tier: /api/emissions (free datasets host today)"),
    ("bridge_volume", re.compile(r"\b(?:bridge (?:volume|flows?|inflows?|outflows?)|bridged (?:in|out)|cross-chain volume)\b", re.I), "DefiLlama API tier: /bridges/*"),
    ("funding_rates", re.compile(r"\b(?:funding rates?|perp funding|basis trade)\b", re.I), "DefiLlama API tier: /yields/perps"),
    ("lsd_rates", re.compile(r"\b(?:lst|lsd|liquid staking) (?:rates?|yields?|apy)\b", re.I), "DefiLlama API tier: /yields/lsdRates"),
    ("inflows", re.compile(r"\b(?:inflows?|outflows?|net flows?|capital (?:in|out)flow)\b", re.I), "DefiLlama API tier: /api/inflows"),
    ("institutional", re.compile(r"\b(?:microstrategy|strategy inc|institutional (?:holdings|buyers)|corporate treasur|digital asset treasur)\b", re.I), "DefiLlama API tier: /dat/institutions"),
    ("equities", re.compile(r"\b(?:earnings call|10-k|10-q|sec filing|balance sheet|income statement|pre-ipo|ipo)\b", re.I), "DefiLlama API tier: /equities/*, /pre-ipo/*"),
    ("fees_revenue", re.compile(r"\b(?:fees?|revenue|earnings)\b", re.I), "free: defillama_fees_revenue (should have matched)"),
    ("yields", re.compile(r"\b(?:apy|apr|yields?)\b", re.I), "free: defillama_yields (should have matched)"),
    ("stablecoins", re.compile(r"\b(?:stablecoins?|peg|depeg|backing|reserves)\b", re.I), "knowledge base: stablecoins (should have matched)"),
    ("incidents", re.compile(r"\b(?:hack(?:ed|s)?|exploit(?:ed|s)?|drained|rug(?:ged| pull)?)\b", re.I), "knowledge base: incidents (should have matched)"),
    ("governance", re.compile(r"\b(?:proposal|governance|vote[ds]?|snapshot)\b", re.I), "knowledge base: governance (should have matched)"),
]

_memory: deque = deque(maxlen=2000)
_ready = False


def classify(request: str) -> str:
    for topic, pattern, _ in TOPICS:
        if pattern.search(request or ""):
            return topic
    return "other"


def needs_for(topic: str) -> str:
    return next((needs for t, _, needs in TOPICS if t == topic), "no structured source identified")


def is_fallthrough(intent: str | None, tools: list[str]) -> bool:
    """A research turn answered by web search with no data tool alongside."""
    if intent != "research" or not tools:
        return False
    names = {t for t in tools if t not in _IGNORED}
    return bool(names & WEB_TOOLS) and not (names - WEB_TOOLS)


async def _ensure_table(pool) -> None:
    global _ready
    if _ready:
        return
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS research_gaps (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            request TEXT NOT NULL,
            topic TEXT NOT NULL,
            tools TEXT[] NOT NULL DEFAULT '{}',
            account_id TEXT,
            session_id TEXT
        );
        CREATE INDEX IF NOT EXISTS research_gaps_topic_time ON research_gaps (topic, created_at DESC);
        """
    )
    _ready = True


async def record(request: str, intent: str | None, tools: list[str], account_id: str | None = None, session_id: str | None = None) -> str | None:
    """Log a fall-through; returns the topic when one was recorded."""
    if not is_fallthrough(intent, tools):
        return None
    topic = classify(request)
    row = {"created_at": datetime.now(timezone.utc), "request": (request or "")[:500], "topic": topic, "tools": [t for t in tools if t not in _IGNORED][:6], "account_id": account_id, "session_id": session_id}
    try:
        pool = await get_pg_pool()
        if pool is not None:
            await _ensure_table(pool)
            await pool.execute("INSERT INTO research_gaps (request, topic, tools, account_id, session_id) VALUES ($1, $2, $3, $4, $5)",
                               row["request"], topic, row["tools"], account_id, session_id)
        else:
            _memory.appendleft(row)
    except Exception:
        logger.warning("research gap recording failed", exc_info=True)
        _memory.appendleft(row)
    return topic


async def summary(days: int = 30, samples: int = 5) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    rows: list[dict] = []
    pool = await get_pg_pool()
    if pool is not None:
        try:
            await _ensure_table(pool)
            fetched = await pool.fetch("SELECT created_at, request, topic, tools FROM research_gaps WHERE created_at >= $1 ORDER BY created_at DESC LIMIT 5000", since)
            rows = [dict(r) for r in fetched]
        except Exception:
            logger.warning("research gap summary failed", exc_info=True)
    if not rows:
        rows = [r for r in _memory if r["created_at"] >= since]
    counts = Counter(r["topic"] for r in rows)
    topics = []
    for topic, n in counts.most_common():
        recent = [r for r in rows if r["topic"] == topic][:samples]
        topics.append({"topic": topic, "count": n, "needs": needs_for(topic),
                       "samples": [{"request": r["request"], "at": r["created_at"].isoformat() if isinstance(r["created_at"], datetime) else str(r["created_at"]), "tools": list(r["tools"] or [])} for r in recent]})
    return {"days": days, "total": len(rows), "topics": topics}


def reset() -> None:
    global _ready
    _memory.clear()
    _ready = False
