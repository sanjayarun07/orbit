"""Transcript of 2026-09-18: "open token details and current price details"
was answered with a DEX pair table mixing OpenLedger, an index token and
"Open tokens" across four chains as if they were one token. Found on the
way: the equity rule anchored on any cashtag, so "thoughts on $WIF" was a
stock question."""
import asyncio

import pytest

from app.nodes import research
from app.routing.intent_router import route_capabilities


@pytest.mark.parametrize("text,is_equity", [
    ("thoughts on $WIF", False), ("thoughts on $BONK", False), ("$OPEN price", False),
    ("$NVDA earnings", True), ("thoughts on $AAPL", True), ("NSE:RELIANCE results", True), ("Reliance stock outlook", True),
])
def test_a_cashtag_is_a_stock_only_for_a_known_instrument_or_exchange_prefix(text, is_equity):
    route = route_capabilities(text)
    assert (route is not None and route.reason == "equity") is is_equity, (text, route)


OPEN = [
    {"chain": "robinhood", "symbol": "OPENB", "name": "Opendoor Technologies Inc.", "address": "0x4b1Bb375" + "0" * 32, "liquidity_usd": 67_403_399.0},   # a tokenized stock: never offered
    {"chain": "bsc", "symbol": "OPEN", "name": "OpenLedger", "address": "0xA227Cc36" + "0" * 32, "liquidity_usd": 464_779.0},
    {"chain": "ethereum", "symbol": "OPEN", "name": "Open Stablecoin Index", "address": "0x323c03c4" + "0" * 32, "liquidity_usd": 294_762.0},
    {"chain": "base", "symbol": "OPEN", "name": "Open tokens", "address": "0xeeB88c57" + "0" * 32, "liquidity_usd": 109_315.0},
]


def _quiet(monkeypatch, candidates):
    monkeypatch.setattr(research, "_jupiter_solana_mint", lambda ticker: None)
    monkeypatch.setattr(research, "token_candidates", lambda ticker: list(candidates))

    async def none(*a, **k):
        return None

    monkeypatch.setattr(research, "_canonical_on_chain", none)
    monkeypatch.setattr(research, "_probe_chain", none)


def test_a_ticker_shared_by_several_tokens_is_asked_not_dumped(monkeypatch):
    _quiet(monkeypatch, OPEN)
    out = asyncio.run(research._resolve_named_token("open token details and current price details", {"market_data", "token_discovery"}))
    assert out.clarification and "several different tokens" in out.clarification
    assert "bsc (OpenLedger) `0xA227Cc36" in out.clarification and "(Open Stablecoin Index)" in out.clarification and "(Open tokens)" in out.clarification
    assert "robinhood" not in out.clarification, "a tokenized-stock mirror is not a token to pick"
    assert out.pending["symbol"] == "OPEN" and out.pending["candidates"][0]["name"] == "OpenLedger", "the follow-up reply picks from these"


@pytest.mark.parametrize("text,expected", [
    ("open token details and current price details", ["OPEN"]), ("bonk token price", ["BONK"]), ("meme token details", []), ("new token launches", []),
    ("the token price", []), ("ANSEM token", ["ANSEM"]),
])
def test_a_lower_case_name_before_token_and_a_data_noun_is_a_ticker(text, expected):
    assert research._named_tickers(text) == expected


def test_one_unsettled_candidate_still_goes_to_the_router(monkeypatch):
    _quiet(monkeypatch, OPEN[1:2])
    out = asyncio.run(research._resolve_named_token("price of OPEN", {"market_data"}))
    assert not out.clarification and out.request == "price of OPEN"


@pytest.mark.parametrize("candidate,mirror", [
    ({"chain": "robinhood", "symbol": "NVDAB", "name": "NVIDIA Corporation Common Stock"}, True),
    ({"chain": "robinhood", "symbol": "AAPLB", "name": "Apple Inc."}, True),
    ({"chain": "robinhood", "symbol": "ROBINHOOD", "name": "Robinhood"}, False),
    ({"chain": "robinhood", "symbol": "ROBIN", "name": "Robinhood"}, False),
    ({"chain": "hyperliquid", "symbol": "PURR", "name": "Purr"}, True),
    ({"chain": "solana", "symbol": "WIF", "name": "dogwifhat"}, False),
])
def test_only_tokenized_stocks_on_robinhood_chain_are_mirrors(candidate, mirror):
    """Robinhood Chain carries native memecoins as well as stock mirrors
    (user focus, 2026-09-18); the whole chain was being filtered."""
    assert research._is_mirror(candidate) is mirror
