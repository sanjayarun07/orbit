import asyncio

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


def _fake_embedding_factory(_model):
    return _FakeEmbeddingRouter()


async def _never_called(*_args, **_kwargs):
    raise AssertionError("call_lm should not be reached in these tests")


def test_open_question_catch_all_now_falls_through_to_the_embedding_tier():
    """Verified live before this fix: route_capabilities("Who owns the most
    of BONK") returns reason='open_question', confidence=0.6 -- previously
    treated identically to a confident rule match and never given a second
    opinion. Reused fake asserts the embedding tier is actually reached."""
    fake = _FakeEmbeddingRouter()
    state = {"request": "Who owns the most of BONK", "session_context": {}}
    result = asyncio.run(resolve(state, _never_called, embedding_factory=lambda _model: fake))
    assert fake.calls == ["Who owns the most of BONK"]
    # low_margin abstention -> a clarifying question, not a guessed route.
    assert "clarification" in result


def test_high_confidence_rule_match_is_unaffected_zero_embedding_calls():
    """Non-regression: the overwhelming majority of rules keep their default
    confidence=1.0 and must stay exactly as fast/deterministic as before --
    the embedding tier must never be reached for them."""
    fake = _FakeEmbeddingRouter()
    state = {
        "request": "Show top holders for 0x1111111111111111111111111111111111111111 token",
        "session_context": {},
    }
    result = asyncio.run(resolve(state, _never_called, embedding_factory=lambda _model: fake))
    assert fake.calls == []
    assert result["route_source"] == "rules"


def test_execution_chain_required_stays_direct_despite_lower_confidence():
    """execution_chain_required (confidence=0.96) is intentionally below 1.0
    but must stay above the 0.8 gate -- collecting a missing chain field is
    deterministic, not a case that benefits from a semantic second opinion."""
    fake = _FakeEmbeddingRouter()
    state = {"request": "buy BONK", "session_context": {}}
    result = asyncio.run(resolve(state, _never_called, embedding_factory=lambda _model: fake))
    assert fake.calls == []
    assert result["missing_fields"] == ["chain"]
