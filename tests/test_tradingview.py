"""TradingView on the user's own account, and the chart under an answer.

The user asked (2026-09-18) for TradingView's crypto-and-equity data and
charts "combined with research". Charts come from TradingView's embeddable
widget (no account); data comes from TradingView's MCP server on the user's
own OAuth-linked account, bound to their turn and invisible to everyone else.
"""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import charts, execution_policy, main, sessions
from app.graph import AgentRun
from app.integrations import tradingview as tv
from app.settings import settings
from tests.conftest import sign_in


# --- charts --------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_cache():
    charts._cache.clear()
    tv._clients.clear(); tv._tokens.clear(); tv._pending.clear(); tv._metadata = None
    yield
    charts._cache.clear()


def test_a_coin_charts_its_most_liquid_usd_pair_and_falls_back_to_the_aggregate_index(monkeypatch):
    monkeypatch.setattr(charts, "_search", lambda text, kind: [
        {"symbol": "SOLUSD", "type": "spot", "exchange": "KRAKEN"}, {"symbol": "SOLUSDT", "type": "spot", "exchange": "BINANCE"},
        {"symbol": "SOL", "type": "index", "exchange": "CRYPTOCAP"}, {"symbol": "SOLBTC", "type": "spot", "exchange": "BINANCE"}])
    assert charts.crypto_symbol("sol") == {"symbol": "BINANCE:SOLUSDT", "label": "SOL / USDT · Binance"}

    def down(text, kind):
        raise RuntimeError("offline")

    charts._cache.clear()
    monkeypatch.setattr(charts, "_search", down)
    assert charts.crypto_symbol("BONK") == {"symbol": "CRYPTO:BONKUSD", "label": "BONK / USD"}


def test_a_stock_charts_its_primary_listing(monkeypatch):
    monkeypatch.setattr(charts, "_search", lambda text, kind: [{"symbol": "AAPL", "type": "stock", "exchange": "NASDAQ", "description": "Apple Inc."}, {"symbol": "AAPL", "type": "stock", "exchange": "BMV"}])
    assert charts.equity_symbol("$aapl") == {"symbol": "NASDAQ:AAPL", "label": "Apple Inc. · NASDAQ"}
    assert charts.equity_ticker("is NVDA a good buy") == "NVDA" and charts.equity_ticker("what is an ETF") is None


def test_the_chart_follows_the_resolved_token_or_the_equity_ticker(monkeypatch):
    monkeypatch.setattr(charts, "_search", lambda text, kind: [])
    crypto = AgentRun(answer="", trajectory=None, trade_plan=None, intent="research", capabilities=["market_data"], resolved_token={"symbol": "BONK", "address": "x", "chain": "solana"})
    assert charts.chart_for(crypto, "price of BONK") == {"symbol": "CRYPTO:BONKUSD", "label": "BONK / USD", "interval": "60", "kind": "crypto"}
    equity = AgentRun(answer="", trajectory=None, trade_plan=None, intent="research", capabilities=["equity_research"])
    assert charts.chart_for(equity, "research MSFT stock")["symbol"] == "MSFT"
    assert charts.chart_for(AgentRun(answer="", trajectory=None, trade_plan=None, intent="general", capabilities=[]), "hi") is None


def test_the_chat_response_and_the_stored_turn_carry_the_chart(monkeypatch):
    monkeypatch.setattr(sessions, "get_redis", AsyncMock(return_value=None))
    monkeypatch.setattr(execution_policy, "allow_chat_request", AsyncMock(return_value=(True, 0)))
    monkeypatch.setattr(execution_policy, "allow_chat_request_from_ip", AsyncMock(return_value=(True, 0)))
    monkeypatch.setattr(execution_policy.tool_outcomes, "record_turn", AsyncMock())
    monkeypatch.setattr(execution_policy.research_gaps, "record", AsyncMock())
    monkeypatch.setattr(execution_policy.notifications, "maybe_low_credit_alert", AsyncMock())
    monkeypatch.setattr(charts, "_search", lambda text, kind: [{"symbol": "SOLUSDT", "type": "spot", "exchange": "BINANCE"}])
    monkeypatch.setattr(execution_policy, "run_agent", AsyncMock(return_value=AgentRun(
        answer="SOL is $100", trajectory={"tool_name_0": "birdeye_token_overview", "observation_0": "x"}, trade_plan=None, intent="research",
        capabilities=["market_data"], resolved_token={"symbol": "SOL", "address": "So1", "chain": "solana"})))
    client = TestClient(main.app)
    sign_in(client, email="chart-history@example.com")
    r = client.post("/chat", json={"message": "price of SOL"}, headers={"X-Orbit-Device": "chart-test"})
    assert r.status_code == 200 and r.json()["chart"] == {"symbol": "BINANCE:SOLUSDT", "label": "SOL / USDT · Binance", "interval": "60", "kind": "crypto"}
    h = client.get(f"/chat/history/{r.json()['session_id']}", headers={"X-Orbit-Device": "chart-test"})
    assert h.status_code == 200, h.text
    assert h.json()["messages"][-1]["chart"]["symbol"] == "BINANCE:SOLUSDT", "the chart is drawn again when the conversation is reopened"


