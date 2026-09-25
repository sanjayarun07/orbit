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
    assert [c["id"] for c in anon] == ["trending", "memes", "week", "crypto", "stocks", "macro"]   # Memes chip added 2026-09-18
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


def test_why_moving_news_keeps_sources_but_hides_search_metadata(monkeypatch):
    monkeypatch.setattr(why_moving.perplexity_tools, "perplexity_available", lambda: True)
    monkeypatch.setattr(why_moving.perplexity_tools, "perplexity_search_with_sources", lambda *args, **kwargs: {
        "text": "A report describes the move [1].", "sources": [
            {"n": 1, "title": "Dated report", "url": "https://example.com/report", "date": "2026-09-25"}]})
    news = asyncio.run(why_moving._news("why is Solana (SOL) moving today crypto news"))
    assert "A report describes the move [1]" in news and "[Dated report](https://example.com/report)" in news
    assert "Provider" not in news and "Perplexity" not in news and "**Query**" not in news


def test_why_moving_separates_x_posts_from_market_and_news(monkeypatch):
    async def identity(sym):
        return {"name": "Solana", "symbol": "SOL", "price": 100, "change_24h": 2}
    async def social(*args, **kwargs):
        return {"posts": [{"author": "trader", "id": "123", "created_at": "2026-09-25T12:00:00+00:00",
                           "text": "Rumour about SOL", "url": "https://x.com/trader/status/123"}]}
    monkeypatch.setattr(why_moving, "_crypto_identity", identity)
    monkeypatch.setattr(why_moving, "_crypto_market_detail", lambda _: asyncio.sleep(0, result=None))
    monkeypatch.setattr(why_moving, "_news", lambda _: asyncio.sleep(0, result="A dated news report"))
    monkeypatch.setattr(why_moving.x_news, "enabled", lambda: True)
    monkeypatch.setattr(why_moving.x_news, "search", social)
    answer, trajectory = asyncio.run(why_moving.compose("SOL", "up"))
    assert "X posts (unverified)" in answer and "Rumour about SOL" in answer
    assert trajectory["tool_name_2"] == "x_news_search"


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


# --- 2026-09-17: "today market trend on crypto. why zec is pumping" ----------

def test_why_moving_matches_anywhere_in_the_message_not_only_at_the_start():
    """Anchored to the start, the question at the end of a compound message
    was invisible, and the answer was a market brief that never mentioned ZEC."""
    assert why_moving.match("today market trend on crypto. why zec is pumping") == ("ZEC", "up")
    assert why_moving.match("tell me why BTC dumped") == ("BTC", "down")
    assert why_moving.match("why is zec up today") == ("ZEC", "up")
    # Still not a "why is X moving" ask: no symbol, or the market as a whole.
    assert why_moving.match("why the market is red") is None
    assert why_moving.match("the market is up") is None


def test_a_market_trend_ask_is_a_market_overview_ask():
    for hit in ("today market trend on crypto", "crypto market trends", "what's the trend in the market"):
        assert research_mod._MARKET_OVERVIEW.search(hit), hit


def test_a_compound_ask_gets_the_overview_and_the_asset_card(monkeypatch):
    """Both halves answered: the overview card first, then why ZEC is moving,
    with the trajectory carrying both."""
    async def fake_compose(sym, direction, prefer_stock=False):
        return f"# Why is {sym} {direction}?\ncard", {"tool_name_0": "market_data", "observation_0": "zec data"}

    monkeypatch.setattr(research_mod.why_moving, "compose", fake_compose)
    monkeypatch.setattr(research_mod, "crypto_market_overview", lambda query: "# Crypto market overview\noverview")
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app import composition
    monkeypatch.setattr(research_mod.market_today, "compose", AsyncMock(return_value=(
        "# Crypto market overview\noverview", {"tool_name_0": "crypto_market_overview", "observation_0": "overview"},
    )))
    monkeypatch.setattr(composition.runtime, "_call_synthesis_lm", AsyncMock(return_value=SimpleNamespace(summary="Market flat; ZEC up on its own news.")))
    out = asyncio.run(research_mod.research_node({"request": "today market trend on crypto. why zec is pumping",
                                                  "capabilities": ["web_research"], "chains": [], "history": "", "session_context": {}}))
    # Two sentences, two cards, read together: the summary on top, then the
    # overview, then ZEC -- the message's own order.
    assert out["answer"].startswith("**Taken together**") and "# Crypto market overview" in out["answer"] and "# Why is ZEC up?" in out["answer"]
    assert out["answer"].index("overview") < out["answer"].index("Why is ZEC")
    assert out["trajectory"]["tool_name_0"] == "crypto_market_overview" and out["trajectory"]["tool_name_1"] == "market_data"
    # A plain overview ask now reads the snapshot and dated reporting together.
    monkeypatch.setattr(research_mod.market_today, "compose", AsyncMock(return_value=(
        "# Crypto market today\n\n## Dated reporting\nsource\n\n# Crypto market overview\noverview",
        {"tool_name_0": "crypto_market_overview", "tool_name_1": "perplexity_web_search"},
    )))
    only = asyncio.run(research_mod.research_node({"request": "how's the crypto market today",
                                                   "capabilities": ["web_research"], "chains": [], "history": "", "session_context": {}}))
    assert only["answer"].startswith("# Crypto market today") and "Dated reporting" in only["answer"]
    assert only["trajectory"]["tool_name_1"] == "perplexity_web_search"


def test_a_coin_on_another_chain_is_identified_through_coingecko_not_treated_as_a_stock(monkeypatch):
    """ZEC is not a major in the table and not a Solana token, so it fell to
    the stock path ('Why is ZEC stock pumping?')."""
    async def no_jupiter(query):
        return []

    monkeypatch.setattr(why_moving.jupiter, "search_tokens", no_jupiter)
    calls = []

    def fake_get_json(url):
        calls.append(url)
        if "/search?query=ZEC" in url:
            return {"coins": [{"id": "zcash", "name": "Zcash", "symbol": "zec", "market_cap_rank": 10},
                              {"id": "binance-peg-zcash-token", "name": "Binance-Peg ZEC", "symbol": "zec", "market_cap_rank": None}]}
        if "/search?query=NVDA" in url:
            # CoinGecko's real exact-symbol hits for NVDA: tokenized-stock wrappers, ranked 796 and below.
            return {"coins": [{"id": "nvidia-robinhood-tokenized-stock", "name": "NVIDIA • Robinhood Token", "symbol": "nvda", "market_cap_rank": 796}]}
        if "ids=zcash" in url:
            return {"zcash": {"usd": 412.5, "usd_24h_change": 18.4, "usd_24h_vol": 900_000_000}}
        raise AssertionError(url)

    monkeypatch.setattr(why_moving.market_overview, "_get_json", fake_get_json)
    identity = asyncio.run(why_moving._crypto_identity("ZEC"))
    assert identity["name"] == "Zcash" and identity["coingecko_id"] == "zcash" and identity["price"] == 412.5
    assert identity["change_24h"] == 18.4 and any("/search?query=ZEC" in c for c in calls)
    # A tokenized-stock wrapper is not "the coin": NVDA still goes to the stock path.
    assert asyncio.run(why_moving._crypto_identity("NVDA")) is None
