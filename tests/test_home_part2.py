"""Home chips, $TICKER search and the 'why is X moving' card."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import home_highlights, main, why_moving
from app.nodes import research as research_mod
from tests.conftest import sign_in


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    home_highlights.reset()
    monkeypatch.setattr(home_highlights, "perplexity_available", lambda: True)
    monkeypatch.setattr(home_highlights, "perplexity_invoke", lambda *a: '{"crypto":[{"headline":"BTC tops $120k","summary":"ETF inflows.","source":"coindesk.com"}],"stocks":[{"headline":"Nvidia beats","summary":"Rev $46B.","source":"reuters.com"}]}')
    yield
    home_highlights.reset()


def test_suggestions_have_categories_and_wallet_rows_when_linked(monkeypatch):
    client = TestClient(main.app)
    anon = client.get("/home/suggestions").json()["categories"]
    assert [c["id"] for c in anon] == ["trending", "week", "crypto", "stocks", "macro"]
    assert anon[0]["rows"][0] == "What does this mean for the market: BTC tops $120k"   # same words as the tile, no "?" after a headline
    assert "Why is BTC moving today?" in anon[0]["rows"]
    sign_in(client, email="chips@example.com")
    client.put("/me/preferences", json={"default_wallet": "5CEbueQnq1Ym2uSSx2xXds3jQAqT1BDnkA59RZobSPAG"})

    async def fake_snapshot(wallet):
        return {"holdings": [{"symbol": "BONK", "usd_value": 3000.0}, {"symbol": "JUP", "usd_value": 100.0}, {"symbol": "UNKNOWN", "usd_value": 1.0}]}

    monkeypatch.setattr(main, "build_portfolio_snapshot", fake_snapshot)
    cats = client.get("/home/suggestions").json()["categories"]
    wallet = next(c for c in cats if c["id"] == "wallet")
    assert wallet["rows"][:2] == ["Why is SOL moving today?", "Why is BONK moving today?"]
    assert "What if my portfolio drops 20%?" in wallet["rows"]


def test_token_search_prefers_majors_and_verified(monkeypatch):
    async def fake_search(query):
        return [
            {"symbol": "BONK", "name": "Bonk", "id": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "tags": ["verified"], "organicScore": 80, "usdPrice": 0.0000026},
            {"symbol": "BONKY", "name": "Bonky", "id": "fake1111111111111111111111111111111111111111", "tags": [], "organicScore": 1},
            {"symbol": "BOME", "name": "Book of Meme", "id": "ukHH6c7mMyiWCf1b9pnWe25TSpkDDt3H5pQZgZ74J82", "tags": ["verified"], "organicScore": 60},
        ]

    monkeypatch.setattr(main, "jupiter_search_tokens", fake_search)
    client = TestClient(main.app)
    tokens = client.get("/tokens/search?q=bo").json()["tokens"]
    assert [t["symbol"] for t in tokens] == ["BONK", "BOME", "BONKY"]
    assert tokens[0]["verified"] is True and tokens[0]["mint"].startswith("DezX")
    majors = client.get("/tokens/search?q=s").json()["tokens"]
    assert majors[0] == {"symbol": "SOL", "name": "Solana", "mint": None, "verified": True, "chain": "multi"}
    assert client.get("/tokens/search?q=").json() == {"tokens": []}


def test_why_moving_matches_the_right_shapes():
    assert why_moving.match("why is SOL down today?") == ("SOL", "down")
    assert why_moving.match("Why did $BONK pump?") == ("BONK", "up")
    # Live: "why USELESS token was pumping this week in binance" fell through to a raw DEX table.
    assert why_moving.match("why USELESS token was pumping this week in binanace?") == ("USELESS", "up")
    assert why_moving.match("why has ETH been rallying") == ("ETH", "up") and why_moving.match("why did SOL crash yesterday") == ("SOL", "down")
    assert why_moving.match("why is the market red") is None and why_moving.match("why does ETH keep dumping") == ("ETH", "down")
    assert why_moving.match("why is NVDA stock up") == ("NVDA", "up")
    assert why_moving.match("why is the market red") is None
    assert why_moving.match("what is a liquidity pool") is None


def test_why_moving_card_for_a_token_and_a_stock(monkeypatch):
    async def fake_identity(sym):
        return {"name": "Solana", "symbol": "SOL", "coingecko_id": "solana", "price": 100.5, "change_24h": -4.2, "volume_24h": 2_000_000_000} if sym == "SOL" else None

    async def fake_detail(identity):
        return "# Solana\n- Liquidity: $500M"

    async def fake_news(query):
        assert "Solana (SOL)" in query and "down" in query
        return "SOL fell after a large ETF outflow. Source: coindesk.com"

    monkeypatch.setattr(why_moving, "_crypto_identity", fake_identity)
    monkeypatch.setattr(why_moving, "_crypto_market_detail", fake_detail)
    monkeypatch.setattr(why_moving, "_news", fake_news)
    answer, trajectory = asyncio.run(why_moving.compose("SOL", "down"))
    assert answer.startswith("# Why is Solana (SOL) down?") and "**Price**: $100.5 · **24h**: -4.20%" in answer
    assert "Liquidity: $500M" in answer and "large ETF outflow" in answer
    assert trajectory["tool_name_0"] == "market_data" and trajectory["tool_name_1"] == "perplexity_web_search"

    monkeypatch.setattr(why_moving.perplexity_tools, "perplexity_available", lambda: True)
    monkeypatch.setattr(why_moving.perplexity_tools, "perplexity_finance_search", lambda q: "NVDA rose 5% after earnings beat. Source: reuters.com")
    answer, trajectory = asyncio.run(why_moving.compose("NVDA", "up"))
    assert answer.startswith("# Why is NVDA up?") and "stock (web sources)" in answer and "earnings beat" in answer
    assert trajectory == {"tool_name_0": "perplexity_finance_search", "observation_0": "NVDA rose 5% after earnings beat. Source: reuters.com"}


def test_research_node_intercepts_why_moving(monkeypatch):
    seen = []

    async def fake_compose(sym, direction, prefer_stock=False):
        seen.append((sym, prefer_stock))
        return f"# Why is {sym} {direction}?\ncard", {"tool_name_0": "market_data", "observation_0": "x"}

    monkeypatch.setattr(research_mod.why_moving, "compose", fake_compose)
    out = asyncio.run(research_mod.research_node({"request": "why is BONK down today", "capabilities": ["web_research"], "chains": [], "history": "", "session_context": {}}))
    assert out["answer"].startswith("# Why is BONK down?") and out["trajectory"]["tool_name_0"] == "market_data"
    asyncio.run(research_mod.research_node({"request": "why is NVDA stock up?", "capabilities": ["web_research"], "chains": [], "history": "", "session_context": {}}))
    asyncio.run(research_mod.research_node({"request": "why is NVDA up?", "capabilities": ["equity_research"], "chains": [], "history": "", "session_context": {}}))
    assert seen == [("BONK", False), ("NVDA", True), ("NVDA", True)]


def test_namesake_memecoins_do_not_count_as_the_token(monkeypatch):
    async def fake_search(query):
        return [{"symbol": "NVDA", "name": "NVIDIA", "id": "fake", "tags": ["verified"], "usdPrice": 0.00016, "organicScore": 3, "stats24h": {"buyVolume": 120, "sellVolume": 136}}]

    monkeypatch.setattr(why_moving.jupiter, "search_tokens", fake_search)
    assert asyncio.run(why_moving._crypto_identity("NVDA")) is None
    assert why_moving.prefers_stock("why is NVDA stock up") and not why_moving.prefers_stock("why is SOL down")