def test_the_browser_draws_the_widget_in_the_pages_theme_with_attribution():
    from tests.test_ui_swap_flow import run_case
    r = run_case("chart_card_embeds_the_resolved_symbol_with_attribution")
    assert r["className"] == "chart-card" and r["none"] is None
    assert r["src"].startswith("https://s.tradingview.com/widgetembed/?") and "symbol=BINANCE%3ASOLUSDT" in r["src"] and "theme=light" in r["src"] and "interval=60" in r["src"]
    assert "Chart by TradingView" in r["caption"] and "tradingview.com/symbols/BINANCE-SOLUSDT" in r["caption"]


# --- the OAuth connector ----------------------------------------------------------

META = {"issuer": "https://www.tradingview.com", "authorization_endpoint": "https://www.tradingview.com/mcp/oauth/authorize",
        "token_endpoint": "https://www.tradingview.com/mcp/oauth/token", "registration_endpoint": "https://www.tradingview.com/mcp/oauth/register",
        "revocation_endpoint": "https://www.tradingview.com/mcp/oauth/revoke", "code_challenge_methods_supported": ["S256"]}


def _stub_server(monkeypatch, *, token_answers=None):
    calls = {"get": [], "json": [], "form": []}

    async def get_json(url):
        calls["get"].append(url)
        if url.endswith("/.well-known/oauth-protected-resource/mcp"):
            return {"resource": settings.tradingview_mcp_url, "authorization_servers": ["https://www.tradingview.com"]}
        return META

    async def post_json(url, payload):
        calls["json"].append((url, payload))
        return {"client_id": "orbit-client-1", "redirect_uris": payload["redirect_uris"]}

    async def post_form(url, data):
        calls["form"].append((url, data))
        if url.endswith("/revoke"):
            return {}
        return (token_answers or (lambda d: {"access_token": "at-" + d["grant_type"], "refresh_token": "rt-1", "expires_in": 3600, "scope": tv.SCOPE}))(data)

    monkeypatch.setattr(tv, "_get_json", get_json)
    monkeypatch.setattr(tv, "_post_json", post_json)
    monkeypatch.setattr(tv, "_post_form", post_form)
    return calls


def test_the_authorization_url_carries_pkce_state_and_the_mcp_resource(monkeypatch):
    calls = _stub_server(monkeypatch)
    url = asyncio.run(tv.begin("user-1"))
    assert url.startswith(META["authorization_endpoint"] + "?")
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["orbit-client-1"] and q["code_challenge_method"] == ["S256"] and q["resource"] == [settings.tradingview_mcp_url]
    assert q["redirect_uri"] == [tv.redirect_uri()] and q["scope"] == [tv.SCOPE] and len(q["state"][0]) > 20
    registration = calls["json"][0][1]
    assert registration["client_name"] == "Orbit" and registration["token_endpoint_auth_method"] == "none" and registration["redirect_uris"] == [tv.redirect_uri()]
    assert len(calls["json"]) == 1, "registered once"
    asyncio.run(tv.begin("user-2"))
    assert len(calls["json"]) == 1, "the registered client is reused"


def test_the_callback_exchanges_the_code_with_the_verifier_for_the_user_who_started(monkeypatch):
    calls = _stub_server(monkeypatch)
    from urllib.parse import parse_qs, urlparse
    url = asyncio.run(tv.begin("user-1"))
    state = parse_qs(urlparse(url).query)["state"][0]
    assert asyncio.run(tv.complete(state, "code-xyz")) == "user-1"
    exchange = calls["form"][-1][1]
    assert exchange["grant_type"] == "authorization_code" and exchange["code"] == "code-xyz" and exchange["code_verifier"] and exchange["resource"] == settings.tradingview_mcp_url
    assert asyncio.run(tv.status("user-1"))["connected"] is True
    assert asyncio.run(tv.access_token("user-1")) == "at-authorization_code"
    with pytest.raises(tv.TradingViewError):
        asyncio.run(tv.complete(state, "code-xyz"))          # a state is single-use
    with pytest.raises(tv.TradingViewError):
        asyncio.run(tv.complete("forged", "code-xyz"))


def test_an_expiring_token_is_refreshed_and_a_failed_refresh_drops_the_connection(monkeypatch):
    calls = _stub_server(monkeypatch)
    tv._tokens["user-1"] = {"user_id": "user-1", "access_token": "old", "refresh_token": "rt-1", "expires_at": time.time() + 10, "scope": tv.SCOPE, "connected_at": "2026-09-18T00:00:00+00:00"}
    assert asyncio.run(tv.access_token("user-1")) == "at-refresh_token"
    assert calls["form"][-1][1]["grant_type"] == "refresh_token"

    async def refuse(url, data):
        raise tv.TradingViewError("invalid_grant")

    monkeypatch.setattr(tv, "_post_form", refuse)
    tv._tokens["user-1"]["expires_at"] = time.time() + 10
    assert asyncio.run(tv.access_token("user-1")) is None
    assert asyncio.run(tv.status("user-1"))["connected"] is False, "the user is asked to connect again rather than served stale data"


