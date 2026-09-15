import asyncio
from types import SimpleNamespace

from app.nodes import team, runtime
from app.answer_validator import validate_answer
from app.routing.resolver import resolve
from app.experience import advance_session_context
from app.capability_router import route_capabilities


_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def test_asset_market_data_emits_multi_source_trajectory(monkeypatch):
    # Two distinct providers return comparable headline metrics that DISAGREE on
    # 24h volume -> the trajectory feeds the validator's consistency check.
    outputs = {
        "birdeye": "# Bonk\n**Provider**: Birdeye\n- **Price**: $0.00000274\n- **24h volume**: $7.6M\n- **Liquidity**: $3.4M",
        "coingecko": "# Bonk\n**Provider**: CoinGecko\n- **Price**: $0.00000274\n- **24h volume**: $22.7M\n- **Liquidity**: $3.3M",
    }

    class FakeRouter:
        def candidates(self, request, capability, chains=(), **k):
            return [SimpleNamespace(provider="birdeye"), SimpleNamespace(provider="coingecko")]

        def try_route_across(self, request, caps, chains=(), *, provider=None):
            if "holders" in request:
                return SimpleNamespace(tool="bitquery_token_top_holders", output="# Top holders\n| 1 | Aaa | 100 |")
            return SimpleNamespace(tool=f"{provider}_token_overview", output=outputs[provider])

    monkeypatch.setattr(team, "get_provider_router", lambda: FakeRouter())
    text, trajectory = asyncio.run(team._asset_market_data(_MINT, "solana"))
    # Three observations: two snapshot sources + holders, each subject-tagged.
    obs = [v for k, v in trajectory.items() if k.startswith("observation_")]
    assert len(obs) == 3
    assert all(_MINT in o for o in obs)

    # The validator, over the desk's nested trajectory, flags the volume gap.
    v = validate_answer("should I buy BONK", f"BONK snapshot for {_MINT}.", {"market_research": trajectory}, "team")
    consistency = next(c for c in v.checks if c.name == "consistency")
    assert consistency.status == "warn" and "volume" in consistency.detail


# ---- team-mode toggle ----

def test_team_toggle_commands_route_to_control_not_content():
    for message in ("enable team mode", "disable team mode", "team status"):
        route = route_capabilities(message)
        assert route is not None and route.intent == "general" and route.reason == "team_mode", message


def test_team_toggle_persists_in_session_context():
    ctx = advance_session_context({}, "enable team mode", None, "general", [], [], None)
    assert ctx["team_mode"] is True
    ctx2 = advance_session_context(ctx, "disable team mode", None, "general", [], [], None)
    assert ctx2["team_mode"] is False


# ---- routing into the team ----

def _no_lm():
    async def call_lm(*_a, **_k):
        raise AssertionError("high-confidence rules path must not call the LM")

    class _Emb:
        def classify(self, _req):
            raise AssertionError("high-confidence rules path must not embed")

    return call_lm, (lambda _m: _Emb())


def test_team_mode_redirects_trade_and_research():
    call_lm, emb = _no_lm()
    trade = asyncio.run(resolve(
        {"request": "swap 0.01 SOL to USDC with 50 bps slippage", "session_context": {"team_mode": True}},
        call_lm, embedding_factory=emb,
    ))
    assert trade["intent"] == "team" and trade["team_subintent"] == "trade"

    analysis = asyncio.run(resolve(
        {"request": "TVL on Solana", "session_context": {"team_mode": True}},
        call_lm, embedding_factory=emb,
    ))
    assert analysis["intent"] == "team" and analysis["team_subintent"] == "analysis"


def test_team_mode_off_leaves_routing_unchanged():
    call_lm, emb = _no_lm()
    out = asyncio.run(resolve(
        {"request": "swap 0.01 SOL to USDC with 50 bps slippage", "session_context": {}},
        call_lm, embedding_factory=emb,
    ))
    assert out["intent"] == "trade" and "team_subintent" not in out


# ---- Coordinator node ----

def _patch_lm(monkeypatch, thesis="Structure... CONVICTION 7/10\nWRONG IF x", conviction=7, synth="Desk recommendation."):
    async def fake_call_lm(program, **_kw):
        if program is runtime.market_research_agent:
            return SimpleNamespace(thesis=thesis, conviction=conviction)
        return SimpleNamespace(answer=synth)
    monkeypatch.setattr(runtime, "_call_lm", fake_call_lm)


def _no_asset(monkeypatch):
    # Force the market-research fallback to the (mocked) research_node path.
    monkeypatch.setattr(team, "_resolve_research_asset", lambda _r, _c=(): _coro((None, None)))


def test_resolve_research_asset(monkeypatch):
    # Bare "SOL" -> wrapped SOL mint without a registry lookup.
    monkeypatch.setattr(team, "search_verified_tokens", lambda q: [])
    assert asyncio.run(team._resolve_research_asset("should I buy SOL?")) == (team.WRAPPED_SOL_MINT, "solana")
    # A verified Solana canonical wins first (authoritative, unpolluted).
    monkeypatch.setattr(team, "search_verified_tokens",
                        lambda q: [{"mint": "BONKverified", "symbol": "BONK", "tags": ["verified"]}])
    monkeypatch.setattr(team, "token_candidates",
                        lambda symbol, chains=(): (_ for _ in ()).throw(AssertionError("verified canonical wins first")))
    assert asyncio.run(team._resolve_research_asset("thoughts on BONK")) == ("BONKverified", "solana")
    # No verified Solana match -> a single dominant DEX Screener candidate resolves.
    monkeypatch.setattr(team, "search_verified_tokens", lambda q: [])
    monkeypatch.setattr(team, "token_candidates", lambda symbol, chains=(): [
        {"chain": "base", "address": "0x9401", "name": "Aero", "symbol": "AERO", "liquidity_usd": 5_000_000.0},
    ])
    assert asyncio.run(team._resolve_research_asset("thoughts on AERO")) == ("0x9401", "base")
    # An explicit mint is used directly.
    mint = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
    assert asyncio.run(team._resolve_research_asset(f"analyze {mint}"))[0] == mint
    # A swap into a stablecoin has no non-stable target -> no resolution.
    monkeypatch.setattr(team, "token_candidates", lambda symbol, chains=(): [])
    assert asyncio.run(team._resolve_research_asset("swap 0.01 SOL to USDC 50 bps")) == (None, None)


