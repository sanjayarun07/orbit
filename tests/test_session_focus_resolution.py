"""Regressions from the e2e sweep: a research turn that resolved a token by
symbol must publish it to the session focus, pronoun follow-ups must accept
"its top holders" / "is it safe", a "<TICKER> is safe" ask must resolve the
token even when the speech layer called it advice, and the LLM-facing wallet
tools must never dump a whale wallet's thousands of accounts into a ReAct loop."""
import asyncio

from app import agent
from app.context_entities import resolve_contextual_request
from app.experience import with_resolved_token
from app.models import ContextCapsule
from app.nodes import research as research_node_mod

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
FOCUS = {"focus": {"kind": "token", "label": "BONK", "address": BONK, "chain": "solana"}}
HISTORY = "user: price of BONK on solana\nassistant: Bonk trades at $0.0000027"


def test_named_token_regex_accepts_ticker_before_copula():
    assert research_node_mod._named_tickers("can you check if BONK is safe to ape into") == ["BONK"]
    assert research_node_mod._named_tickers("PEPE is a scam?") == ["PEPE"]
    # Lower-case pronouns/articles before "is safe" never bind as a ticker.
    assert research_node_mod._named_tickers("it is safe") == []
    assert research_node_mod._named_tickers("this is legit") == []


def test_security_ask_resolves_token_without_token_capabilities(monkeypatch):
    monkeypatch.setattr(research_node_mod, "search_verified_tokens",
                        lambda q: [{"mint": BONK, "symbol": "BONK", "tags": ["verified"]}])
    monkeypatch.setattr(research_node_mod, "token_candidates", lambda symbol, chains=(): [])
    monkeypatch.setattr(research_node_mod, "bitquery_evm_candidates", lambda symbol, chain: [])
    # The speech layer tags "should I ape into X" as advice -> web capabilities only.
    out = asyncio.run(research_node_mod._resolve_named_token(
        "can you check if BONK is safe to ape into", {"finance_data", "web_research"}))
    assert BONK in out.request and out.chain == "solana"
    record = research_node_mod._resolved_token_record("can you check if BONK is safe to ape into", out)
    assert record == {"symbol": "BONK", "address": BONK, "chain": "solana"}
    # A non-security web question with a ticker still stays on the web path.
    out2 = asyncio.run(research_node_mod._resolve_named_token("latest news on $BONK", {"web_research"}))
    assert out2.chain is None and out2.request == "latest news on $BONK"


def test_direct_mcp_request_never_reads_resolved_token_as_wallet():
    request = f"can you check if BONK is safe to ape into {BONK} on solana"
    assert research_node_mod._detect_wallet_request(request) is not None  # the raw shape looks like a wallet check
    direct = research_node_mod.direct_mcp_request(request, token_subject=True)
    assert direct is None or "wallet" not in direct[0] and "address_portfolio" not in direct[0]


def test_with_resolved_token_sets_focus_capsule():
    resolved = {"symbol": "BONK", "address": BONK, "chain": "solana"}
    capsules = with_resolved_token([], resolved)
    assert [c.model_dump() for c in capsules] == [{
        "kind": "token", "label": "BONK", "address": BONK, "chain": "solana",
        "confidence": 0.95, "source": "token_resolver",
    }]
    # An addressed token capsule the turn already produced wins.
    existing = [ContextCapsule(kind="token", label="Token", address="9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump", chain="solana")]
    assert with_resolved_token(existing, resolved) == existing
    # A label-only capsule for the same symbol is replaced by the addressed one.
    label_only = [ContextCapsule(kind="token", label="BONK", chain="solana", confidence=0.8)]
    assert with_resolved_token(label_only, resolved)[0].address == BONK
    assert with_resolved_token([], None) == []


def test_pronoun_follow_ups_bind_to_token_focus():
    for q in ["who are its top holders?", "what's its liquidity", "is it safe?", "is that one legit?"]:
        resolved = resolve_contextual_request(q, HISTORY, FOCUS)
        assert BONK in resolved and "on solana" in resolved, q
    # Unrelated follow-ups are left untouched.
    assert resolve_contextual_request("what time is it", HISTORY, FOCUS) == "what time is it"


def test_spl_balances_tool_is_bounded_for_llm(monkeypatch):
    accounts = {"value": [
        {"account": {"data": {"parsed": {"info": {"mint": f"Mint{i:040d}", "tokenAmount": {"uiAmount": float(i)}}}}}}
        for i in range(0, 300)
    ]}

    async def fake_accounts(wallet):
        return accounts

    monkeypatch.setattr(agent, "get_token_accounts", fake_accounts)
    out = agent.spl_balances("wallet")
    assert out["token_account_count"] == 299  # the zero-amount account is dropped
    assert out["truncated"] is True
    assert len(out["holdings"]) == agent.LLM_MAX_HOLDINGS
    assert out["holdings"][0]["amount"] == 299.0  # largest first


def test_portfolio_snapshot_tool_caps_listed_holdings(monkeypatch):
    holdings = [{"mint": f"M{i}", "symbol": f"T{i}", "amount": 1.0, "usd_value": float(1000 - i)} for i in range(120)]

    async def fake_snapshot(wallet):
        return {"wallet": wallet, "holdings": holdings, "total_usd_value": 1.0}

    monkeypatch.setattr(agent, "build_portfolio_snapshot", fake_snapshot)
    out = agent.portfolio_snapshot("wallet")
    assert len(out["holdings"]) == agent.LLM_MAX_HOLDINGS
    assert out["holdings_omitted"] == 120 - agent.LLM_MAX_HOLDINGS
    assert out["total_usd_value"] == 1.0


def test_solana_rpc_top_holders_is_keyless_fallback_behind_bitquery(monkeypatch):
    from app import provider_registry

    monkeypatch.setattr(provider_registry, "token_top_holders_rpc", lambda mint: {
        "mint": mint, "total_supply": 1_000_000.0, "decimals": 5, "top10_supply_pct": 60.0,
        "holders": [
            {"token_account": "5hpfAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwNiw", "owner": "9WzDAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWWM", "amount": 400_000.0, "supply_pct": 40.0},
            {"token_account": "2vPvAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAnEiw", "owner": None, "amount": 200_000.0, "supply_pct": 20.0},
        ],
    })
    request = f"who are its top holders?\nResolved from canonical session context: token {BONK} on solana."
    out = provider_registry._solana_rpc_top_holders(request)
    assert "getTokenLargestAccounts" in out and "40.00%" in out and "400,000" in out
    assert "solscan.io/account/9WzD" in out and "| 2 | — |" in out
    router = provider_registry.get_provider_router()
    ranked = [t.name for t in router._ranked_union(request, ("token_discovery", "token_security"), ("solana",), None)]
    assert ranked.index("bitquery_token_top_holders") < ranked.index("solana_rpc_token_top_holders")
    assert ranked.index("solana_rpc_token_top_holders") < ranked.index("dexscreener_token_pairs")
    # No holders wording -> the matcher stays quiet (never steals a price question).
    assert "solana_rpc_token_top_holders" not in [
        t.name for t in router._ranked_union(f"price of {BONK} on solana", ("token_discovery", "market_data"), ("solana",), None)
    ]
