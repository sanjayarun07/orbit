"""The twenty-prompt live run of 2026-09-18 (reports/uncertain-live-20),
turned into tests. Each case here is one of that run's failures with the
evidence it captured: the probe result, the web context, the answer.

- Mercury: probe "concept", web answer about the planet -> a question, not
  an astronomy summary.
- TRUMP: probe the Solana memecoin, web answer the politician -> the token
  route stands, the web card is dropped.
- "the unlock next month": the web asks which unlock -> the turn asks;
  the calendar never supplies SUI.
- FARTCOIN: "what's happening with X" is about X, not the macro calendar.
- ANSEM / RENDER: a summary never calls a token audited on a Shield check.
- OPEN / M: a web question about a ticker-like name is scoped to markets.
- Compare OPEN and MOVE: a clarification grows no related questions;
  follow-ups never speculate or address the user.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app import clarify, composition, followups
from app.nodes import research
from app.routing import resolver, subject_probe
from app.routing.semantic import embedding_router
from app.settings import settings


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    subject_probe.reset()
    resolver._understanding_cache.clear()
    monkeypatch.setattr(subject_probe, "perplexity_available", lambda: True)
    monkeypatch.setattr(subject_probe, "perplexity_finance_search", lambda q: None)
    monkeypatch.setattr(subject_probe, "perplexity_web_search", lambda q: None)
    yield
    subject_probe.reset()


def _abstaining_model():
    async def model(program, **kwargs):
        return SimpleNamespace(understanding={"speech_act": "abstain", "domain": "general", "explicit_action": False, "confidence": 0.3})
    return model


MERCURY_PROBE = {"kind": "concept", "name": "Mercury retrograde", "symbol": None, "chain": None, "confidence": 0.82,
                 "summary": "Mercury most likely refers to the astrological Mercury retrograde framework; a separate ambiguity is Mercury Protocol or Mercury Systems (NASDAQ: MRCY). Sources: https://x"}
MERCURY_WEB = "If you mean **Mercury's visibility in the sky**:\n\n- Tonight, Friday, September 18, 2026: Mercury is a difficult evening target, very low in the west shortly after sunset, at about magnitude -0.2. It sets roughly 40 minutes after the Sun."
TRUMP_PROBE = {"kind": "token", "name": "OFFICIAL TRUMP", "symbol": "TRUMP", "chain": "solana", "confidence": 0.99, "summary": "OFFICIAL TRUMP is a Solana SPL memecoin launched on January 17, 2025."}
TRUMP_WEB = ("“TRUMP” most commonly refers to **Donald J. Trump**, the U.S. politician and businessman.\n\n- He is the 45th and 47th president of the United States, "
             "having served first from January 20, 2017, to January 20, 2021, and returning to office on January 20, 2025 after winning the 2024 election.")
UNLOCK_WEB = ("I need to know what “the unlock” refers to. Do you mean a token/crypto vesting unlock, an employee stock lockup, a phone/SIM unlock, or something else? "
              "If you share the asset/company/project name, I can check the date and details for next month.")


# --- clarification and market-text detection -----------------------------------

@pytest.mark.parametrize("text,expected", [
    (UNLOCK_WEB, True),
    ("Which token's unlock? Name it (a $ticker, the project name or its contract address) and I'll pull its vesting schedule.", True),
    ("I can compare them, but I don't have any instrument data yet in this turn.\n\nPlease send one of these:\n- the full names for **OPEN** and **MOVE**, or\n- the exact tickers.\n\n_Not financial advice._", True),
    ("What does “M” refer to—an app, medication, person, product, place, or drug?\n\nIf you mean **M Safe 50 mg**, it is not universally safe.", True),
    ("**BONK** exists on several chains with comparable liquidity, so I don't want to guess which one you mean:\n\n- BONK on solana\n\nReply with the chain.", True),
    ("Assuming you mean OpenLedger (OPEN), its unlock schedule has a major event in September 2026: the 12-month cliff ends.", False),
    ("# Unlock schedule — ARB\n| 2026-10-15 | 56,125,000 tokens | cliff | insiders |", False),
])
def test_a_clarification_is_recognised_by_its_shape(text, expected):
    assert clarify.is_clarification(text) is expected


def test_market_text_is_about_a_token_protocol_company_or_market():
    assert clarify.is_market_text(TRUMP_PROBE["summary"])
    assert clarify.is_market_text("OpenLedger (OPEN) is a BNB Chain token; 1B total supply, 12-month cliff then 36 months of monthly vesting.")
    assert not clarify.is_market_text(MERCURY_WEB)
    assert not clarify.is_market_text(TRUMP_WEB)


# --- the uncertain branch: one subject per turn, clarification is terminal ---------

def test_mercury_asks_instead_of_summarising_the_sky(monkeypatch):
    monkeypatch.setattr(subject_probe, "perplexity_invoke", lambda tool, prompt, instructions: json.dumps(MERCURY_PROBE))
    monkeypatch.setattr(subject_probe, "perplexity_web_search", lambda q: MERCURY_WEB)
    out = asyncio.run(resolver.resolve({"request": "Mercury outlook", "history": "", "session_context": {}}, _abstaining_model(), embedding_factory=embedding_router))
    assert out["intent"] == "general" and out.get("clarification") and "web_context" not in out
    assert "**Mercury**" in out["clarification"] and "Mercury retrograde" in out["clarification"] and "Mercury Systems" in out["clarification"]
    assert "Sources:" not in out["clarification"] and "name it" in out["clarification"]
    assert out["routing_decision"]["reason"] == "model_uncertain:probe_concept:off_market"


def test_trump_keeps_the_token_route_and_drops_the_politician(monkeypatch):
    monkeypatch.setattr(subject_probe, "perplexity_invoke", lambda tool, prompt, instructions: json.dumps(TRUMP_PROBE))
    monkeypatch.setattr(subject_probe, "perplexity_web_search", lambda q: TRUMP_WEB)
    out = asyncio.run(resolver.resolve({"request": "Tell me about TRUMP", "history": "", "session_context": {}}, _abstaining_model(), embedding_factory=embedding_router))
    assert out["intent"] == "research" and out["chains"] == ["solana"] and "token_security" in out["capabilities"]
    assert "web_context" not in out, "two subjects never share a turn"
    assert out["routing_decision"]["web_context"] == "dropped:off_subject"


def test_an_agreeing_web_answer_still_rides_along(monkeypatch):
    monkeypatch.setattr(subject_probe, "perplexity_invoke", lambda tool, prompt, instructions: json.dumps(TRUMP_PROBE))
    monkeypatch.setattr(subject_probe, "perplexity_web_search", lambda q: "OFFICIAL TRUMP (TRUMP) is a Solana memecoin; price about $2, market cap $550M, 1B supply, traded on Meteora and Raydium.")
    out = asyncio.run(resolver.resolve({"request": "Tell me about TRUMP", "history": "", "session_context": {}}, _abstaining_model(), embedding_factory=embedding_router))
    assert out["web_context"].startswith("OFFICIAL TRUMP") and out["routing_decision"]["web_context"] is True


def test_the_webs_own_question_ends_the_turn_as_a_question(monkeypatch):
    monkeypatch.setattr(subject_probe, "perplexity_invoke", lambda tool, prompt, instructions: "no idea")
    monkeypatch.setattr(subject_probe, "perplexity_web_search", lambda q: UNLOCK_WEB)
    out = asyncio.run(resolver.resolve({"request": "What about the unlock next month?", "history": "", "session_context": {}}, _abstaining_model(), embedding_factory=embedding_router))
    assert out["intent"] == "general" and out.get("clarification") and "web_context" not in out
    assert out["routing_decision"]["reason"].endswith(":web_asks")


# --- the research node: calendar and unlock gates, no merge onto a question ------

def test_a_question_that_names_a_token_is_not_a_calendar_question():
    assert research._calendar_ask("what events are coming this week?")
    assert research._calendar_ask("upcoming unlocks next month")
    assert not research._calendar_ask("What's happening with FARTCOIN?")
    assert not research._calendar_ask("what's happening with $BONK this week")
    assert not research._calendar_ask("What about the unlock next month for JUP?")


def test_an_unlock_ask_with_no_token_asks_or_takes_the_focus(monkeypatch):
    async def never(*a, **k):
        raise AssertionError("no tool should run")

    monkeypatch.setattr(research, "_resolve_named_token", never)
    out = asyncio.run(research._research_node({"request": "What about the unlock next month?", "capabilities": ["web_research"], "chains": [], "session_context": {}}, {}))
    assert out["answer"].startswith("Which token's unlock?") and out["trajectory"] is None

    seen = {}

    async def resolve(request, capabilities, user_chains=()):
        seen["request"] = request
        raise RuntimeError("stop here")

    monkeypatch.setattr(research, "_resolve_named_token", resolve)
    state = {"request": "What about the unlock next month?", "capabilities": ["web_research"], "chains": [],
             "session_context": {"focus": {"kind": "token", "label": "JUP", "address": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", "chain": "solana"}}}
    with pytest.raises(RuntimeError):
        asyncio.run(research._research_node(state, {}))
    assert seen["request"].startswith("JUP What about the unlock next month? JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN on solana")


def test_a_web_card_is_never_merged_onto_a_clarifying_answer(monkeypatch):
    async def asks(state, sink):
        return {"answer": "Which token's unlock? Name it and I'll pull its vesting schedule.", "trajectory": None}

    called = []

    async def synthesize(*a, **k):
        called.append(1)
        return "never"

    monkeypatch.setattr(research, "_research_node", asks)
    monkeypatch.setattr(composition, "synthesize", synthesize)
    out = asyncio.run(research.research_node({"request": "What about the unlock next month?", "capabilities": ["web_research"], "chains": [],
                                              "web_context": "The SUI unlock on October 1 releases 21.7M tokens.", "session_context": {}}))
    assert out["answer"].startswith("Which token's unlock?") and not called


def test_a_web_question_about_a_ticker_like_name_is_scoped_to_markets():
    scoped = research._market_scoped("What is OPEN?")
    assert scoped.startswith("What is OPEN?") and 'Read "OPEN" as a token, protocol, company or ticker' in scoped
    assert 'Read "M" as a token' in research._market_scoped("Is M safe?")
    assert 'Read "ARC" as' in research._market_scoped("Research ARC")
    assert research._market_scoped("what is the best way to bridge to base") == "what is the best way to bridge to base"


# --- the summary never upgrades a verification into an audit ---------------------

SHIELD_CARD = ("# Token security — RENDER\n**Verified**: yes (Jupiter strict list) · **Shield warnings**: none\n\n"
               "Verification and Shield checks are listing and safety signals, not an audit.")


def test_an_audit_claim_without_an_audit_report_is_corrected_in_front():
    summary = "RENDER is verified and audited by Jupiter with no Shield warnings, so it passes the standard checks."
    out = composition.audit_guard(summary, SHIELD_CARD)
    assert out.startswith("**No audit report was found in the evidence.**") and summary in out
    assert composition.audit_guard("RENDER is verified on Jupiter with no Shield warnings.", SHIELD_CARD) == "RENDER is verified on Jupiter with no Shield warnings."
    audited = SHIELD_CARD + "\n\n---\n\n# Audits\nAudit report by OtterSec, March 2025: no critical findings."
    assert composition.audit_guard(summary, audited) == summary


def test_the_synthesis_applies_the_guard(monkeypatch):
    from app.nodes import runtime

    async def lm(program, **kwargs):
        return SimpleNamespace(summary="ANSEM is audited by Jupiter and safe.")

    monkeypatch.setattr(runtime, "_call_synthesis_lm", lm)
    out = asyncio.run(composition.synthesize("Audit report on ANSEM", SHIELD_CARD, {"tool_name_0": "solana_token_security"}))
    assert "**No audit report was found in the evidence.**" in out and "ANSEM is audited by Jupiter" in out


# --- related questions: factual, never after a clarification -------------------------

ANSWER = ("The primary unlock event next month is scheduled for October 1, when approximately 21.7 million SUI tokens, representing about 0.2% of the maximum supply, "
          "are set for release (per the market events calendar). The September jobs report follows on October 2 and GDP data on September 30.")


@pytest.mark.parametrize("line", [
    "What potential impact could the October 1 SUI token unlock have on the SUI market price and trading volume?",
    "Are there any measures in place by the Sui project to mitigate sell pressure following the October unlock?",
    "Could the proximity of the September jobs report influence trader sentiment around the SUI token unlock?",
    "Do you want a comparison of SUI based on current price and market capitalization?",
    "Would you prefer a comparison focusing on the business model of SUI?",
])
def test_speculative_and_user_directed_follow_ups_are_dropped(line):
    assert not followups.grounded(line, "What about the unlock next month?", ANSWER)


def test_factual_follow_ups_still_pass():
    assert followups.grounded("How many SUI tokens were released in the September 2026 unlock?", "SUI unlock", ANSWER)
    assert followups.grounded("What is the maximum supply of SUI?", "SUI unlock", ANSWER)


def test_a_clarifying_answer_grows_no_related_questions(monkeypatch):
    monkeypatch.setattr(settings, "followups_enabled", True)
    compare = ("I can compare them, but I don't have any instrument data yet in this turn.\n\nPlease send one of these:\n- the full names for **OPEN** and **MOVE**, or\n"
               "- the exact tickers plus whether you want **price/valuation**, **business profile**, or **financials**.\n\nIf you want, I can compare them on:\n- current price and market cap\n"
               "- business model and sector\n- revenue, profitability, and growth\n\n_This is general information, not financial advice._")
    assert not followups.eligible("research", compare, None)
    assert not followups.eligible("research", "What does “M” refer to—an app, medication, person, product, place, or drug?\n\n" + "If you mean M Safe, it is not universally safe. " * 8, None)
