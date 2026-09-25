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
    monkeypatch.setattr(research, "_solana_own_snapshot", nothing)
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
    monkeypatch.setattr(research, "_solana_own_snapshot", nothing)
    monkeypatch.setattr(research, "_goldrush_hyperliquid_supplement", nothing)
    monkeypatch.setattr(research, "_nansen_defi_positions", nothing)
    answer, trajectory = asyncio.run(research._compose_wallet_portfolio("HDixbrzwwLXczhDBk1JVrurPQsuLE8FUKnW2pucSXN3o", None))
    assert "| SOL | 12.5 |" in answer and trajectory["tool_name_0"] == "goldrush_wallet_balances"


def test_a_rejected_solana_wallet_is_read_from_the_chain_before_goldrush(monkeypatch):
    """Live 2026-09-21: Mobula answered 400 "Unsupported wallet" and GoldRush
    priced none of 1,548 balances; the chain plus Jupiter priced it."""
    monkeypatch.setattr(settings, "mobula_api_key", "test-key")
    monkeypatch.setattr(mobula_wallet, "portfolio", lambda request: (_ for _ in ()).throw(RuntimeError("400 Unsupported wallet")))

    async def never(*a, **k):
        raise AssertionError("GoldRush must not run when the chain read priced the wallet")

    async def own(wallet):
        return "# Wallet token balances\n**Provider**: Orbit (Solana RPC balances, Jupiter prices)\n| USDC | 15,390.7847 | $1.00 | $15.4K | 2.2% |"

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(research, "_bitquery_balances_supplement", never)
    monkeypatch.setattr(research, "_solana_own_snapshot", own)
    monkeypatch.setattr(research, "_goldrush_hyperliquid_supplement", nothing)
    monkeypatch.setattr(research, "_nansen_defi_positions", nothing)
    answer, trajectory = asyncio.run(research._compose_wallet_portfolio("HDixbrzwwLXczhDBk1JVrurPQsuLE8FUKnW2pucSXN3o", None))
    assert "| USDC | 15,390.7847 |" in answer and trajectory["tool_name_0"] == "solana_rpc_portfolio"


def test_render_card_prices_first_and_refuses_an_unpriced_snapshot():
    from app.portfolio import render_card

    snapshot = {"sol": {"amount": 47.734, "usd_price": 140.0, "usd_value": 6682.76, "allocation_pct": 30.2},
                "holdings": [{"mint": "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump", "symbol": "ANSEM", "amount": 3754834.1193, "usd_price": 0.0041, "usd_value": 15394.8, "verified": True, "allocation_pct": 69.8},
                             {"mint": "A8WG" + "x" * 36 + "W61g", "symbol": "UNKNOWN", "amount": 50119234.2, "usd_price": None, "usd_value": None, "verified": False, "allocation_pct": None}],
                "total_usd_value": 22077.56, "unpriced_holdings": 1, "partial": False}
    card = render_card(snapshot)
    assert "| SOL | 47.7340 | $140.00 | $6.7K | 30.2% |" in card and "| ANSEM | 3,754,834.1193 |" in card
    assert "2 priced, 1 without a Jupiter price" in card and "not in the total" in card
    assert "$0.0000" not in card and "| $0.00 |" not in card
    from app.portfolio import _usd
    assert _usd(0.00001122) == "$0.0000112" and _usd(0.0041) == "$0.0041" and _usd(19084.8) == "$19.1K"
    assert render_card({"sol": {"amount": 1.0, "usd_value": None}, "holdings": [{"mint": "m", "usd_value": None}], "total_usd_value": None}) is None


# --- transcript 2026-09-18 22:22: "ANSEM top holders on solana" ------------------

def test_a_bare_symbol_before_a_data_word_is_the_token():
    assert research._bare_symbols("ANSEM top holders on solana") == ["ANSEM"]
    assert research._bare_symbols("what are the largest smart money wallets holding ANSEM") == ["ANSEM"]
    assert research._bare_symbols("USDC price") == [], "market jargon is not a subject"
    assert research._bare_symbols("BTC options at 08:00 UTC") == ["BTC"], "a time zone is not the last-mentioned token"
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


