"""The live meme set of 2026-09-18 (reports/meme-live-20260918): sheet-listed
trader wallets, Solana pump.fun tokens and Robinhood Chain memecoins. Three
misses it found, each pinned here:

- a Solana wallet portfolio went to the per-chain provider although Mobula
  is first for wallet tracking (the portfolio intercept bypassed the router);
- "deep dive on 0x… on robinhood" became an equity brief about HOOD;
- "what else did <wallet> deploy" reached no tool ("deploy" was not in the
  deployer vocabulary).
"""
import asyncio

from app import mobula_meme, mobula_wallet
from app.nodes import research
from app.routing.entities import extract_chains
from app.settings import settings


def test_robinhood_after_an_address_is_a_chain():
    assert extract_chains("deep dive on 0x500CeE0c4184faF3B3d0Cf8c0380B57B2dE44607 on robinhood") == ("robinhood",)
    assert extract_chains("ROBINHOOD token price on robinhood chain") == ("robinhood",)


def test_deploy_reaches_the_deployer_tool():
    assert mobula_meme.DEPLOYER_ASK.search("what else did 9BMzTpSo4URse1oN666pmexhdjpU1vA5p7LtroCFQdLU deploy")
    assert mobula_meme.DEPLOYER_ASK.search("tokens launched by this dev")


def test_the_portfolio_intercept_asks_mobula_first(monkeypatch):
    monkeypatch.setattr(settings, "mobula_api_key", "test-key")
    seen = {}
    monkeypatch.setattr(mobula_wallet, "portfolio", lambda request: seen.setdefault("request", request) and "# Wallet portfolio — HDix…N3o\n| BONK | Solana | 1,000 |")

    async def never(*a, **k):
        raise AssertionError("the per-chain balances path must not run when Mobula answered")

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(research, "_bitquery_balances_supplement", never)
    monkeypatch.setattr(research, "_goldrush_hyperliquid_supplement", nothing)
    monkeypatch.setattr(research, "_nansen_defi_positions", nothing)
    answer, trajectory = asyncio.run(research._compose_wallet_portfolio("HDixbrzwwLXczhDBk1JVrurPQsuLE8FUKnW2pucSXN3o", None))
    assert "# Wallet portfolio — HDix…N3o" in answer and trajectory["tool_name_0"] == "mobula_wallet_portfolio"
    assert seen["request"].startswith("HDixbrzwwLXczhDBk1JVrurPQsuLE8FUKnW2pucSXN3o wallet portfolio")


def test_the_per_chain_path_is_the_fallback_when_mobula_has_nothing(monkeypatch):
    monkeypatch.setattr(settings, "mobula_api_key", "test-key")
    monkeypatch.setattr(mobula_wallet, "portfolio", lambda request: "# Wallet portfolio\n\nNo priced holdings were returned for this wallet.")

    async def goldrush(wallet, chain):
        return "| ETH | 0.8 |"

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(research, "_bitquery_balances_supplement", goldrush)
    monkeypatch.setattr(research, "_goldrush_hyperliquid_supplement", nothing)
    monkeypatch.setattr(research, "_nansen_defi_positions", nothing)
    answer, trajectory = asyncio.run(research._compose_wallet_portfolio("HDixbrzwwLXczhDBk1JVrurPQsuLE8FUKnW2pucSXN3o", None))
    assert "| ETH | 0.8 |" in answer and trajectory["tool_name_0"] == "goldrush_wallet_balances"


def test_an_address_is_never_an_equity_question(monkeypatch):
    seen = {}

    async def equity(state):
        raise AssertionError("an address must not become an equity brief")

    async def resolve(request, capabilities, user_chains=()):
        seen["request"] = request
        raise RuntimeError("stop here")

    monkeypatch.setattr(research, "_equity_research", equity)
    monkeypatch.setattr(research, "_resolve_named_token", resolve)
    monkeypatch.setattr(research, "_run_token_deep_dive", lambda state, request: _coro(None))
    state = {"request": "deep dive on 0x500CeE0c4184faF3B3d0Cf8c0380B57B2dE44607 on robinhood", "capabilities": ["equity_research"], "chains": ["robinhood"], "session_context": {}}
    try:
        asyncio.run(research._research_node(state, {}))
    except RuntimeError:
        pass
    assert "0x500CeE0c4184faF3B3d0Cf8c0380B57B2dE44607" in seen.get("request", "")


def _coro(value):
    async def run():
        return value
    return run()


