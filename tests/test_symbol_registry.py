"""Which crypto asset a ticker means (app/symbol_registry.py). User rule
(2026-09-18): "OPEN token should take only crypto assets in the result; it
is fetching Open Stablecoin Index." The listing the market uses decides."""
import asyncio

import pytest

from app import symbol_registry
from app.nodes import research

SEARCH = {
    "OPEN": [{"id": "openledger-2", "symbol": "OPEN", "name": "OpenLedger", "market_cap_rank": 673}, {"id": "open", "symbol": "OPEN", "name": "OPEN", "market_cap_rank": 1963},
             {"id": "open-stablecoin-index", "symbol": "OPEN", "name": "Open Stablecoin Index", "market_cap_rank": 4991}, {"id": "openx", "symbol": "OPENX", "name": "not this", "market_cap_rank": 5}],
    "MON": [{"id": "monad", "symbol": "MON", "name": "Monad", "market_cap_rank": 144}, {"id": "mon-protocol", "symbol": "MON", "name": "MON Protocol", "market_cap_rank": 1360}],
    "MOVE": [{"id": "movement", "symbol": "MOVE", "name": "Movement", "market_cap_rank": 567}, {"id": "moveapp", "symbol": "MOVE", "name": "MoveApp", "market_cap_rank": None}],
    "ZZZ": [],
}
COINS = {
    "openledger-2": {"id": "openledger-2", "symbol": "open", "platforms": {"binance-smart-chain": "0xa227cc36938f0c9e09ce0e64dfab226cad739447", "ethereum": "0xa227cc36938f0c9e09ce0e64dfab226cad739447"}},
    "monad": {"id": "monad", "symbol": "mon", "platforms": {"monad": "0xmon"}},
    "mon-protocol": {"id": "mon-protocol", "symbol": "mon", "platforms": {"ethereum": "0x" + "1" * 40}},
}


@pytest.fixture(autouse=True)
def _cg(monkeypatch):
    symbol_registry.reset()

    def fake(path, params=None):
        if path == "/search":
            return {"coins": SEARCH.get(params["query"], [])}
        return COINS[path.rsplit("/", 1)[1]]

    monkeypatch.setattr(symbol_registry, "_cg", fake)
    monkeypatch.setattr(symbol_registry, "_liquid_chain", lambda symbol, platforms: "bsc" if "bsc" in platforms else None)
    monkeypatch.setattr(symbol_registry, "listed", symbol_registry.listed.__wrapped__ if hasattr(symbol_registry.listed, "__wrapped__") else _real_listed)
    yield
    symbol_registry.reset()


_real_listed = symbol_registry.listed


def test_listed_coins_come_ranked_and_only_with_the_exact_symbol():
    rows = symbol_registry.listed("open")
    assert [r["name"] for r in rows] == ["OpenLedger", "OPEN", "Open Stablecoin Index"] and rows[0]["rank"] == 673
    assert symbol_registry.listed("ZZZ") == []


def test_a_clear_leader_is_the_token_and_close_ranks_are_not():
    assert symbol_registry.leader(symbol_registry.listed("OPEN"))["id"] == "openledger-2"
    assert symbol_registry.leader(symbol_registry.listed("MON"))["id"] == "monad", "Monad leads MON Protocol; the ask below comes from its chain being uncovered"
    assert symbol_registry.leader([{"id": "a", "name": "A", "symbol": "X", "rank": 300}, {"id": "b", "name": "B", "symbol": "X", "rank": 450}]) is None, "300 vs 450 is close: ask"
    assert symbol_registry.leader(symbol_registry.listed("MOVE"))["id"] == "movement", "an unranked runner-up never blocks"
    assert symbol_registry.leader([]) is None


def test_the_contract_is_on_the_chain_where_it_trades():
    assert symbol_registry.contract("openledger-2") == ("bsc", "0xa227cc36938f0c9e09ce0e64dfab226cad739447")
    assert symbol_registry.contract("monad") is None, "a chain the tools do not cover"


def test_open_token_resolves_to_openledger_never_the_index(monkeypatch):
    monkeypatch.setattr(research, "_jupiter_solana_mint", lambda t: None)
    monkeypatch.setattr(research, "token_candidates", lambda t: [{"chain": "ethereum", "symbol": "OPEN", "name": "Open Stablecoin Index", "address": "0x323c03c4" + "0" * 32, "liquidity_usd": 294_762.0}])
    out = asyncio.run(research._resolve_named_token("OPEN token details and current price", {"market_data", "token_discovery"}))
    assert not out.clarification and out.chain == "bsc" and out.request.endswith("0xa227cc36938f0c9e09ce0e64dfab226cad739447 on bsc")


def test_two_close_listings_become_a_question_naming_them(monkeypatch):
    monkeypatch.setattr(research, "_jupiter_solana_mint", lambda t: None)
    monkeypatch.setattr(research, "token_candidates", lambda t: [{"chain": "solana", "symbol": "MON", "name": "MON", "address": "So1" + "1" * 40, "liquidity_usd": 270_000.0}])
    out = asyncio.run(research._resolve_named_token("price of MON", {"market_data"}))
    assert out.clarification and "(Monad, CoinGecko rank 144)" in out.clarification and "(MON Protocol, CoinGecko rank 1360)" in out.clarification
    assert "a chain the tools do not cover (Monad" in out.clarification and "So1" not in out.clarification, "pool dust is never offered"
