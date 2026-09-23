"""The Home Explore and news review of 2026-09-23: exact figures and dates
in headlines with short taps, burn addresses out of holder classes, single-
asset yields first and no yield table on a Fed story, an honest scope on
Base gainers, a concentration line on the balances card, wallet health that
shows what it checked, research-mode trade starts, and freshness for
"recently"."""
import asyncio

import pytest

from app import additional_providers, home_highlights, mobula_meme, portfolio, wallet_insights
from app.routing import resolver

ANSEM = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


def test_news_cards_carry_a_date_and_ask_for_a_short_exact_answer():
    assert "Copy every number exactly as the source prints it" in home_highlights._NEWS_INSTRUCTIONS and '"date":""' in home_highlights._NEWS_INSTRUCTIONS
    from app import composition
    assert composition.word_limit("What does this mean for the market: Nasdaq closes at record") == 150 and composition.word_limit("price of BONK") is None
    parsed = home_highlights._parse_news('{"crypto":[{"headline":"Bitcoin ETFs draw $998.95M","summary":"s","source":"decrypt.co","date":"2026-09-21"},{"headline":"x","summary":"y","source":"z"}],"stocks":[],"memes":[]}')
    assert parsed["crypto"][0]["date"] == "2026-09-21" and "date" not in parsed["crypto"][1]


def test_burn_addresses_are_not_holders_or_pools(monkeypatch):
    rows = [{"walletAddress": "0x000000000000000000000000000000000000dEaD", "percentageOfTotalSupply": 41.0, "tokenAmountUSD": 1.0, "labels": ["liquidityPool"]},
            {"walletAddress": "0x1111111111111111111111111111111111111111", "percentageOfTotalSupply": 3.0, "tokenAmountUSD": 1.0, "labels": ["proTrader"]}]
    monkeypatch.setattr(mobula_meme, "_get", lambda path, params: rows)
    monkeypatch.setattr(mobula_meme, "_subject", lambda request: ("0x6982508145454Ce325dDbE47a25d4ec3d2311933", "ethereum"))
    card = mobula_meme.token_holders("top holders of PEPE")
    assert "burn / null address (tokens out of circulation; not a holder, not a pool)" in card and "liquidityPool" not in card
    assert "of which 41.00% sits in burn/null addresses and is out of circulation" in card
    assert mobula_meme.is_burn_address("1nc1nerator11111111111111111111111111111111") and not mobula_meme.is_burn_address("0x1111111111111111111111111111111111111111")


def test_best_stable_yield_ranks_single_asset_first_and_a_fed_story_gets_no_yield_table(monkeypatch):
    pools = [{"symbol": "USDC-WETH", "project": "aerodrome", "chain": "Base", "apy": 414.0, "tvlUsd": 3e7, "exposure": "multi", "ilRisk": "yes"},
             {"symbol": "USDC", "project": "aave-v3", "chain": "Base", "apy": 4.1, "tvlUsd": 5e7, "exposure": "single", "stablecoin": True}]
    monkeypatch.setattr(additional_providers, "_llama_pools", lambda: pools)
    card = additional_providers.DefiLlamaProvider().yields("Best USDC yield on Base")
    assert card.index("| USDC | aave-v3") < card.index("| USDC-WETH | aerodrome") and "Single-asset pools first" in card and "| single-asset |" in card and "| LP (multi-asset) |" in card
    card = additional_providers.DefiLlamaProvider().yields("USDC LP farms on Base")
    assert card.index("| USDC-WETH") < card.index("| USDC |")
    from app.provider_registry import get_provider_router
    tool = next((t for t in get_provider_router().tools() if t.name == "defillama_yields"), None)
    if tool is not None:
        assert not tool.matches("What does this mean for the market: Fed rate at 3.75%-4.00% keeps markets focused on yields")
        assert tool.matches("best USDC yield on Base")


def test_base_gainers_say_where_their_numbers_come_from():
    import inspect
    src = inspect.getsource(additional_providers)
    assert "ecosystem category (tokens associated with" in src and "not trades on a" in src and '**Basis**: {basis}' in src


def test_the_balances_card_states_concentration_and_wallet_health_shows_its_checks():
    snap = {"wallet": "w", "sol": {"amount": 0.07, "usd_price": 118.0, "usd_value": 8.66, "allocation_pct": 81.3},
            "holdings": [{"mint": ANSEM, "symbol": "ANSEM", "amount": 11.5, "usd_price": 0.17, "usd_value": 1.99, "allocation_pct": 18.7, "verified": True}],
            "total_usd_value": 10.65, "unpriced_holdings": 0, "unread_programs": []}
    card = portfolio.render_card(snap)
    assert "Concentration: largest holding SOL at 81.3% of priced value; top two 100.0%; 2 priced asset(s)." in card and "not a verdict" in card
    report = wallet_insights.wallet_health(snap)
    assert report["checked"][0].startswith("Gas: 0.0700 SOL available") and report["checked"][1].startswith("Coverage: 1 SPL holding(s) read across both token programs, 1 priced")
    report = wallet_insights.wallet_health({**snap, "unread_programs": ["spl-token-2022"]})
    assert any(f["title"] == "Partial read" for f in report["findings"])


def test_a_trade_start_in_research_mode_says_so(monkeypatch):
    from app import deployment
    monkeypatch.setattr(deployment, "execution_enabled", lambda config=None: False)

    async def never(*a, **k):
        raise AssertionError("no model")
    from types import SimpleNamespace

    async def abstain(*a, **k):
        return SimpleNamespace(understanding={"speech_act": "abstain", "domain": "crypto", "explicit_action": False, "confidence": 0.3})
    out = asyncio.run(resolver.resolve({"request": "Start a cross-chain swap", "contextual_request": None, "session_context": {}}, abstain))
    assert out.get("clarification") and "research mode" in out["clarification"] and "what would happen if I sold" in out["clarification"]
    assert out["routing_decision"]["reason"] == "research_mode"
    # a routed trade keeps its route: the plan path refuses it with the same words
    out = asyncio.run(resolver.resolve({"request": "Swap 0.01 SOL to USDC on Solana with 50 bps slippage", "contextual_request": None, "session_context": {}}, abstain))
    assert out["intent"] == "trade"


def test_public_config_names_the_deployment_mode():
    from fastapi.testclient import TestClient
    from app.main import app
    cfg = TestClient(app).get("/config/public").json()
    assert cfg["deployment_mode"] in ("research", "execution") and isinstance(cfg["execution_enabled"], bool)


def test_a_recently_question_leads_with_the_dated_web_answer(monkeypatch):
    from app.nodes import research

    async def kb(request, passages, history, stream=True):
        return "The BORG foundation vote [1].\n\n## Sources\n[1] lido docs"
    async def web(request):
        assert "most recent events first" in request
        return "2026-09-21: Snapshot vote on expense optimization and stETH buyback parameters began."
    monkeypatch.setattr(research, "_knowledge_answer", kb)
    monkeypatch.setattr(research, "_knowledge_from_the_web", web)
    out = asyncio.run(research._synthesize_knowledge("What did the Lido community vote on recently?", "[1] …", "", stream=False))
    assert out.startswith("**Latest, from the web (dated)**\n\n2026-09-21: Snapshot vote") and "**Background from the knowledge base**" in out and "BORG" in out
    out = asyncio.run(research._synthesize_knowledge("How does Lido's stETH work?", "[1] …", "", stream=False))
    assert out.startswith("The BORG foundation vote")
