import asyncio
from types import SimpleNamespace

from app import execution_policy
from app.nodes import runtime
from app.nodes import trading
from app.nodes.general import general_node
from app.experience import advance_session_context
from app.capability_router import route_capabilities, extract_charter


def _fake_plan(plan_id="plan_1", verified=True, usd=5.0, warnings=None):
    return SimpleNamespace(
        plan_id=plan_id,
        input_token=SimpleNamespace(symbol="SOL", decimals=9),
        output_token=SimpleNamespace(symbol="USDC", verified=verified),
        proposal=SimpleNamespace(amount_atomic=10_000_000, slippage_bps=50),
        input_value_usd=usd,
        quote={"priceImpactPct": "0.001"},
        warnings=warnings or [],
    )


def _patch(monkeypatch, verdict, summary="", reason=None, total=1000.0):
    async def fake_snapshot(_w):
        return {"total_usd_value": total}

    async def fake_call_lm(_program, **_kw):
        return SimpleNamespace(verdict=verdict, summary=summary, blocked_reason=reason)

    monkeypatch.setattr(trading, "build_portfolio_snapshot", fake_snapshot)
    monkeypatch.setattr(runtime, "_call_lm", fake_call_lm)


# ---- charter_risk_node ----

def test_charter_risk_passthrough_when_no_plan():
    assert asyncio.run(trading.charter_risk_node({"trade_plan": None})) == {}


def test_advisory_mode_passes_and_never_blocks(monkeypatch):
    _patch(monkeypatch, "ok", summary="0.5% of portfolio")
    plan = _fake_plan()
    out = asyncio.run(trading.charter_risk_node(
        {"trade_plan": plan, "wallet_address": "w", "session_context": {}}
    ))
    # The plan is not suppressed (no trade_plan key returned) and it's advisory.
    assert "trade_plan" not in out
    assert out["risk_assessment"].verdict == "ok"
    assert out["risk_assessment"].charter_applied is False


def test_no_charter_never_blocks_even_if_agent_says_blocked(monkeypatch):
    # Hard rule: the Risk agent can only make things stricter WITH a charter.
    _patch(monkeypatch, "blocked", summary="x", reason="y")
    plan = _fake_plan()
    out = asyncio.run(trading.charter_risk_node(
        {"trade_plan": plan, "wallet_address": "w", "session_context": {}}
    ))
    assert "trade_plan" not in out
    assert out["risk_assessment"].verdict == "ok"


def test_hard_veto_supersedes_plan_and_suppresses_card(monkeypatch):
    _patch(monkeypatch, "blocked", summary="exceeds your cap",
           reason="Trade is $50 but your cap is $10 — trim to $10.")
    superseded = []

    async def fake_supersede(plan_id):
        superseded.append(plan_id)

    monkeypatch.setattr(trading, "mark_plan_superseded", fake_supersede)
    plan = _fake_plan(plan_id="plan_block")
    out = asyncio.run(trading.charter_risk_node({
        "trade_plan": plan,
        "wallet_address": "w",
        "session_context": {"risk_charter": "only verified tokens, max $10 per trade"},
    }))
    assert out["trade_plan"] is None                       # card suppressed
    assert superseded == ["plan_block"]                    # CONFIRM token inert
    assert "Blocked by your risk charter" in out["answer"]
    assert out["risk_assessment"].verdict == "blocked"
    assert out["risk_assessment"].charter_applied is True


def test_risk_agent_failure_falls_back_to_advisory_not_a_block(monkeypatch):
    async def fake_snapshot(_w):
        return {"total_usd_value": 100.0}

    async def boom(_program, **_kw):
        raise RuntimeError("model down")

    monkeypatch.setattr(trading, "build_portfolio_snapshot", fake_snapshot)
    monkeypatch.setattr(runtime, "_call_lm", boom)
    out = asyncio.run(trading.charter_risk_node({
        "trade_plan": _fake_plan(),
        "wallet_address": "w",
        "session_context": {"risk_charter": "max $10 per trade"},
    }))
    assert "trade_plan" not in out  # a Risk-agent crash must not block a trade
    assert out["risk_assessment"].verdict == "ok"


