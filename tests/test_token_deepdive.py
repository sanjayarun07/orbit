import asyncio
from types import SimpleNamespace

from app import token_deepdive
from app.token_deepdive import ANALYSIS_RULES, build_token_evidence, format_evidence_bundle
from app.nodes import research, runtime


_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


# ---- the reverse-engineered grounding rules ----

def test_analysis_rules_encode_the_key_confusions():
    r = ANALYSIS_RULES.lower()
    assert "beneficial owner" in r          # holder lists != beneficial owners
    assert "not proof of no mint" in r or "not proof of no mint/freeze" in r
    assert "unknown, not 'safe'" in r
    assert "unlocks are the most deterministic" in r
    assert "not contract supply or fdv" in r


# ---- evidence bundle composition ----

class _FakeRouter:
    def __init__(self, by_cap):
        self._by_cap = by_cap

    def try_route(self, request, capability, chains=(), **k):
        return self._by_cap.get(capability)

    def try_route_across(self, request, caps, chains=(), **k):
        for c in caps:
            if c in self._by_cap:
                return self._by_cap[c]
        return None


def test_build_evidence_bundle_marks_coverage_and_gaps(monkeypatch):
    by_cap = {
        "token_security": SimpleNamespace(tool="solana_token_security", output="# Security\nno flags"),
        "market_data": SimpleNamespace(tool="birdeye_token_overview", output="# Market\nprice $0.00000278"),
        "token_discovery": SimpleNamespace(tool="bitquery_token_top_holders", output="# Holders\ntop10 30%"),
        "market_sentiment": SimpleNamespace(tool="market_sentiment_snapshot", output="# Sentiment\n57 Greed"),
    }
    monkeypatch.setattr(token_deepdive, "get_provider_router", lambda: _FakeRouter(by_cap))
    bundle = asyncio.run(build_token_evidence(_MINT, "solana"))
    have, total = bundle.coverage
    assert total == 10 and have >= 4                   # 5 composable + liquidity locks + latest trades + 3 disclosed gaps
    names = {d.name: d.status for d in bundle.dimensions}
    assert names["identity_safety"] == "available"
    assert names["market"] == "available"
    # The three we can't source yet are present and explicitly unavailable.
    assert names["unlocks"] == "unavailable"
    assert names["exchange_flows"] == "unavailable"
    assert names["onchain_quant"] == "unavailable"

    rendered = format_evidence_bundle(bundle)
    assert "Coverage" in rendered and "dimensions available" in rendered
    assert "solana_token_security" in rendered
    assert "UNAVAILABLE" in rendered                   # a gap is shown, not hidden


def test_build_evidence_degrades_when_a_source_dies(monkeypatch):
    class _Boom:
        def try_route(self, *a, **k):
            raise RuntimeError("down")
        def try_route_across(self, *a, **k):
            raise RuntimeError("down")
    monkeypatch.setattr(token_deepdive, "get_provider_router", lambda: _Boom())
    bundle = asyncio.run(build_token_evidence(_MINT, "solana"))
    assert bundle.coverage == (0, 10)                   # every composable dim unavailable, never raises


# ---- the deep-dive intercept ----

def _mock_deepdive_pieces(monkeypatch, resolution):
    monkeypatch.setattr(research, "_resolve_named_token", lambda *a, **k: _coro(resolution))
    monkeypatch.setattr(research, "build_token_evidence",
                        lambda addr, chain, symbol=None: _coro(SimpleNamespace(dimensions=[], address=addr, chain=chain)))
    monkeypatch.setattr(research, "format_evidence_bundle", lambda b: "EVIDENCE")

    async def fake_call_lm(program, **kw):
        return SimpleNamespace(answer="Bottom line: medium confidence.\nWhat would flip this: X.")
    monkeypatch.setattr(runtime, "_call_lm", fake_call_lm)


def _coro(value):
    async def _c(*_a, **_k):
        return value
    return _c()


def test_deepdive_intercept_resolves_and_synthesizes(monkeypatch):
    resolved = research._TokenResolution(f"top holders of BONK {_MINT} on solana", chain="solana")
    _mock_deepdive_pieces(monkeypatch, resolved)
    out = asyncio.run(research._run_token_deep_dive({"capabilities": ["web_research"], "chains": []}, "deep dive on BONK"))
    assert out is not None
    assert out["trajectory"]["tool_name_0"] == "token_deep_dive"
    assert "flip this" in out["answer"].lower()


def test_deepdive_intercept_asks_on_ambiguous_ticker(monkeypatch):
    # The resolver stamps its SYNTHETIC "top holders of PEPE" probe as
    # original_request; the deep-dive path must override it with the user's real
    # ask so a disambiguation reply resumes the deep-dive, not a holders lookup.
    ask = research._TokenResolution("top holders of PEPE", clarification="PEPE exists on several chains...",
                                    pending={"symbol": "PEPE", "candidates": [], "original_request": "top holders of PEPE"})
    monkeypatch.setattr(research, "_resolve_named_token", lambda *a, **k: _coro(ask))
    out = asyncio.run(research._run_token_deep_dive({"capabilities": ["web_research"], "chains": []}, "analyze PEPE"))
    assert out is not None and "several chains" in out["answer"]
    assert out.get("pending_token", {}).get("symbol") == "PEPE"
    assert out["pending_token"]["original_request"] == "analyze PEPE"  # not the synthetic probe


def test_deepdive_intercept_falls_through_without_a_token(monkeypatch):
    # "analyze the market" names no token -> None so normal research handles it.
    out = asyncio.run(research._run_token_deep_dive({"capabilities": ["web_research"], "chains": []}, "analyze the market today"))
    assert out is None
