"""A listed coin's own facts (app/listed_asset.py): supply and market-cap
questions answer from CoinGecko's coin page by ticker, never a chain question."""
import asyncio

import pytest

from app import listed_asset, symbol_registry
from app.nodes import research
from app.provider_registry import get_provider_router
from app.provider_router import NoData

ZCASH = {"id": "zcash", "symbol": "zec", "name": "Zcash", "market_cap_rank": 9, "platforms": {}, "categories": ["Privacy Coins", "Proof of Work (PoW)"],
         "links": {"homepage": ["https://z.cash/"]},
         "market_data": {"current_price": {"usd": 62.41}, "market_cap": {"usd": 1_022_000_000}, "fully_diluted_valuation": {"usd": 1_310_000_000},
                         "total_volume": {"usd": 88_000_000}, "circulating_supply": 16_380_000, "total_supply": 16_380_000, "max_supply": 21_000_000,
                         "price_change_percentage_24h": 1.8, "price_change_percentage_7d": -4.2, "price_change_percentage_30d": 12.5,
                         "ath": {"usd": 3191.93}, "ath_date": {"usd": "2016-10-29T00:00:00.000Z"}, "ath_change_percentage": {"usd": -98.0},
                         "atl": {"usd": 17.93}, "atl_date": {"usd": "2020-03-13T02:29:47.000Z"}}}


def test_listed_asks_match_by_ticker_never_by_address():
    assert listed_asset.matches("What is the current total supply and circulating supply of the $ZEC token?")
    assert listed_asset.matches("BONK market cap and rank") and listed_asset.matches("what is the ATH of WIF")
    assert listed_asset.matches("price of ZEC (CoinGecko id zcash)")                     # the resolver's marker
    assert not listed_asset.matches("top holders of ZEC")                                 # a chain question, not a listed fact
    assert not listed_asset.matches("circulating supply of DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263 on solana")
    assert listed_asset.ticker_in("total supply of the $ZEC token") == "ZEC"
    assert listed_asset.ticker_in("what is the max supply") is None


def test_the_card_reads_supply_market_and_ath_from_the_coin_page(monkeypatch):
    monkeypatch.setattr(listed_asset, "_cg", lambda path, params=None: ZCASH if path == "/coins/zcash" else {})
    card = listed_asset.coin_snapshot("zcash")
    assert card.startswith("# Zcash (ZEC) — listed asset") and "**Rank**: #9" in card
    assert "- **Circulating**: 16.380M ZEC (78.0% of max)" in card and "- **Max**: 21.000M ZEC" in card
    assert "**Market cap**: $1.02B" in card and "**Fully diluted**: $1.31B" in card
    assert "**All-time high**: $3,191.93 on 2016-10-29 (-98.00% from it)" in card
    assert "none — a native coin on its own chain" in card and "Privacy Coins" in card
    assert "**All-time low**: $17.93 on 2020-03-13" in card
    assert listed_asset._usd(3.33e-06) == "$0.00000333" and listed_asset._usd(62.41, 4) == "$62.41" and listed_asset._usd(1496.0) == "$1,496.00"


def test_the_handler_uses_the_id_marker_or_the_clear_leader(monkeypatch):
    monkeypatch.setattr(listed_asset, "_cg", lambda path, params=None: ZCASH if path == "/coins/zcash" else {})
    monkeypatch.setattr(symbol_registry, "listed", lambda symbol: [{"id": "zcash", "name": "Zcash", "symbol": "ZEC", "rank": 9}] if symbol == "ZEC" else
                        [{"id": "a", "name": "A", "symbol": symbol, "rank": 100}, {"id": "b", "name": "B", "symbol": symbol, "rank": 120}])
    assert "Zcash (ZEC)" in listed_asset.listed_coin_snapshot("circulating supply of ZEC")
    assert "Zcash (ZEC)" in listed_asset.listed_coin_snapshot("supply? (CoinGecko id zcash)")
    with pytest.raises(NoData):
        listed_asset.listed_coin_snapshot("market cap of MON")                            # two close listings: no leader
    with pytest.raises(ValueError):
        listed_asset.listed_coin_snapshot("what is the max supply")


def test_the_tool_is_reachable_and_outranks_pair_searches_for_a_supply_ask():
    router = get_provider_router()
    ranked = [t.name for t in router._ranked_union("total supply and circulating supply of the $ZEC token (CoinGecko id zcash)", ("market_data", "token_discovery"), (), None)]
    assert ranked and ranked[0] == "coingecko_coin_snapshot", ranked


