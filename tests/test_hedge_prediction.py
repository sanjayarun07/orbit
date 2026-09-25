"""Prediction tool contract: scope, defaults, polling and secret handling."""

from unittest.mock import Mock
import asyncio
import json

import pytest

from app import hedge_prediction as hedge, prediction_route as route


@pytest.mark.parametrize(("prompt", "expected"), [
    ("Predict SOL", {"symbol": "SOL"}),
    ("Forecast BTCUSDT short for 24h", {"symbol": "BTC", "side": "short", "horizons": ["24h"]}),
    ("Ethereum futures outlook", {"symbol": "ETH"}),
    ("SOL price now", None),
    ("Predict this Solana mint 0x91A2DAe9699f0B82540B5886b0d8759C22820bA3", None),
    ("What are the Polymarket prediction market odds for BTC?", None),
    ("Predict BTC and ETH", None),
    ("Will BTC rise tomorrow?", {"symbol": "BTC"}),
])
def test_explicit_futures_scope(prompt, expected):
    assert hedge.parse(prompt) == expected


def test_poll_url_cannot_redirect_the_key():
    base = "https://hedge.openledger.dev/api"
    assert hedge._poll_url("/api/v1/predict/123", base) == "https://hedge.openledger.dev/api/v1/predict/123"
    for url in ("https://evil.example/api/v1/predict/123", "http://hedge.openledger.dev/api/v1/predict/123", "/admin"):
        with pytest.raises(ValueError):
            hedge._poll_url(url, base)


def test_prediction_polls_and_labels_assumptions(monkeypatch):
    monkeypatch.setattr(hedge.settings, "hedge_api_key", "secret-test-key")
    monkeypatch.setattr(hedge.time, "sleep", lambda _: None)
    calls = []
    responses = iter([
        {"status": "running", "progressPct": 40, "pollUrl": "/api/v1/predict/job-1"},
        {"status": "done", "generatedAt": "2026-09-25T14:00:00Z", "defaultsApplied": ["side", "leverage"],
         "input": {"exchange": "binance", "market": "futures", "symbol": "SOLUSDT", "side": "long", "notionalUsd": 1000,
                   "leverage": 5, "horizons": ["4h", "24h"]},
         "result": {"verdict": {"signal": "buy", "confidence": 62, "summary": "Test scenario."},
                    "plan": {"entry": 100, "expectedRoiPct": 3.8}, "scores": {"technical": 34}}},
    ])

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["headers"]["Authorization"] == "Bearer secret-test-key"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            data = next(responses)
            return Mock(raise_for_status=lambda: None, json=lambda: data)

    monkeypatch.setattr(hedge.httpx, "Client", FakeClient)
    card = hedge.predict("Predict SOL")
    assert [c[0] for c in calls] == ["POST", "GET"]
    assert calls[0][2]["json"] == {"symbol": "SOL"}
    assert calls[1][1].endswith("/api/v1/predict/job-1")
    assert "side, leverage" in card and "5.00× leverage" in card and "model scenario" in card
    assert "secret-test-key" not in card


def test_failed_job_does_not_render_a_signal(monkeypatch):
    monkeypatch.setattr(hedge.settings, "hedge_api_key", "test")
    monkeypatch.setattr(hedge, "_request", lambda *_args, **_kwargs: {"status": "error", "result": {"verdict": {"signal": "buy"}}})
    with pytest.raises(RuntimeError, match="failed"):
        hedge.predict("Predict SOL")


def test_chat_forecast_uses_prediction_before_tape(monkeypatch):
    from app.nodes import research

    monkeypatch.setattr(hedge, "enabled", lambda: True)
    async def decision(_request):
        return route.PredictionContract(kind="futures_scenario", symbol="SOL")
    monkeypatch.setattr(route, "plan", decision)
    monkeypatch.setattr(route, "listed_usdt_perpetual", lambda _symbol: True)
    monkeypatch.setattr(research, "tape_for", lambda _: (_ for _ in ()).throw(AssertionError("tape must not run")))
    card = {"kind": "futures_scenario_v1", "symbol": "SOLUSDT", "verdict": {"signal": "buy"}}
    monkeypatch.setattr(research, "get_provider_router", lambda: Mock(invoke=lambda name, request: Mock(
        output=json.dumps({"answer": "### SOLUSDT · futures scenario", "card": card}), tool=name)))
    out = asyncio.run(research.research_node({"request": "Predict SOL", "capabilities": ["market_data"],
                                             "chains": [], "session_context": {}}))
    assert out["trajectory"]["tool_name_0"] == "hedge_token_prediction"
    assert "SOLUSDT" in out["answer"]
    assert out["prediction_card"] == card
    assert "prediction_card" not in out["trajectory"]["observation_0"]


