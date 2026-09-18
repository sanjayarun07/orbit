"""Transcript of 2026-09-18: a CoinMarketCap link to OpenLedger was answered
with "I couldn't access the CoinMarketCap page". The link names the token;
the turn answers from our own data."""
import asyncio

import pytest

from app import token_pages
from app.nodes import research

CG_OPENLEDGER = {"id": "openledger-2", "symbol": "open", "name": "OpenLedger",
                 "platforms": {"": "", "binance-smart-chain": "0xa227cc36938f0c9e09ce0e64dfab226cad739447", "ethereum": "0xa227cc36938f0c9e09ce0e64dfab226cad739447"}}


def _cg(monkeypatch):
    calls = []

    def fake(path, params=None):
        calls.append(path)
        if path == "/coins/openledger":
            return {"error": "coin not found"}
        if path == "/search":
            return {"coins": [{"id": "openledger-2", "symbol": "OPEN", "name": "OpenLedger"}]}
        if path == "/coins/openledger-2":
            return CG_OPENLEDGER
        raise AssertionError(path)

    monkeypatch.setattr(token_pages, "_cg", fake)
    monkeypatch.setattr(token_pages, "_liquid_chain", lambda symbol, platforms: None)
    return calls


def test_a_coinmarketcap_link_becomes_the_token_it_names(monkeypatch):
    calls = _cg(monkeypatch)
    page = token_pages.parse("https://coinmarketcap.com/currencies/openledger/")
    assert page.symbol == "OPEN" and page.name == "OpenLedger" and page.chain == "ethereum" and page.address == "0xa227cc36938f0c9e09ce0e64dfab226cad739447"
    assert calls == ["/coins/openledger", "/search", "/coins/openledger-2"], "the slug first, then a search when CoinGecko names it differently"
    assert page.rewrite("https://coinmarketcap.com/currencies/openledger/") == "OPEN token details and current price 0xa227cc36938f0c9e09ce0e64dfab226cad739447 on ethereum"
    assert page.rewrite("holders of https://coinmarketcap.com/currencies/openledger/") == "OPEN token holders of 0xa227cc36938f0c9e09ce0e64dfab226cad739447 on ethereum"
    assert page.note() == "_Read the CoinMarketCap link as OpenLedger (OPEN) on ethereum._"


@pytest.mark.parametrize("url,source,chain,address", [
    ("https://dexscreener.com/solana/EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", "DEX Screener", "solana", "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"),
    ("https://birdeye.so/token/EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm?chain=solana", "Birdeye", "solana", "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"),
    ("https://solscan.io/token/EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", "solscan.io", "solana", "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"),
    ("https://basescan.org/token/0x940181a94A35A4569E4529A3CDfB74e38FD98631", "basescan.org", "base", "0x940181a94A35A4569E4529A3CDfB74e38FD98631"),
])
def test_explorer_and_dex_links_carry_the_contract(url, source, chain, address):
    page = token_pages.parse(f"what is this {url}")
    assert page.source == source and page.chain == chain and page.address == address


def test_a_news_or_docs_link_is_not_a_token_page():
    assert token_pages.parse("summarize https://www.coindesk.com/markets/2026/09/18/some-story") is None
    assert token_pages.parse("https://docs.openledger.foundation/open-tokenomics") is None
    assert token_pages.parse("no link here") is None


def test_the_research_node_answers_a_token_link_from_our_own_data(monkeypatch):
    seen = {}

    async def inner(state, sink):
        seen.update(state)
        return {"answer": "# Token (OPEN)\n| price | $0.137 |", "trajectory": {"tool_name_0": "birdeye_token_overview"}}

    monkeypatch.setattr(research, "_research_node", inner)
    monkeypatch.setattr(token_pages, "parse", lambda request: token_pages.TokenPage("CoinMarketCap", symbol="OPEN", name="OpenLedger", chain="ethereum", address="0xa227cc36938f0c9e09ce0e64dfab226cad739447", slug="openledger"))
    out = asyncio.run(research.research_node({"request": "https://coinmarketcap.com/currencies/openledger/", "capabilities": ["url_fetch"], "chains": [], "session_context": {}}))
    assert seen["request"].startswith("OPEN token") and seen["request"].endswith("0xa227cc36938f0c9e09ce0e64dfab226cad739447 on ethereum")
    assert seen["capabilities"] == ["market_data", "token_discovery"] and seen["chains"] == ["ethereum"]
    assert out["answer"].startswith("_Read the CoinMarketCap link as OpenLedger (OPEN) on ethereum._\n\n# Token (OPEN)")


def test_the_chain_is_the_one_where_the_contract_trades(monkeypatch):
    import app.token_resolve as tr
    monkeypatch.setattr(tr, "token_candidates", lambda symbol, chains=(): [
        {"chain": "ethereum", "address": "0xA227cc36938f0c9e09ce0e64dfab226cad739447", "liquidity_usd": 257_000.0},
        {"chain": "bsc", "address": "0xa227cc36938f0c9e09ce0e64dfab226cad739447", "liquidity_usd": 464_000.0},
        {"chain": "robinhood", "address": "0xdifferent", "liquidity_usd": 50_000_000.0},
    ])
    platforms = {"ethereum": "0xa227cc36938f0c9e09ce0e64dfab226cad739447", "bsc": "0xa227cc36938f0c9e09ce0e64dfab226cad739447"}
    assert token_pages._liquid_chain("OPEN", platforms) == "bsc"
    assert token_pages._liquid_chain("OPEN", {"bsc": "0x1"}) is None, "one chain needs no lookup"