# ---- the resolver: a listed fact is about the listed coin, not a chain ----

def _coro(value):
    async def run():
        return value
    return run()


def test_a_supply_question_resolves_to_the_listed_coin_without_a_chain_question(monkeypatch):
    monkeypatch.setattr(research, "_listed_leader", lambda ticker: _coro({"id": "zcash", "name": "Zcash", "rank": 9}))
    monkeypatch.setattr(research, "_jupiter_solana_mint", lambda ticker: (_ for _ in ()).throw(AssertionError("no chain resolution for a listed fact")))
    out = asyncio.run(research._resolve_named_token("What is the current total supply and circulating supply of the $ZEC token?", {"market_data"}))
    assert out.clarification is None and out.chain is None
    assert out.request.endswith("(CoinGecko id zcash)") and "Zcash, CoinGecko rank 9" in out.note


def test_a_native_coin_with_no_contract_never_gets_a_chain_question(monkeypatch):
    monkeypatch.setattr(symbol_registry, "listed", lambda symbol: [{"id": "zcash", "name": "Zcash", "symbol": "ZEC", "rank": 9},
                                                                    {"id": "binance-peg-zcash-token", "name": "Binance-Peg ZEC", "symbol": "ZEC", "rank": None}])
    monkeypatch.setattr(symbol_registry, "contract", lambda coin_id: None)
    out = asyncio.run(research._listed_resolution("who made ZEC", "ZEC"))
    assert out.clarification is None and out.request.endswith("(CoinGecko id zcash)") and "native coin" in out.note


def test_a_holders_question_about_a_multi_chain_ticker_still_asks(monkeypatch):
    """The listed short-cut is for listed facts and native coins; "top holders
    of ZEC" with no clear leader is a chain question and keeps the existing behaviour."""
    monkeypatch.setattr(research, "_listed_leader", lambda ticker: _coro(None))
    monkeypatch.setattr(research, "_jupiter_solana_mint", lambda ticker: "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS")
    monkeypatch.setattr(research, "token_candidates", lambda ticker: [{"chain": "bsc", "address": "0x1Ba42e5193dfA8B03D15dd1B86a3113bbBEF8Eeb", "symbol": "ZEC",
                                                                        "name": "Zcash Token", "liquidity_usd": 1_800_000.0, "volume_24h_usd": 900_000.0}])
    monkeypatch.setattr(research, "_canonical_on_chain", lambda ticker, chain, strict=False: _coro({"chain": chain, "address": "0x1Ba42e5193dfA8B03D15dd1B86a3113bbBEF8Eeb", "symbol": "ZEC", "name": "Zcash Token", "liquidity_usd": 1_800_000.0}))
    monkeypatch.setattr(research, "_probe_chain", lambda request: _coro(None))
    out = asyncio.run(research._resolve_named_token("top holders of ZEC", {"token_discovery"}))
    assert out.clarification and "several chains" in out.clarification


def test_a_native_major_never_enters_the_chain_machinery_for_any_question(monkeypatch):
    """Live 2026-09-22: "BTC price and funding" resolved to a wrapped BTC on
    Tron by DEX liquidity and Birdeye was asked about a TRON address."""
    monkeypatch.setattr(research, "_listed_leader", lambda ticker: _coro({"id": "bitcoin", "name": "Bitcoin", "rank": 1}))
    monkeypatch.setattr(symbol_registry, "contract", lambda coin_id: None)
    monkeypatch.setattr(research, "_jupiter_solana_mint", lambda ticker: (_ for _ in ()).throw(AssertionError("a native coin is not resolved on a chain")))
    out = asyncio.run(research._resolve_named_token("BTC price, 24h change, funding rate, open interest right now", {"market_data"}))
    assert out.clarification is None and out.request.endswith("(CoinGecko id bitcoin)") and "native coin" in out.note


def test_a_contract_backed_leader_still_resolves_on_its_chain_for_a_chain_question(monkeypatch):
    monkeypatch.setattr(research, "_listed_leader", lambda ticker: _coro({"id": "bonk", "name": "Bonk", "rank": 151}))
    monkeypatch.setattr(symbol_registry, "contract", lambda coin_id: ("solana", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"))
    monkeypatch.setattr(research, "_jupiter_solana_mint", lambda ticker: "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")
    monkeypatch.setattr(research, "token_candidates", lambda ticker: [])
    out = asyncio.run(research._resolve_named_token("top holders of BONK", {"token_discovery"}))
    assert out.chain == "solana" and "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263 on solana" in out.request