# --- review 2026-09-20: a bare EVM contract never defaults to Ethereum ------------

def test_a_bare_evm_contract_is_placed_on_its_chain_or_asked_about(monkeypatch):
    from app.nodes import research as r

    async def no_token(request, capabilities, user_chains=()):
        return r._TokenResolution(request)

    monkeypatch.setattr(r, "_resolve_named_token", no_token)
    monkeypatch.setattr(r, "direct_mcp_request", lambda request, token_subject=False: None)
    monkeypatch.setattr(r, "is_crypto_trends_query", lambda request: False)
    seen = {}

    async def gathered(request, capabilities, chains, state, scope_web=False):
        seen["request"], seen["chains"] = request, tuple(chains)
        return {"answer": "# Token holders", "trajectory": {"tool_name_0": "mobula_token_holders"}}

    monkeypatch.setattr(r, "_gather_planned", gathered)
    monkeypatch.setattr(r, "_router_eligible_capabilities", lambda request, caps, chains: ("token_holdings",))
    monkeypatch.setattr(r, "get_provider_router", lambda: __import__("types").SimpleNamespace(matched_capabilities=lambda *a: (), try_route_across=lambda *a, **k: None, plan_across=lambda *a, **k: []))
    aero = "0x940181a94A35A4569E4529A3CDfB74e38FD98631"
    monkeypatch.setattr(r, "_chains_for_contract", lambda address: ["base"])
    out = asyncio.run(r._research_node({"request": f"top holders of {aero}", "capabilities": ["token_holdings"], "chains": [], "session_context": {}}, {}))
    assert out["answer"] == "# Token holders" and seen["chains"] == ("base",) and seen["request"].endswith("on base"), "one chain known: placed there"

    monkeypatch.setattr(r, "_chains_for_contract", lambda address: ["base", "ethereum"])
    out = asyncio.run(r._research_node({"request": f"top holders of {aero}", "capabilities": ["token_holdings"], "chains": [], "session_context": {}}, {}))
    assert out["answer"].startswith(f"Which chain is `{aero}` on (base, ethereum)?"), "several chains: asked, never Ethereum by default"

    monkeypatch.setattr(r, "_chains_for_contract", lambda address: [])
    out = asyncio.run(r._research_node({"request": f"is {aero} safe", "capabilities": ["token_security"], "chains": [], "session_context": {}}, {}))
    assert out["answer"].startswith(f"Which chain is `{aero}` on (Ethereum, Base")


def test_the_chain_lookup_reads_dex_screener_and_drops_decoys(monkeypatch):
    """The first version swallowed a NameError (httpx was never imported) and
    returned nothing for every contract; a stubbed response pins the parsing."""
    from app.nodes import research as r

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"pairs": [
                {"chainId": "base", "baseToken": {"address": "0x940181a94A35A4569E4529A3CDfB74e38FD98631"}, "liquidity": {"usd": 34_240_468.0}, "volume": {"h24": 2_747_794.0}},
                {"chainId": "ethereum", "baseToken": {"address": "0x940181a94A35A4569E4529A3CDfB74e38FD98631"}, "liquidity": {"usd": 5_000_000.0}, "volume": {"h24": 12.0}},   # decoy
                {"chainId": "bsc", "baseToken": {"address": "0xSOMETHINGELSE"}, "liquidity": {"usd": 9_000_000.0}, "volume": {"h24": 900_000.0}},                            # another token
                {"chainId": "arbitrum", "baseToken": {"address": "0x940181a94A35A4569E4529A3CDfB74e38FD98631"}, "liquidity": {"usd": 400.0}, "volume": {"h24": 40.0}},        # dust
            ]}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            assert url.endswith("/latest/dex/tokens/0x940181a94A35A4569E4529A3CDfB74e38FD98631")
            return Response()

    monkeypatch.setattr(r.httpx, "Client", Client)
    assert r._chains_for_contract("0x940181a94A35A4569E4529A3CDfB74e38FD98631") == ["base"]
