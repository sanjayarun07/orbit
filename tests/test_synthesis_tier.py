"""The synthesis tier: a separately configurable model for the programs that
write an answer from gathered evidence, with a recorder for the eval."""
import asyncio
from types import SimpleNamespace

import pytest

from app.nodes import runtime


class _Transient(Exception):
    pass


def test_without_a_synthesis_model_the_primary_path_answers(monkeypatch):
    calls = []

    async def primary(program, **kwargs):
        calls.append(("primary", kwargs))
        return SimpleNamespace(answer="from primary")

    monkeypatch.setattr(runtime, "_synthesis_lm", None)
    monkeypatch.setattr(runtime, "_call_lm", primary)
    result = asyncio.run(runtime._call_synthesis_lm(runtime.knowledge_synthesizer, request="q", conversation_history="", passages="p"))
    assert result.answer == "from primary" and calls[0][1]["passages"] == "p"


def test_a_configured_synthesis_model_answers_and_a_transient_failure_falls_back(monkeypatch):
    attempts = []

    async def guarded(program, lm, kwargs):
        attempts.append(lm)
        if lm == "synthesis-lm":
            raise _Transient("rate limited")
        return SimpleNamespace(answer="from primary after fallback")

    async def primary(program, **kwargs):
        return await guarded(program, "primary-lm", kwargs)

    monkeypatch.setattr(runtime, "_synthesis_lm", "synthesis-lm")
    monkeypatch.setattr(runtime, "_run_guarded", guarded)
    monkeypatch.setattr(runtime, "_call_lm", primary)
    monkeypatch.setattr(runtime, "_is_transient_lm_error", lambda exc: isinstance(exc, _Transient))
    result = asyncio.run(runtime._call_synthesis_lm(runtime.token_deepdive_agent, request="r", evidence="e", analysis_rules="", user_context="", learned_lessons=""))
    assert attempts == ["synthesis-lm", "primary-lm"] and "fallback" in result.answer


def test_a_non_transient_failure_on_the_synthesis_model_surfaces(monkeypatch):
    async def guarded(program, lm, kwargs):
        raise ValueError("context length exceeded")

    monkeypatch.setattr(runtime, "_synthesis_lm", "synthesis-lm")
    monkeypatch.setattr(runtime, "_run_guarded", guarded)
    monkeypatch.setattr(runtime, "_is_transient_lm_error", lambda exc: False)
    with pytest.raises(ValueError):
        asyncio.run(runtime._call_synthesis_lm(runtime.knowledge_synthesizer, request="q", conversation_history="", passages="p"))


def test_the_recorder_sees_the_program_name_and_the_evidence_it_was_given(monkeypatch):
    seen = []

    async def primary(program, **kwargs):
        return SimpleNamespace(answer="x")

    monkeypatch.setattr(runtime, "_synthesis_lm", None)
    monkeypatch.setattr(runtime, "_call_lm", primary)
    monkeypatch.setattr(runtime, "synthesis_recorder", lambda name, kwargs: seen.append((name, kwargs)))
    asyncio.run(runtime._call_synthesis_lm(runtime.equity_research_synthesizer, request="COIN", conversation_history="", as_of_date="now",
                                           financial_evidence="fin", news_evidence="news"))
    assert seen == [("equity_research_synthesizer", {"request": "COIN", "conversation_history": "", "as_of_date": "now",
                                                     "financial_evidence": "fin", "news_evidence": "news"})]


def test_the_three_synthesis_call_sites_go_through_the_tier():
    import inspect
    from app.nodes import research
    source = inspect.getsource(research)
    # Each program is called through the tier: either the plain synthesis call
    # or runtime.answer(..., tier="synthesis"), which streams on that tier.
    for program in ("equity_research_synthesizer", "token_deepdive_agent", "knowledge_synthesizer"):
        tiered = (f"_call_synthesis_lm(\n        runtime.{program}" in source or f"_call_synthesis_lm(\n            runtime.{program}" in source
                  or f"_call_synthesis_lm(runtime.{program}" in source)
        streamed = (f'runtime.answer(\n        runtime.{program}, tier="synthesis"' in source or f'runtime.answer(\n            runtime.{program}, tier="synthesis"' in source
                    or f'runtime.answer(runtime.{program}, tier="synthesis"' in source)
        assert tiered or streamed, program
        assert f"_call_lm(runtime.{program}" not in source and f"_call_lm(\n        runtime.{program}" not in source and f"_call_lm(\n            runtime.{program}" not in source, program
        assert f"runtime.answer(runtime.{program}," not in source.replace(f'runtime.answer(runtime.{program}, tier="synthesis"', "") \
            and f"runtime.answer(\n            runtime.{program},\n" not in source and f"runtime.answer(\n        runtime.{program},\n" not in source, f"{program} streamed on the primary tier"
