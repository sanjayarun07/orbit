"""ROUTING_BACKEND: which model answers the classification seam, for an A/B
deployment of Jev against the speech model. Whatever the backend, a Jev
outage or an under-confident Jev answer hands off to the speech model, the
decision's provenance is stamped into routing_decision, and a production
instance on Jev without a key refuses to boot.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import deployment
from app.nodes import routing as routing_node, runtime
from app.routing import backends, jev_backend
from app.routing.decided import decided_by
from app.settings import Settings, settings


async def _decide(request: str):
    """Run the configured backend and read the provenance INSIDE the task context."""
    result = await backends.intent_call_lm()(None, request=request)
    return result, decided_by.get()


def _understanding(act="research", confidence=0.97, domain="crypto"):
    return SimpleNamespace(understanding={"speech_act": act, "domain": domain, "explicit_action": False, "confidence": confidence})


@pytest.fixture
def speech_model(monkeypatch):
    calls = []

    async def fake(program, **kwargs):
        calls.append(kwargs["request"])
        return _understanding("research", 0.99)

    monkeypatch.setattr(runtime, "_call_intent_lm", fake)
    return calls


def test_the_default_backend_is_the_speech_model():
    assert settings.routing_backend == "speech_model"
    assert backends.intent_call_lm() is runtime._call_intent_lm


def test_jev_decides_and_is_recorded(monkeypatch, speech_model):
    monkeypatch.setattr(settings, "routing_backend", "jev")

    async def jev(program, **kwargs):
        return _understanding("advice", 0.95)

    monkeypatch.setattr(jev_backend, "call_lm", jev)
    result, who = asyncio.run(_decide("thoughts on HYPE"))
    assert result.understanding["speech_act"] == "advice" and who == "jev" and speech_model == []


@pytest.mark.parametrize("backend", ["jev", "jev_fallthrough"])
def test_a_jev_outage_hands_off_to_the_speech_model(monkeypatch, speech_model, backend):
    monkeypatch.setattr(settings, "routing_backend", backend)

    async def down(program, **kwargs):
        raise jev_backend.JevError("529 overloaded")

    monkeypatch.setattr(jev_backend, "call_lm", down)
    result, who = asyncio.run(_decide("why is PEPE dumping"))
    assert result.understanding["speech_act"] == "research" and speech_model == ["why is PEPE dumping"]
    assert who == "speech_model:jev_unavailable"


@pytest.mark.parametrize("act,confidence", [("research", 0.52), ("abstain", 0.99)])
def test_fallthrough_hands_an_uncertain_jev_answer_to_the_speech_model(monkeypatch, speech_model, act, confidence):
    monkeypatch.setattr(settings, "routing_backend", "jev_fallthrough")

    async def unsure(program, **kwargs):
        return _understanding(act, confidence)

    monkeypatch.setattr(jev_backend, "call_lm", unsure)
    result, who = asyncio.run(_decide("background on Berachain"))
    assert speech_model == ["background on Berachain"] and result.understanding["confidence"] == 0.99
    assert who == "speech_model:jev_uncertain"


def test_fallthrough_keeps_a_confident_jev_answer(monkeypatch, speech_model):
    monkeypatch.setattr(settings, "routing_backend", "jev_fallthrough")

    async def sure(program, **kwargs):
        return _understanding("portfolio", 0.96)

    monkeypatch.setattr(jev_backend, "call_lm", sure)
    result, who = asyncio.run(_decide("how much SOL do I have"))
    assert result.understanding["speech_act"] == "portfolio" and speech_model == [] and who == "jev"


def test_the_routing_node_stamps_backend_and_provenance_into_the_decision(monkeypatch, speech_model):
    monkeypatch.setattr(settings, "routing_backend", "jev_fallthrough")

    async def sure(program, **kwargs):
        return _understanding("research", 0.97)

    monkeypatch.setattr(jev_backend, "call_lm", sure)
    from app.routing import resolver
    resolver._understanding_cache.clear()
    out = asyncio.run(routing_node.resolve_intent_node({"request": "what's the hot narrative rn", "history": "", "session_context": {}}))
    decision = out["routing_decision"]
    assert decision["routing_backend"] == "jev_fallthrough" and decision["decided_by"] == "jev"
    # A rule-anchored turn (a pasted address is a hard entity) reaches no model and says so.
    out = asyncio.run(routing_node.resolve_intent_node({"request": "Top holders for 0x940181a94A35A4569E4529A3CDfB74e38FD98631 on Base", "history": "", "session_context": {}}))
    assert out["routing_decision"]["decided_by"] == "none" and out["routing_decision"]["method"] == "rules"


def test_an_unknown_backend_is_refused_by_the_audit():
    cfg = Settings(); cfg.routing_backend = "gpt-magic"
    assert "routing-backend-unrecognised" in {p.code for p in deployment.audit(cfg) if p.severity == deployment.FATAL}
    with pytest.raises(ValueError):
        settings_backup = settings.routing_backend
        try:
            settings.routing_backend = "gpt-magic"
            backends.intent_call_lm()
        finally:
            settings.routing_backend = settings_backup


def test_a_production_instance_on_jev_without_a_key_refuses_to_boot():
    cfg = Settings()
    for k, v in dict(environment="production", deployment_mode="research", live_trading=False, dev_expose_magic_links=False,
                     allow_memory_fallback=False, database_url="postgresql://db/orbit", redis_url="redis://cache:6379/0",
                     mcp_api_key="m", admin_api_key="a", resend_api_key="r", openai_api_key="k",
                     public_base_url="https://orbit.example.com", routing_backend="jev_fallthrough", typesafe_api_key=None).items():
        setattr(cfg, k, v)
    fatal = {p.code for p in deployment.audit(cfg) if p.severity == deployment.FATAL}
    assert fatal == {"routing-backend-key-missing"}
    cfg.typesafe_api_key = "ts-key"
    assert deployment.audit(cfg) == []
    cfg.environment = "development"; cfg.typesafe_api_key = None
    assert "routing-backend-key-missing" in {p.code for p in deployment.audit(cfg) if p.severity == deployment.WARNING}