def test_disconnect_revokes_and_forgets(monkeypatch):
    calls = _stub_server(monkeypatch)
    tv._tokens["user-1"] = {"user_id": "user-1", "access_token": "at", "refresh_token": "rt", "expires_at": None, "scope": tv.SCOPE, "connected_at": "2026-09-18T00:00:00+00:00"}
    assert asyncio.run(tv.disconnect("user-1")) is True
    assert {d["token"] for u, d in calls["form"] if u.endswith("/revoke")} == {"at", "rt"}
    assert asyncio.run(tv.status("user-1"))["connected"] is False and asyncio.run(tv.disconnect("user-1")) is False


# --- the routes -------------------------------------------------------------------

def test_connect_redirects_a_signed_in_browser_and_refuses_everyone_else(monkeypatch):
    _stub_server(monkeypatch)
    client = TestClient(main.app)
    assert client.get("/integrations/tradingview/connect", follow_redirects=False).status_code == 401
    sign_in(client, email="tv-connect@example.com")
    r = client.get("/integrations/tradingview/connect", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(META["authorization_endpoint"])
    me = client.get("/me").json()
    assert me["integrations"]["tradingview"] == {"connected": False, "connected_at": None, "enabled": True}


def test_the_callback_finishes_the_flow_and_the_account_shows_it(monkeypatch):
    _stub_server(monkeypatch)
    client = TestClient(main.app)
    sign_in(client, email="tv-callback@example.com")
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(client.get("/integrations/tradingview/connect", follow_redirects=False).headers["location"]).query)["state"][0]
    r = client.get("/integrations/tradingview/callback", params={"state": state, "code": "c1"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/ui/?connected=tradingview"
    assert client.get("/me").json()["integrations"]["tradingview"]["connected"] is True
    bad = client.get("/integrations/tradingview/callback", params={"state": "nope", "code": "c1"}, follow_redirects=False)
    assert bad.status_code == 302 and "connect_error=" in bad.headers["location"]
    assert client.delete("/integrations/tradingview").json() == {"connected": False, "removed": True}
    assert client.get("/me").json()["integrations"]["tradingview"]["connected"] is False


# --- the turn: tools only for a connected user ------------------------------------

def test_the_tradingview_tools_match_only_while_a_token_is_bound_to_the_turn():
    assert not tv.matches_market("AAPL price and technicals"), "no connection: invisible"
    token = tv.current_token.set("at-1")
    try:
        assert tv.matches_market("AAPL price and technicals") and tv.matches_equity("NVDA fundamentals") and tv.matches_news("latest news on TSLA") and tv.matches_earnings("when does MSFT report earnings")
        assert not tv.matches_market("what is an ETF"), "no ticker, no match"
    finally:
        tv.current_token.reset(token)


def test_a_snapshot_card_is_built_from_the_documented_tools(monkeypatch):
    seen = []

    def fake_call(name, arguments):
        seen.append((name, arguments))
        return {"search_symbols": json.dumps({"symbols": [{"symbol": "AAPL", "exchange": "NASDAQ"}]}),
                "get_symbol_data": "close: 230.1\\nchange: 1.2%", "get_technicals_rating": "Recommend.All: buy"}[name]

    monkeypatch.setattr(tv, "call_tool_sync", fake_call)
    token = tv.current_token.set("at-1")
    try:
        card = tv.snapshot("AAPL price and technicals")
    finally:
        tv.current_token.reset(token)
    assert card.startswith("# TradingView snapshot") and "NASDAQ:AAPL" in card and "close: 230.1" in card and "Recommend.All: buy" in card
    assert [n for n, _ in seen] == ["search_symbols", "get_symbol_data", "get_technicals_rating"]
    assert seen[1][1]["symbol"] == "NASDAQ:AAPL"


def test_the_turn_binds_the_users_token_and_unbinds_it_after(monkeypatch):
    tv._tokens["u-bound"] = {"user_id": "u-bound", "access_token": "at-bound", "refresh_token": None, "expires_at": None, "scope": tv.SCOPE, "connected_at": "2026-09-18T00:00:00+00:00"}

    async def run():
        assert tv.current_token.get() is None
        bound = await tv.bind_turn("u-bound")
        try:
            assert tv.current_token.get() == "at-bound" and tv.available()
        finally:
            tv.current_token.reset(bound)
        assert tv.current_token.get() is None
        assert (await tv.bind_turn("nobody")) is not None and tv.current_token.get() is None

    asyncio.run(run())


def test_the_router_lists_the_tradingview_tools_but_only_a_bound_turn_can_reach_them():
    from app.provider_registry import get_provider_router
    router = get_provider_router()
    names = {t.name for t in router._tools}
    assert {"tradingview_snapshot", "tradingview_financials", "tradingview_news", "tradingview_earnings"} <= names
    assert "tradingview_snapshot" not in {t.name for t in router.candidates("AAPL price and technicals", "market_data", ())}
    token = tv.current_token.set("at-1")
    try:
        assert "tradingview_snapshot" in {t.name for t in router.candidates("AAPL price and technicals", "market_data", ())}
    finally:
        tv.current_token.reset(token)