# ---- charter commands ----

def test_charter_commands_route_to_control_not_the_trade_path():
    for message in (
        "set my risk charter: only verified tokens, max $10 per trade",
        "show my risk charter",
        "clear my risk charter",
    ):
        route = route_capabilities(message)
        assert route is not None and route.intent == "general" and route.reason == "risk_charter", message


def test_charter_extract_and_persist_and_clear():
    assert extract_charter("set my risk charter: max 1% of portfolio") == "max 1% of portfolio"
    ctx = advance_session_context({}, "set my risk charter: only verified tokens, max $10", None, "general", [], [], None)
    assert "only verified tokens" in ctx["risk_charter"]
    ctx2 = advance_session_context(ctx, "clear my risk charter", None, "general", [], [], None)
    assert ctx2["risk_charter"] is None
    # A plain message leaves an existing charter untouched.
    ctx3 = advance_session_context(ctx, "what is the price of SOL", None, "research", [], [], None)
    assert ctx3["risk_charter"] == ctx["risk_charter"]


def test_general_node_charter_replies():
    out_set = asyncio.run(general_node({"request": "set my risk charter: max $10 per trade", "session_context": {}}))
    assert "Risk charter updated" in out_set["answer"]
    out_show = asyncio.run(general_node({"request": "show my risk charter", "session_context": {"risk_charter": "max $10 per trade"}}))
    assert "max $10 per trade" in out_show["answer"]
    out_empty = asyncio.run(general_node({"request": "show my risk charter", "session_context": {}}))
    assert "No risk charter" in out_empty["answer"]


def test_policy_question_is_answered_from_settings_not_the_model(monkeypatch):
    import asyncio
    from app.nodes import general, runtime
    from app.settings import settings

    async def forbidden(*_a, **_k):
        raise AssertionError("a policy question must not reach the general LM")

    monkeypatch.setattr(runtime, "_call_lm", forbidden)
    monkeypatch.setattr(settings, "max_trade_usd", 25.0)
    state = {
        "request": "max spend policy?", "routing_decision": {"speech_act": "policy"},
        "session_context": {"risk_charter": "only verified tokens", "team_mode": True},
        "wallet_address": "3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS",
    }
    answer = asyncio.run(general.general_node(state))["answer"]
    assert "$25.00" in answer and "only verified tokens" in answer and "3aHLqH…KZkS" in answer
    assert "Team desk**: ON" in answer and "bps" in answer


def test_policy_speech_act_routes_to_general():
    import asyncio
    from types import SimpleNamespace
    from app.routing import resolver
    from app.routing.semantic import SpeechUnderstanding

    async def policy(program, **kwargs):
        return SimpleNamespace(understanding=SpeechUnderstanding(speech_act="policy", domain="wallet", explicit_action=False, confidence=0.96))

    resolver._understanding_cache.clear()
    result = asyncio.run(resolver.resolve({"request": "my wallet policy?", "session_context": {}}, policy, embedding_factory=lambda _m: None))
    assert result["intent"] == "general" and result["routing_decision"]["speech_act"] == "policy"