def test_prediction_card_keeps_only_typed_result_fields():
    data = {"status": "done", "generatedAt": "2026-09-25T14:00:00Z",
            "defaultsApplied": ["side", "leverage"], "private": "discard me",
            "input": {"symbol": "SOLUSDT", "exchange": "binance", "market": "futures",
                      "side": "long", "notionalUsd": 1000, "leverage": 5,
                      "horizons": ["4h", "24h"]},
            "result": {"verdict": {"signal": "buy", "confidence": 62, "summary": "Model scenario.",
                                   "keyFactors": [{"label": "Momentum", "impact": "bullish", "detail": "Higher highs."}],
                                   "risks": ["Liquidation risk"]},
                       "forecast": {"horizons": [{"label": "24h", "p10": 80, "p50": 100, "p90": 120,
                                                  "upProbPct": 61, "expectedMovePct": 3.2}]},
                       "plan": {"expectedPnlUsd": 30, "expectedRoiPct": 3.0},
                       "market": {"price": 100},
                       "candles": [{"secret": "discard me"}]}}
    card = hedge.compact_card(data)
    assert card["verdict"]["confidence"] == 62
    assert card["forecast"]["horizons"][0]["p50"] == 100
    assert card["input"]["defaults"] == ["side", "leverage"]
    assert "candles" not in json.dumps(card)
    assert "discard me" not in json.dumps(card)


def test_prediction_card_renders_fields_and_escapes_provider_text():
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    harness = Path(__file__).parent / "js" / "ui_harness.mjs"
    completed = subprocess.run([node, str(harness), "prediction_card_uses_structured_data"],
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    rendered = json.loads(completed.stdout)
    assert rendered["className"] == "prediction-card"
    assert "SOLUSDT" in rendered["html"] and "80% simulated range" in rendered["html"]
    assert "100.00" in rendered["html"] and "Liquidation risk" in rendered["html"]
    assert "<script>" not in rendered["html"] and "&lt;script&gt;" in rendered["html"]
    assert rendered["missing"] is None


def test_model_contract_cannot_invent_an_asset_or_change_venue():
    assert route._model_contract('{"kind":"futures_scenario","symbol":"ETH"}', "Predict SOL") is None
    valid = route._model_contract('{"kind":"futures_scenario","symbol":"SOL","venue":"binance"}', "Will SOL rise tomorrow?")
    assert valid and valid.symbol == "SOL" and valid.planner == "model"
    inferred = route._model_contract('{"kind":"futures_scenario","symbol":"BTC","side":"long","horizons":["tomorrow"],"venue":"binance"}',
                                     "Will BTC rise tomorrow?")
    assert inferred and inferred.side is None and inferred.horizons == ["24h"] and inferred.venue is None
    lowercase = route._model_contract('{"kind":"futures_scenario","symbol":"sol"}', "predict sol")
    assert lowercase and lowercase.symbol == "SOL"


def test_prediction_planner_preserves_explicit_side_and_horizon(monkeypatch):
    from app.nodes import runtime

    monkeypatch.setattr(runtime, "planner_available", lambda: True)
    async def model(*_args, **_kwargs):
        return Mock(contract='{"kind":"futures_scenario","symbol":"SOL","side":"long","horizons":["7d"]}')
    monkeypatch.setattr(runtime, "_call_planner_lm", model)
    decision = asyncio.run(route.plan("Forecast SOL short for 24h"))
    assert decision and decision.side == "short" and decision.horizons == ["24h"]
    assert decision.canonical_request() == "Predict SOL on Binance futures short for 24h"


def test_prediction_planner_excludes_other_markets(monkeypatch):
    from app.nodes import runtime
    monkeypatch.setattr(runtime, "planner_available", lambda: False)
    assert asyncio.run(route.plan("Predict SOL on Hyperliquid")) is None
    assert asyncio.run(route.plan("Forecast NVDA stock price target")) is None
    assert asyncio.run(route.plan("Polymarket prediction odds for BTC")) is None
    assert asyncio.run(route.plan("Predict SOL on Solana")) is None
    assert route.candidate("Any chance SOL goes up next week?") is True


def test_binance_instrument_directory_is_the_eligibility_boundary(monkeypatch):
    monkeypatch.setattr(route, "_directory", None)
    response = Mock(raise_for_status=lambda: None, json=lambda: {"symbols": [
        {"symbol": "SOLUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
        {"symbol": "OLDUSDT", "status": "BREAK", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
    ]})
    monkeypatch.setattr(route.httpx, "get", lambda *_args, **_kwargs: response)
    assert route.listed_usdt_perpetual("SOL") is True
    assert route.listed_usdt_perpetual("OLD") is False
    assert route.listed_usdt_perpetual("BONK") is False


def test_explicit_unlisted_futures_request_does_not_call_prediction(monkeypatch):
    from app.nodes import research
    monkeypatch.setattr(hedge, "enabled", lambda: True)
    async def decision(_request):
        return route.PredictionContract(kind="futures_scenario", symbol="ANSEM", market="futures")
    monkeypatch.setattr(route, "plan", decision)
    monkeypatch.setattr(route, "listed_usdt_perpetual", lambda _symbol: False)
    monkeypatch.setattr(research, "get_provider_router", lambda: (_ for _ in ()).throw(AssertionError("must not call provider")))
    out = asyncio.run(research.research_node({"request": "Predict ANSEM futures", "capabilities": ["market_data"],
                                             "chains": [], "session_context": {}}))
    assert "couldn't verify" in out["answer"] and out["trajectory"] is None