def test_resolve_research_asset_defers_ambiguous_symbol(monkeypatch):
    # No verified Solana match, and comparable same-symbol tokens on different
    # chains -> the desk doesn't guess; it defers so the framed research asks.
    monkeypatch.setattr(team, "search_verified_tokens", lambda q: [])
    monkeypatch.setattr(team, "token_candidates", lambda symbol, chains=(): [
        {"chain": "ethereum", "address": "0x6982", "name": "Pepe", "symbol": "PEPE", "liquidity_usd": 3.0e8},
        {"chain": "base", "address": "0x6921", "name": "Pepe", "symbol": "PEPE", "liquidity_usd": 1.2e8},
    ])
    assert asyncio.run(team._resolve_research_asset("thoughts on PEPE")) == (None, None)


def test_team_trade_path_synthesizes_and_carries_card(monkeypatch):
    _no_asset(monkeypatch)
    async def fake_research(_s):
        return {"answer": "SOL/USDC price and funding data", "trajectory": None}
    monkeypatch.setattr(team, "research_node", fake_research)

    async def fake_planner(_s):
        return {"answer": "drafting", "proposal": object()}
    monkeypatch.setattr(team, "trade_planner_node", fake_planner)
    monkeypatch.setattr(team, "risk_check_node", lambda _s: {})

    async def fake_quote(_s):
        return {"trade_plan": SimpleNamespace(plan_id="p1", confirmation_text="CONFIRM p1")}
    monkeypatch.setattr(team, "quote_and_simulate_node", fake_quote)

    async def fake_charter(_s):
        return {"risk_assessment": SimpleNamespace(verdict="ok", summary="within rules")}
    monkeypatch.setattr(team, "charter_risk_node", fake_charter)
    monkeypatch.setattr(team, "finalize_trade_node", lambda _s: {"answer": "Reviewable plan. CONFIRM p1"})
    _patch_lm(monkeypatch, synth="Desk: buy, conviction 7, risk ok. Approve CONFIRM p1.")

    out = asyncio.run(team.team_node({
        "request": "swap 0.01 SOL to USDC 50 bps", "contextual_request": "swap 0.01 SOL to USDC 50 bps",
        "team_subintent": "trade", "session_context": {}, "wallet_address": "w", "capabilities": ["swap"],
    }))
    assert out["intent"] == "trade"
    assert out["trade_plan"].plan_id == "p1"
    assert out["risk_assessment"].verdict == "ok"
    assert "Desk" in out["answer"]
    assert out["team_report"]["conviction"] == 7


def test_team_trade_path_risk_block_suppresses_card(monkeypatch):
    _no_asset(monkeypatch)
    async def fake_research(_s):
        return {"answer": "data", "trajectory": None}
    monkeypatch.setattr(team, "research_node", fake_research)
    monkeypatch.setattr(team, "trade_planner_node", lambda _s: _coro({"answer": "drafting", "proposal": object()}))
    monkeypatch.setattr(team, "risk_check_node", lambda _s: {})
    monkeypatch.setattr(team, "quote_and_simulate_node", lambda _s: _coro({"trade_plan": SimpleNamespace(plan_id="p2", confirmation_text="CONFIRM p2")}))
    monkeypatch.setattr(team, "charter_risk_node", lambda _s: _coro({"trade_plan": None, "answer": "🚫 Blocked by your risk charter", "risk_assessment": SimpleNamespace(verdict="blocked", summary="over cap")}))
    monkeypatch.setattr(team, "finalize_trade_node", lambda _s: {})
    _patch_lm(monkeypatch, synth="Blocked: trim to comply.")

    out = asyncio.run(team.team_node({
        "request": "swap 1000 SOL to USDC 50 bps", "contextual_request": "swap 1000 SOL to USDC 50 bps",
        "team_subintent": "trade", "session_context": {"risk_charter": "max $10 per trade"}, "wallet_address": "w",
    }))
    assert out["trade_plan"] is None
    assert out["risk_assessment"].verdict == "blocked"


def test_team_analysis_path_no_trade(monkeypatch):
    _no_asset(monkeypatch)
    async def fake_research(_s):
        return {"answer": "SOL data", "trajectory": None}
    monkeypatch.setattr(team, "research_node", fake_research)

    async def boom(*_a, **_k):
        raise AssertionError("execution must not run for an analysis request")
    monkeypatch.setattr(team, "trade_planner_node", boom)
    _patch_lm(monkeypatch, thesis="SOL thesis CONVICTION 6/10", conviction=6, synth="Should you buy SOL? Desk view...")

    out = asyncio.run(team.team_node({
        "request": "should I buy SOL", "contextual_request": "should I buy SOL",
        "team_subintent": "analysis", "session_context": {},
    }))
    assert out["intent"] == "research"
    assert out["trade_plan"] is None
    assert "Desk view" in out["answer"]
    assert out["team_report"]["conviction"] == 6


def _coro(value):
    async def _c(*_a, **_k):
        return value
    return _c()