def test_chat_response_and_history_carry_the_persisted_risk_charter(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    from app.graph import AgentRun

    async def fake_run(message, wallet, history, session_context, action):
        return AgentRun(answer="ok", trajectory=None, trade_plan=None, intent="general", capabilities=[])

    monkeypatch.setattr(execution_policy, "run_agent", fake_run)
    client = TestClient(main.app)
    from tests.conftest import sign_in
    sign_in(client)
    first = client.post("/chat", json={"message": "set my risk charter: only verified tokens, max $10 per trade"})
    assert first.status_code == 200, first.text
    assert first.json()["risk_charter"] == "only verified tokens, max $10 per trade"
    sid, rev = first.json()["session_id"], first.json()["session_revision"]
    assert client.get(f"/chat/history/{sid}").json()["context"]["risk_charter"] == "only verified tokens, max $10 per trade"
    second = client.post("/chat", json={"message": "hello", "session_id": sid, "context_revision": rev})
    assert second.json()["risk_charter"] == "only verified tokens, max $10 per trade"  # persists across turns
    third = client.post("/chat", json={"message": "clear my risk charter", "session_id": sid, "context_revision": second.json()["session_revision"]})
    assert third.json()["risk_charter"] is None


def test_unset_charter_replies_include_a_complete_example_prompt():
    import asyncio
    from app.nodes import general

    unset = {"request": "my wallet policy?", "routing_decision": {"speech_act": "policy"}, "session_context": {}, "wallet_address": ""}
    summary = asyncio.run(general.general_node(unset))["answer"]
    assert general.CHARTER_EXAMPLE in summary and summary.count("set my risk charter") == 1
    shown = asyncio.run(general.general_node({"request": "show my risk charter", "session_context": {}}))["answer"]
    assert general.CHARTER_EXAMPLE in shown
    is_set = dict(unset, session_context={"risk_charter": "max $5 per trade"})
    assert general.CHARTER_EXAMPLE not in asyncio.run(general.general_node(is_set))["answer"]


# ---- structured charter (the card) ----

def test_charter_fields_render_canonical_text_and_reject_unknown_chains():
    import pytest as _pytest
    from app.models import RiskCharterFields

    f = RiskCharterFields(max_trade_usd=10, max_position_pct=1, max_slippage_bps=50, verified_only=True,
                          allowed_chains=["Solana", "base", "solana"], notes="  no memecoins   during selloffs ")
    assert f.render() == ("max $10.00 per trade; max 1% of portfolio per position; max 50 bps slippage; "
                          "only Jupiter-verified tokens; chains: solana, base; also: no memecoins during selloffs")
    assert RiskCharterFields().is_empty()
    with _pytest.raises(ValueError):
        RiskCharterFields(allowed_chains=["mars"])


def _forbid_lm(monkeypatch):
    async def forbidden(*_a, **_k):
        raise AssertionError("structured rules must be checked without the Risk agent")

    async def fake_snapshot(_w):
        return {"total_usd_value": 200.0}

    monkeypatch.setattr(runtime, "_call_lm", forbidden)
    monkeypatch.setattr(trading, "build_portfolio_snapshot", fake_snapshot)


def test_structured_charter_blocks_deterministically_without_the_model(monkeypatch):
    _forbid_lm(monkeypatch)
    superseded = []

    async def fake_supersede(plan_id):
        superseded.append(plan_id)

    monkeypatch.setattr(trading, "mark_plan_superseded", fake_supersede)
    fields = {"max_trade_usd": 3.0, "max_position_pct": 1.0, "max_slippage_bps": 30, "verified_only": True, "allowed_chains": ["base"]}
    out = asyncio.run(trading.charter_risk_node({
        "trade_plan": _fake_plan(plan_id="p", verified=False, usd=5.0), "wallet_address": "w",
        "session_context": {"risk_charter": "structured", "risk_charter_fields": fields},
    }))
    assert out["trade_plan"] is None and superseded == ["p"]
    text = out["answer"]
    for expected in ("$5.00", "max per trade is $3.00", "2.5% of your $200.00 portfolio", "slippage is 50 bps", "not Jupiter-verified", "allows only base"):
        assert expected in text, expected


def test_structured_charter_within_rules_skips_the_model(monkeypatch):
    _forbid_lm(monkeypatch)
    fields = {"max_trade_usd": 10.0, "max_slippage_bps": 50, "verified_only": True}
    out = asyncio.run(trading.charter_risk_node({
        "trade_plan": _fake_plan(usd=5.0), "wallet_address": "w",
        "session_context": {"risk_charter": "structured", "risk_charter_fields": fields},
    }))
    assert out["risk_assessment"].verdict == "ok" and out["risk_assessment"].charter_applied is True
    assert "checked: max trade usd, max slippage bps, verified only" in out["risk_assessment"].summary


def test_structured_charter_with_notes_still_consults_the_model(monkeypatch):
    _patch(monkeypatch, "ok", summary="fine")
    fields = {"max_trade_usd": 10.0, "notes": "no memecoins"}
    out = asyncio.run(trading.charter_risk_node({
        "trade_plan": _fake_plan(usd=5.0), "wallet_address": "w",
        "session_context": {"risk_charter": "structured", "risk_charter_fields": fields},
    }))
    assert out["risk_assessment"].summary == "fine"


def test_card_saves_through_chat_and_free_text_replaces_fields(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    from app.graph import AgentRun
    from app.settings import settings

    async def fake_run(message, wallet, history, session_context, action):
        return AgentRun(answer=message, trajectory=None, trade_plan=None, intent="general", capabilities=[])

    monkeypatch.setattr(execution_policy, "run_agent", fake_run)
    monkeypatch.setattr(settings, "max_trade_usd", 25.0)
    client = TestClient(main.app)
    from tests.conftest import sign_in
    sign_in(client)
    assert client.get("/chat/risk-charter/limits").json()["max_trade_usd"] == 25.0

    saved = client.post("/chat", json={"message": "set my risk charter", "risk_charter_fields": {"max_trade_usd": 10, "verified_only": True}})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["answer"] == "set my risk charter: max $10.00 per trade; only Jupiter-verified tokens"  # canonical message reached the agent
    assert body["risk_charter"] == "max $10.00 per trade; only Jupiter-verified tokens"
    assert body["risk_charter_fields"]["max_trade_usd"] == 10 and body["risk_charter_fields"]["verified_only"] is True
    sid, rev = body["session_id"], body["session_revision"]
    assert client.get(f"/chat/history/{sid}").json()["context"]["risk_charter_fields"]["max_trade_usd"] == 10

    too_high = client.post("/chat", json={"message": "set my risk charter", "session_id": sid, "context_revision": rev,
                                          "risk_charter_fields": {"max_trade_usd": 100}})
    assert too_high.status_code == 400 and "built-in cap" in too_high.json()["detail"]
    empty = client.post("/chat", json={"message": "set my risk charter", "session_id": sid, "context_revision": rev, "risk_charter_fields": {}})
    assert empty.status_code == 400

    typed = client.post("/chat", json={"message": "set my risk charter: only bluechips", "session_id": sid, "context_revision": rev})
    assert typed.json()["risk_charter"] == "only bluechips" and typed.json()["risk_charter_fields"] is None
    cleared = client.post("/chat", json={"message": "clear my risk charter", "session_id": sid, "context_revision": typed.json()["session_revision"]})
    assert cleared.json()["risk_charter"] is None and cleared.json()["risk_charter_fields"] is None


def test_clearing_the_charter_in_chat_also_clears_the_saved_default(monkeypatch):
    """Live regression (e2e sweep): a signed-in user cleared the charter, but
    the saved default in preferences survived and every new conversation
    re-applied a $0.50 cap that blocked all quotes."""
    from fastapi.testclient import TestClient
    from app import main
    from app.graph import AgentRun

    async def fake_run(message, wallet, history, session_context, action):
        return AgentRun(answer="ok", trajectory=None, trade_plan=None, intent="general", capabilities=[])

    monkeypatch.setattr(execution_policy, "run_agent", fake_run)
    client = TestClient(main.app)
    from tests.conftest import sign_in
    sign_in(client)
    first = client.post("/chat", json={"message": "set my risk charter", "risk_charter_fields": {"max_trade_usd": 0.5, "verified_only": True}})
    assert first.status_code == 200, first.text
    assert client.get("/me").json()["user"]["preferences"]["risk_charter_fields"]["max_trade_usd"] == 0.5
    # A brand-new conversation is seeded from the saved default.
    fresh = client.post("/chat", json={"message": "hello"}).json()
    assert fresh["risk_charter_fields"]["max_trade_usd"] == 0.5
    cleared = client.post("/chat", json={"message": "clear my risk charter", "session_id": fresh["session_id"], "context_revision": fresh["session_revision"]}).json()
    assert cleared["risk_charter"] is None and cleared["risk_charter_fields"] is None
    assert client.get("/me").json()["user"]["preferences"].get("risk_charter_fields") is None
    # The next conversation starts clean.
    after = client.post("/chat", json={"message": "hello again"}).json()
    assert after["risk_charter"] is None and after.get("risk_charter_fields") is None
