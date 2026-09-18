"""The trading desk on its own (user, 2026-09-18: "leave it as it is, make it
auto in the backend whenever required", then "remove it from UI").

An advice-shaped crypto ask about a named asset goes to the desk without
any switch; a factual lookup, a security check, an equity question, a
plain swap and a quick action do not. The session's team_mode (chat
phrase, MCP) still forces the desk for everything. The browser has no
toggle any more and sends no team_mode field.
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.routing import resolver
from app.routing.semantic import SpeechUnderstanding, embedding_router
from app.settings import settings

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _model(speech_act, domain="crypto", confidence=0.96):
    async def call_lm(program, **kwargs):
        return SimpleNamespace(understanding=SpeechUnderstanding(speech_act=speech_act, domain=domain, explicit_action=False, confidence=confidence))
    return call_lm


def _resolve(request, model, session_context=None):
    resolver._understanding_cache.clear()
    return asyncio.run(resolver.resolve({"request": request, "history": "", "session_context": session_context or {}}, model, embedding_factory=embedding_router))


@pytest.mark.parametrize("request_text", ["should I buy BONK here?", "thoughts on $WIF", "is WIF worth aping into"])
def test_an_opinion_about_an_asset_goes_to_the_desk_on_its_own(request_text):
    out = _resolve(request_text, _model("advice"))
    assert out["intent"] == "team" and out["team_subintent"] == "analysis", out
    assert out["routing_decision"]["team_auto"] is True


@pytest.mark.parametrize("request_text,speech_act", [
    ("OPEN token unlock schedule", "research"),
    ("who are the investors backing EigenLayer", "research"),
    ("what happened to HYPE", "research"),
    ("what's the price of BONK", "research"),
])
def test_a_factual_lookup_stays_on_the_single_path(request_text, speech_act):
    out = _resolve(request_text, _model(speech_act))
    assert out["intent"] != "team" and "team_auto" not in out["routing_decision"], out


def test_a_security_check_is_a_dossier_not_a_thesis():
    out = _resolve("is BONK safe to buy?", _model("advice"))
    assert out["intent"] != "team", out


def test_advice_with_no_asset_named_is_not_a_desk_turn():
    out = _resolve("should I be in crypto right now?", _model("advice"))
    assert out["intent"] != "team", out


def test_the_switch_still_forces_everything_and_the_setting_turns_auto_off(monkeypatch):
    out = _resolve("OPEN token unlock schedule", _model("research"), {"team_mode": True})
    assert out["intent"] == "team" and "team_auto" not in out["routing_decision"]
    monkeypatch.setattr(settings, "team_desk_auto", False)
    out = _resolve("should I buy BONK here?", _model("advice"))
    assert out["intent"] != "team"


def test_the_browser_has_no_desk_switch_and_sends_no_team_mode():
    html = (STATIC / "index.html").read_text()
    assert 'id="teamMode"' not in html and 'id="teamToggle"' not in html and "team_mode:teamModePending" not in html
    assert "function syncTeamMode(on){}" in html, "history and answers may still carry team_mode; the page ignores it"
    for sheet in ("chat.css", "product.css", "mobile.css"):
        assert ".team-toggle" not in (STATIC / sheet).read_text(), sheet


def test_the_desk_never_researches_a_decoy_listing(monkeypatch):
    """Live: "thoughts on $WIF" got a thesis on a Robinhood-chain WIF with
    $779M of listed liquidity and $10K of volume; dogwifhat lives on Solana.
    Robinhood Chain itself is no longer a mirror chain (its memecoins are
    real), so the decoy is caught by its volume, as the research resolver does."""
    from app.nodes import team

    monkeypatch.setattr(team, "search_verified_tokens", lambda q: [])
    monkeypatch.setattr(team, "token_candidates", lambda symbol, chains=(): [
        {"chain": "robinhood", "symbol": "WIF", "address": "0xmirror", "liquidity_usd": 778_900_000.0, "volume_24h_usd": 10_000.0},
        {"chain": "solana", "symbol": "WIF", "address": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", "liquidity_usd": 20_000_000.0, "volume_24h_usd": 90_000_000.0},
    ])
    assert asyncio.run(team._resolve_research_asset("thoughts on $WIF")) == ("EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", "solana")


def test_the_desk_asks_exactly_when_the_resolver_would(monkeypatch):
    """"Thoughts on MON?" used to be a 5/10 thesis on whichever MON the desk
    found; the research resolver's ambiguity question now comes first."""
    from app.nodes import research, team

    async def ambiguous(request, capabilities, user_chains=()):
        return research._TokenResolution(request, clarification="**MON** exists on several chains; which one do you mean?", pending={"symbol": "MON", "candidates": []})

    monkeypatch.setattr(team, "_resolve_named_token", ambiguous)
    out = asyncio.run(team.team_node({"request": "Thoughts on MON?", "team_subintent": "analysis", "chains": [], "session_context": {}}))
    assert out["answer"].startswith("**MON** exists on several chains") and out["pending_token"]["symbol"] == "MON" and out["intent"] == "research"


def test_a_question_about_me_names_no_asset_for_the_desk():
    """"given what you know about me, what should I look at today?" went to
    the desk because "about me" parsed as an asset called "me"."""
    from app.routing import resolver as _resolver
    update = {"intent": "research", "capabilities": ["web_research"], "route_source": "speech_model"}
    assert not _resolver._desk_wanted(update, {"speech_act": "advice", "domain": "crypto"}, "given what you know about me, what should I look at today?")
    assert not _resolver._desk_wanted(update, {"speech_act": "advice", "domain": "crypto"}, "what should I buy today?")