def test_a_decoy_pool_never_wins_a_chain_pick(monkeypatch):
    """"ROBINHOOD token price on robinhood chain" resolved to a pool with $1.3M
    listed liquidity and one cent of daily volume."""
    monkeypatch.setattr(research, "bitquery_evm_lookup", lambda ticker, chain: ([], True))
    monkeypatch.setattr(research, "token_candidates", lambda ticker: [
        {"chain": "robinhood", "symbol": "ROBINHOOD", "address": "0xDECOY" + "0" * 36, "liquidity_usd": 1_300_000.0, "volume_24h_usd": 0.01},
        {"chain": "robinhood", "symbol": "ROBINHOOD", "address": "0x500CeE0c4184faF3B3d0Cf8c0380B57B2dE44607", "liquidity_usd": 130_216.0, "volume_24h_usd": 41_000.0},
    ])
    picked = asyncio.run(research._canonical_on_chain("ROBINHOOD", "robinhood"))
    assert picked and picked["address"] == "0x500CeE0c4184faF3B3d0Cf8c0380B57B2dE44607"


def test_a_deep_dive_on_an_address_runs_even_when_classed_as_equity(monkeypatch):
    seen = {}

    async def lens(state, request):
        seen["request"] = request
        return {"answer": "lens", "trajectory": {"tool_name_0": "token_deep_dive"}}

    monkeypatch.setattr(research, "_run_token_deep_dive", lens)
    out = asyncio.run(research._research_node({"request": "deep dive on 0x500CeE0c4184faF3B3d0Cf8c0380B57B2dE44607 on robinhood",
                                               "capabilities": ["equity_research"], "chains": ["robinhood"], "session_context": {}}, {}))
    assert out["answer"] == "lens" and "0x500CeE" in seen["request"]


def test_a_slow_mobula_portfolio_yields_to_the_per_chain_path(monkeypatch):
    """Live: one sheet wallet's Mobula portfolio took over ninety seconds."""
    import time

    monkeypatch.setattr(settings, "mobula_api_key", "test-key")
    monkeypatch.setattr(settings, "mobula_portfolio_timeout_seconds", 0.2)
    monkeypatch.setattr(mobula_wallet, "portfolio", lambda request: time.sleep(1.0) or "# Wallet portfolio\n| late |")

    async def goldrush(wallet, chain):
        return "| SOL | 12.5 |"

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(research, "_bitquery_balances_supplement", goldrush)
    monkeypatch.setattr(research, "_goldrush_hyperliquid_supplement", nothing)
    monkeypatch.setattr(research, "_nansen_defi_positions", nothing)
    answer, trajectory = asyncio.run(research._compose_wallet_portfolio("HDixbrzwwLXczhDBk1JVrurPQsuLE8FUKnW2pucSXN3o", None))
    assert "| SOL | 12.5 |" in answer and trajectory["tool_name_0"] == "goldrush_wallet_balances"


# --- transcript 2026-09-18 22:22: "ANSEM top holders on solana" ------------------

def test_a_bare_symbol_before_a_data_word_is_the_token():
    assert research._bare_symbols("ANSEM top holders on solana") == ["ANSEM"]
    assert research._bare_symbols("what are the largest smart money wallets holding ANSEM") == ["ANSEM"]
    assert research._bare_symbols("USDC price") == [], "market jargon is not a subject"
    assert research._DATA_ASK.search("ANSEM top holders on solana") and not research._DATA_ASK.search("thoughts on ANSEM")


def test_the_resolver_engages_on_a_bare_symbol_holders_ask(monkeypatch):
    seen = {}

    async def canonical(ticker, chain, strict=False):
        seen["ticker"] = ticker
        return {"chain": "solana", "address": "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump", "symbol": ticker, "liquidity_usd": 1.0, "verified": True}

    monkeypatch.setattr(research, "_canonical_on_chain", canonical)
    out = asyncio.run(research._resolve_named_token("ANSEM top holders on solana", {"token_discovery", "market_data"}))
    assert seen["ticker"] == "ANSEM" and out.chain == "solana" and "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump" in out.request


def test_a_whale_ask_that_names_a_bare_symbol_is_not_asked_back(monkeypatch):
    seen = {}

    async def resolve(request, capabilities, user_chains=()):
        seen["request"] = request
        raise RuntimeError("stop here")

    monkeypatch.setattr(research, "_resolve_named_token", resolve)
    state = {"request": "What are the largest known smart money wallets holding ANSEM tokens on Solana?", "capabilities": ["token_holdings"], "chains": ["solana"], "session_context": {}}
    try:
        asyncio.run(research._research_node(state, {}))
    except RuntimeError:
        pass
    assert "ANSEM" in seen.get("request", ""), "the whale intercept must not ask which token when one is named"
