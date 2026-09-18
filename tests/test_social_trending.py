"""Transcript of 2026-09-18: "Trending meme on twitter socials" was answered
with DEX Screener's narrative categories ranked by trading volume -- a market
table for a social question. `x_social_trending` is the social answer: what
crypto Twitter is posting about, market-wide, measured (LunarCrush) or
reported (web search over X posts, with accounts and links); the narratives
tool is kept away from social asks."""
import json
import sys
from pathlib import Path

import pytest

from app import social_sentiment
from app.settings import settings


@pytest.fixture(autouse=True)
def _fresh():
    social_sentiment.reset()
    yield
    social_sentiment.reset()


@pytest.mark.parametrize("text,expected", [
    ("Trending meme on twitter socials", True), ("what memecoins are trending on crypto twitter today", True), ("what is CT talking about", True),
    ("hot memes on X right now", True),
    ("what is CT saying about BONK", False),          # one token: x_kol_sentiment
    ("what narratives are trending", False),          # no social word: the market's narratives
    ("trending tokens on solana", False),
])
def test_the_market_wide_social_ask_is_recognised_and_a_token_ask_is_not(text, expected):
    assert social_sentiment.matches_trending(text) is expected


def test_the_web_path_renders_the_posts_as_a_ranked_table_with_accounts(monkeypatch):
    monkeypatch.setattr(settings, "lunarcrush_api_key", None)
    monkeypatch.setattr(settings, "perplexity_api_key", "pk-test")
    monkeypatch.setattr(social_sentiment, "perplexity_available", lambda: True)
    monkeypatch.setattr(social_sentiment, "perplexity_invoke", lambda tool, prompt, instructions: json.dumps({
        "as_of": "2026-09-18", "items": [
            {"symbol": "WIF", "name": "dogwifhat", "chain": "solana", "why": "hat meme revival after a whale buy", "accounts": ["@ansem", "@blknoiz06"], "url": "https://x.com/ansem/status/1", "stance": "bullish"},
            {"symbol": "PEPE", "chain": "ethereum", "why": "Pepe creator post", "accounts": ["@matt"], "url": "https://evil.example/phish", "stance": "mixed"}],
        "themes": ["hats", "frogs"]}))
    card = social_sentiment.social_trending("Trending meme on twitter socials")
    assert card.startswith("# Trending on crypto Twitter") and "Perplexity web search over X posts" in card
    assert "[WIF](https://x.com/ansem/status/1)" in card and "@ansem, @blknoiz06" in card
    assert "PEPE |" in card and "evil.example" not in card, "only x.com links are rendered as links"
    assert "**Themes**: hats · frogs" in card and "Reported, not measured" in card


def test_the_lunarcrush_path_is_preferred_when_a_key_is_set(monkeypatch):
    monkeypatch.setattr(settings, "lunarcrush_api_key", "lc-test")
    monkeypatch.setattr(social_sentiment, "_lc_get", lambda client, path: [
        {"symbol": "BONK", "name": "Bonk", "interactions_24h": 1234567, "social_dominance": 3.2, "galaxy_score": 71, "percent_change_24h": 4.5},
        {"symbol": "WIF", "name": "dogwifhat", "interactions_24h": 900000, "social_dominance": 2.1, "galaxy_score": 65, "percent_change_24h": -1.2}])
    called = []
    monkeypatch.setattr(social_sentiment, "perplexity_invoke", lambda *a, **k: called.append(a) or "{}")
    card = social_sentiment.social_trending("what is crypto twitter talking about")
    assert "LunarCrush API v4" in card and "| 1 | BONK (Bonk) | 1,234,567 |" in card and "+4.5%" in card
    assert not called, "measured data wins over a search when it is configured"


def test_the_social_ask_ranks_the_social_tool_first_and_the_narratives_table_last():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "routing_eval"))
    from harness import _ranked_tools
    ranked = _ranked_tools("Trending meme on twitter socials", semantic=False)
    assert ranked and ranked[3][0] == "x_social_trending", ranked
    assert "dexscreener_trending_metas" not in ranked[3][:2], "a market table is not the answer to a social question"
    token = _ranked_tools("what is CT saying about BONK", semantic=False)
    assert token and token[3][0] == "x_kol_sentiment"
