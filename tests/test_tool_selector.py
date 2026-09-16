"""Model arbitration among already-qualified tools (app/routing/tool_selector.py).

Every property tested here backs one safety claim from the module docstring:
disabled/too-few-candidates is a free no-op, a valid pick reorders without
dropping any candidate, an invalid or errored pick falls back to the
deterministic order, and repeats are cached so a stress run doesn't multiply
real model calls.
"""
from types import SimpleNamespace

import pytest

from app.provider_router import ProviderTool
from app.routing import tool_selector
from app.settings import settings


def _tool(name, summary="", answers=(), not_for=()):
    spec = SimpleNamespace(summary=summary, answers=answers, not_answers=(), not_for=frozenset(not_for)) if summary else None
    return ProviderTool(name, "test", ("token_discovery",), lambda request: "ok", description=summary, spec=spec)


@pytest.fixture(autouse=True)
def _fresh_selector(monkeypatch):
    """Each test gets its own selector instance (own cache/circuit state) and starts disabled."""
    monkeypatch.setattr(settings, "llm_tool_selection_enabled", False)
    monkeypatch.setattr(tool_selector, "_selector", tool_selector.ToolSelector())


def _mock_arbiter(monkeypatch, chosen, reason="because", raises=None):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        if raises:
            raise raises
        return SimpleNamespace(chosen_tool=chosen, reason=reason)

    monkeypatch.setattr(tool_selector, "_arbiter", fake)
    monkeypatch.setattr(tool_selector.dspy, "context", lambda **kwargs: _NullCtx())
    return calls


class _NullCtx:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_disabled_is_a_free_no_op(monkeypatch):
    calls = _mock_arbiter(monkeypatch, "b")
    ranked = [_tool("a"), _tool("b")]
    assert tool_selector.select_tool("trending tokens", ranked) == ranked
    assert calls == []


def test_too_few_candidates_is_a_free_no_op(monkeypatch):
    monkeypatch.setattr(settings, "llm_tool_selection_enabled", True)
    calls = _mock_arbiter(monkeypatch, "a")
    ranked = [_tool("a")]
    assert tool_selector.select_tool("trending tokens", ranked) == ranked
    assert calls == []


def test_a_valid_pick_reorders_without_dropping_anyone(monkeypatch):
    monkeypatch.setattr(settings, "llm_tool_selection_enabled", True)
    _mock_arbiter(monkeypatch, "c", reason="ranks by volume")
    ranked = [_tool("a"), _tool("b"), _tool("c")]
    out = tool_selector.select_tool("trending tokens by volume", ranked)
    assert [t.name for t in out] == ["c", "a", "b"]


def test_agreeing_with_the_deterministic_top_pick_changes_nothing(monkeypatch):
    monkeypatch.setattr(settings, "llm_tool_selection_enabled", True)
    _mock_arbiter(monkeypatch, "a")
    ranked = [_tool("a"), _tool("b")]
    assert [t.name for t in tool_selector.select_tool("x", ranked)] == ["a", "b"]


def test_a_name_outside_the_shown_set_falls_back_to_deterministic_order(monkeypatch):
    monkeypatch.setattr(settings, "llm_tool_selection_enabled", True)
    _mock_arbiter(monkeypatch, "not_a_real_tool")
    ranked = [_tool("a"), _tool("b")]
    assert [t.name for t in tool_selector.select_tool("x", ranked)] == ["a", "b"]


def test_a_model_error_falls_back_to_deterministic_order(monkeypatch):
    monkeypatch.setattr(settings, "llm_tool_selection_enabled", True)
    _mock_arbiter(monkeypatch, "b", raises=RuntimeError("provider down"))
    ranked = [_tool("a"), _tool("b")]
    assert [t.name for t in tool_selector.select_tool("x", ranked)] == ["a", "b"]


def test_three_consecutive_errors_open_the_circuit(monkeypatch):
    monkeypatch.setattr(settings, "llm_tool_selection_enabled", True)
    calls = _mock_arbiter(monkeypatch, "b", raises=RuntimeError("down"))
    ranked = [_tool("a"), _tool("b")]
    for _ in range(3):
        tool_selector.select_tool(f"x{_}", ranked)          # distinct requests: no cache hit
    assert len(calls) == 3
    tool_selector.select_tool("x-after-open", ranked)        # circuit open: no fourth call
    assert len(calls) == 3


def test_repeat_requests_over_the_same_candidate_set_are_cached(monkeypatch):
    monkeypatch.setattr(settings, "llm_tool_selection_enabled", True)
    calls = _mock_arbiter(monkeypatch, "b")
    ranked = [_tool("a"), _tool("b")]
    tool_selector.select_tool("same wording", ranked)
    tool_selector.select_tool("same wording", ranked)
    tool_selector.select_tool("SAME   wording", ranked)       # case/whitespace-insensitive key
    assert len(calls) == 1


def test_only_the_capped_prefix_is_shown_to_the_model(monkeypatch):
    monkeypatch.setattr(settings, "llm_tool_selection_enabled", True)
    monkeypatch.setattr(settings, "tool_selector_max_candidates", 2)
    _mock_arbiter(monkeypatch, "a")
    ranked = [_tool("a"), _tool("b"), _tool("c")]
    out = tool_selector.select_tool("x", ranked)
    assert [t.name for t in out] == ["a", "b", "c"]           # c stays, just wasn't shown to the model


def test_disabled_by_default():
    assert settings.llm_tool_selection_enabled is False, "must ship OFF: an explicit opt-in pilot, not a silent default"
