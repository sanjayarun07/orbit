import asyncio
from types import SimpleNamespace

import pytest

from app.routing import resolver
from app.routing.resolver import resolve
from app.routing.semantic import SemanticMatch, SpeechUnderstanding


class _FakeEmbeddingRouter:
    """Tracks whether .classify was invoked and returns a fixed abstention,
    so a test can assert on reach without depending on a real embedding call."""

    def __init__(self):
        self.calls: list[str] = []

    def classify(self, request: str) -> SemanticMatch:
        self.calls.append(request)
        return SemanticMatch(None, "embedding", reason="low_margin")


async def _never_called(*_args, **_kwargs):
    raise AssertionError("call_lm should not be reached in these tests")


def _model(speech_act, domain="crypto", confidence=0.97, explicit=False):
    calls = []

    async def call_lm(program, **kwargs):
        calls.append(kwargs["request"])
        return SimpleNamespace(understanding=SpeechUnderstanding(
            speech_act=speech_act, domain=domain, explicit_action=explicit, confidence=confidence))

    call_lm.calls = calls
    return call_lm


@pytest.fixture(autouse=True)
def _clear_classifier_cache():
    resolver._understanding_cache.clear()
    yield
    resolver._understanding_cache.clear()


def test_topical_question_is_decided_by_the_model_not_the_keyword_rule():
    """'explain what TVL means' trips the DEFI keyword rule (research/defi_data),
    but the act is an explanation. The model decides the intent; the embedding
    tier is not reached at all."""
    fake = _FakeEmbeddingRouter()
    call_lm = _model("explain", domain="general")
    result = asyncio.run(resolve({"request": "explain what TVL means", "session_context": {}}, call_lm, embedding_factory=lambda _m: fake))
    assert call_lm.calls == ["explain what TVL means"]
    assert fake.calls == []
    assert result["intent"] == "general" and "clarification" not in result
    assert result["routing_decision"]["reason"] == "overrides:defi"


def test_model_agreeing_with_a_rule_keeps_the_rules_richer_capabilities():
    call_lm = _model("research")
    result = asyncio.run(resolve({"request": "TVL on Solana", "session_context": {}}, call_lm, embedding_factory=_never_called))
    assert result["intent"] == "research"
    assert result["capabilities"] == ["defi_data"]  # the rule's hint, not the generic web_research
    assert result["routing_decision"]["reason"] == "agrees:defi"


def test_anchored_rules_stay_deterministic_zero_model_or_embedding_calls():
    """An address, an execution command with its fields, 'my' ownership: these
    are anchored by a hard signal and must stay exactly as fast/deterministic
    as before -- neither the model nor the embedding tier is reached."""
    fake = _FakeEmbeddingRouter()
    for request in (
        "Show top holders for 0x1111111111111111111111111111111111111111 token",
        "swap 0.1 SOL to USDC on Base with 50 bps slippage",
        "how much SOL do I have",
        "What would happen if I sold half my SOL?",
    ):
        result = asyncio.run(resolve({"request": request, "session_context": {}}, _never_called, embedding_factory=lambda _m: fake))
        assert result["route_source"] == "rules", request
    assert fake.calls == []


def test_execution_chain_required_stays_direct_despite_lower_confidence():
    """execution_chain_required (confidence=0.96) is intentionally below 1.0
    but must stay deterministic -- collecting a missing chain field never
    benefits from a model second opinion."""
    result = asyncio.run(resolve({"request": "buy BONK", "session_context": {}}, _never_called, embedding_factory=_never_called))
    assert result["missing_fields"] == ["chain"]


def test_uncertain_model_defers_to_a_strong_rule_and_never_to_embeddings():
    fake = _FakeEmbeddingRouter()
    call_lm = _model("research", confidence=0.4)
    result = asyncio.run(resolve({"request": "TVL on Solana", "session_context": {}}, call_lm, embedding_factory=lambda _m: fake))
    assert result["intent"] == "research" and result["capabilities"] == ["defi_data"]
    assert result["routing_decision"]["method"] == "rules"
    assert result["routing_decision"]["reason"] == "model_uncertain:defi"
    assert fake.calls == []


def test_uncertain_model_with_no_rule_asks():
    call_lm = _model("abstain", confidence=0.3)
    result = asyncio.run(resolve({"request": "Who owns the most of BONK", "session_context": {}}, call_lm, embedding_factory=_never_called))
    assert "clarification" in result


def test_unavailable_model_falls_back_to_the_embedding_tier():
    fake = _FakeEmbeddingRouter()

    async def down(*_a, **_k):
        raise RuntimeError("provider outage")

    result = asyncio.run(resolve({"request": "Who owns the most of BONK", "session_context": {}}, down, embedding_factory=lambda _m: fake))
    assert fake.calls == ["Who owns the most of BONK"]
    assert result["routing_decision"]["model_unavailable"] is True
    assert "clarification" in result  # the fake embedding abstains


def test_model_can_never_grant_execution_beyond_the_rules():
    """A confident 'quote' from the model still goes through plan_execution_route
    only with explicit_action on a crypto subject; otherwise it asks."""
    result = asyncio.run(resolve({"request": "could you get me some BONK", "session_context": {}},
                                 _model("quote", explicit=False, confidence=0.99), embedding_factory=_never_called))
    assert "clarification" in result


def test_classifier_result_is_cached_per_request_text():
    call_lm = _model("advice")
    for _ in range(3):
        asyncio.run(resolve({"request": "thoughts on HYPE?", "session_context": {}}, call_lm, embedding_factory=_never_called))
    assert call_lm.calls == ["thoughts on HYPE?"]
