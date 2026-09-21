"""The X sentiment analyst: TwitterAPI.io tweets cached by id (app/x_tweets.py),
deterministic sample statistics, Jev's typed judgement as a Signal, and the
three surfaces that read it (app/sentiment_analyst.py)."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import sentiment_analyst as sa, social_sentiment, token_deepdive, x_tweets
from app.settings import settings
from app.signals import Subject

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _tweet(i, author="alice", likes=5, text="BONK looks strong, accumulating", minutes_ago=0, verified=False):
    return {"id": str(1000 + i), "text": text, "author": {"userName": author, "followers": 1200, "isBlueVerified": verified},
            "likeCount": likes, "retweetCount": 1, "replyCount": 0, "createdAt": (NOW - timedelta(minutes=minutes_ago)).strftime("%a %b %d %H:%M:%S +0000 %Y")}


def _http(pages: list[dict], calls: list | None = None):
    """A stand-in for httpx.Client returning one prepared page per call."""
    class Response:
        def __init__(self, payload):
            self.payload = payload
            self.status_code = 200
            self.text = ""
        def raise_for_status(self):
            pass
        def json(self):
            return self.payload

    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, url, params=None, headers=None):
            (calls if calls is not None else []).append(dict(params or {}))
            return Response(pages.pop(0) if pages else {"tweets": [], "has_next_page": False})
        def post(self, url, json=None, headers=None):
            (calls if calls is not None else []).append(json)
            return Response(_JEV_ANSWER)
    return Client


_JEV_ANSWER = {"model": "jev-latest", "answers": {
    "stance": {"choice": "bullish", "probabilities": {"bullish": 0.7, "bearish": 0.1, "neutral": 0.2}, "confidence": 0.8},
    "mood": {"score": 3.2}, "catalyst": {"score": 1.0}, "organic": {"noul": 0.85}}}


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(settings, "twitterapi_io_key", "tw-key")
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-key")


# ---- tweets: query, normalisation, early stop, cache ----

def test_query_uses_the_cashtag_and_a_real_name_only():
    assert x_tweets.query_for("$bonk") == "$BONK lang:en -is:retweet min_faves:2"
    assert x_tweets.query_for("BONK", "Bonk Inu") == '($BONK OR "Bonk Inu") lang:en -is:retweet min_faves:2'
    assert x_tweets.query_for("WIF", "dogwifhat🎩") == "$WIF lang:en -is:retweet min_faves:2"


def test_fetch_pages_until_target_and_stops_at_a_known_tweet(keys, monkeypatch):
    calls = []
    first = [_tweet(i, minutes_ago=i) for i in range(20)]
    monkeypatch.setattr(x_tweets.httpx, "Client", _http([{"tweets": first, "has_next_page": True, "next_cursor": "c1"},
                                                          {"tweets": [_tweet(20 + i, minutes_ago=20 + i) for i in range(20)], "has_next_page": False}], calls))
    out = asyncio.run(x_tweets.fetch("BONK", 40))
    assert out["count"] == 40 and out["new"] == 40 and out["pages"] == 2 and calls[1]["cursor"] == "c1"
    # Second ask: page one repeats the newest tweets, so paging stops there and the store fills the rest.
    calls.clear()
    newer = [_tweet(99, minutes_ago=0, text="new one")] + first[:5]
    monkeypatch.setattr(x_tweets.httpx, "Client", _http([{"tweets": newer, "has_next_page": True, "next_cursor": "c2"}], calls))
    again = asyncio.run(x_tweets.fetch("BONK", 40))
    assert again["new"] == 1 and again["early_stop"] and again["pages"] == 1 and again["count"] == 40
    assert again["tweets"][0]["text"] == "new one"


def test_stats_measure_diversity_engagement_and_a_stratified_sample():
    rows = [x_tweets.normalize(_tweet(i, author=f"u{i % 3}", likes=i, minutes_ago=i, verified=(i == 1))) for i in range(30)]
    s = x_tweets.stats(rows)
    assert s["sample_size"] == 30 and s["unique_authors"] == 3 and s["author_diversity_pct"] == 10.0
    assert s["top_author_share_pct"] == pytest.approx(33.3, abs=0.1) and s["verified_share_pct"] == pytest.approx(3.3, abs=0.1)
    assert s["span_hours"] == pytest.approx(29 / 60, abs=0.02)
    kinds = [x["kind"] for x in s["sample"]]
    assert kinds.count("high_engagement") == 25 and kinds.count("latest") == 5      # 25 most liked, then the latest not already in
    assert s["sample"][0]["likes"] == 29
    assert x_tweets.stats([])["sample_size"] == 0


# ---- the judge and the Signal ----

def test_judge_asks_typed_questions_about_the_crowd_and_folds_the_answers(keys, monkeypatch):
    calls = []
    monkeypatch.setattr(sa.httpx, "Client", _http([], calls))
    stats = x_tweets.stats([x_tweets.normalize(_tweet(i)) for i in range(12)])
    j = sa.judge("BONK", stats)
    body = calls[0]
    assert set(body["questions"]) == {"stance", "mood", "catalyst", "organic"} and body["questions"]["stance"]["type"] == "choice"
    assert body["state"]["asset"] == "BONK" and "sample" not in body["state"]["social_stats"] and len(body["state"]["representative_tweets"]) == 12
    assert "trade" not in " ".join(q["instructions"].lower() for q in body["questions"].values())   # the crowd, never a trade action
    assert j["stance"] == "bullish" and j["mood"] == "Optimistic / bullish" and j["catalyst"] == "Rumour or minor update" and j["organic_probability"] == 0.85


def test_signal_is_the_stance_lean_halved_when_the_sample_is_a_push():
    subject = Subject(kind="token", id="DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", chain="solana", symbol="BONK")
    stats = {"sample_size": 40, "author_diversity_pct": 80.0}
    j = {"stance": "bullish", "stance_probabilities": {"bullish": 0.7, "bearish": 0.1, "neutral": 0.2}, "stance_confidence": 0.8,
         "mood": "Optimistic / bullish", "mood_score": 3.2, "catalyst": "No news, retail noise", "catalyst_score": 0.0, "organic_probability": 0.85}
    s = sa.to_signal(subject, NOW.isoformat(), stats, j)
    assert s.value == pytest.approx(0.6) and not s.abstained and s.components["p_bullish"] == 0.7 and "leans bullish on 40 tweets" in s.reasoning
    pushed = sa.to_signal(subject, NOW.isoformat(), stats, {**j, "organic_probability": 0.2})
    assert pushed.value == pytest.approx(0.3) and pushed.metadata["damped"] and "coordinated push" in pushed.reasoning
    assert sa.to_signal(subject, NOW.isoformat(), stats, None, "only 3 tweets").abstained


def test_analyze_abstains_on_a_thin_sample_and_caches_the_judgement(keys, monkeypatch):
    monkeypatch.setattr(x_tweets.httpx, "Client", _http([{"tweets": [_tweet(i) for i in range(4)], "has_next_page": False}]))
    out = asyncio.run(sa.analyze("WIF"))
    assert out["judgement"] is None and out["signal"].abstained and "only 4 tweets" in out["signal"].metadata["abstain_reason"]
    assert "_only 4 tweets" in out["card"]
    calls = []
    monkeypatch.setattr(x_tweets.httpx, "Client", _http([{"tweets": [_tweet(i, author=f"a{i}") for i in range(30)], "has_next_page": False}], calls))
    out = asyncio.run(sa.analyze("BONK"))
    assert out["judgement"]["stance"] == "bullish" and out["signal"].value == pytest.approx(0.6)
    assert "**Crowd stance: bullish** (bullish 70% · bearish 10% · neutral 20%)" in out["card"] and "30 distinct authors (100.0%)" in out["card"]
    n = len(calls)
    asyncio.run(sa.analyze("BONK"))
    assert len(calls) == n                                  # cached: no fetch, no judge


# ---- surfaces ----

def test_the_social_card_prefers_the_analyst_and_falls_back_when_it_abstains(keys, monkeypatch):
    monkeypatch.setattr(x_tweets.httpx, "Client", _http([{"tweets": [_tweet(i, author=f"a{i}") for i in range(30)], "has_next_page": False}]))
    monkeypatch.setattr(settings, "lunarcrush_api_key", None)
    monkeypatch.setattr(settings, "x_bearer_token", None)
    social_sentiment._cache.clear()
    card = social_sentiment.x_kol_sentiment("what is X saying about BONK")
    assert card.startswith("# X sentiment") and "Crowd stance: bullish" in card
    sa.reset_for_test(); social_sentiment._cache.clear(); x_tweets.reset_for_test()
    monkeypatch.setattr(x_tweets.httpx, "Client", _http([{"tweets": [], "has_next_page": False}]))
    monkeypatch.setattr(social_sentiment, "_from_perplexity", lambda symbol: "# Perplexity fallback")
    assert social_sentiment.x_kol_sentiment("what is CT saying about WIF") == "# Perplexity fallback"


def test_the_deep_dive_gains_the_dimension_and_the_vote(keys, monkeypatch):
    class _Router:
        def try_route(self, *a, **k):
            return None
        def try_route_across(self, *a, **k):
            return None
    monkeypatch.setattr(token_deepdive, "get_provider_router", lambda: _Router())
    monkeypatch.setattr(x_tweets.httpx, "Client", _http([{"tweets": [_tweet(i, author=f"a{i}") for i in range(30)], "has_next_page": False}]))
    bundle = asyncio.run(token_deepdive.build_token_evidence("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "solana", "BONK"))
    dim = next(d for d in bundle.dimensions if d.name == "x_sentiment")
    assert dim.status == "available" and dim.source == "x_sentiment_analyst"
    vote = next(s for s in bundle.signals if s.model_name == "x_sentiment")
    assert vote.value == pytest.approx(0.6) and vote.subject.symbol == "BONK"


def test_without_a_symbol_the_deep_dive_has_no_x_dimension(keys, monkeypatch):
    class _Router:
        def try_route(self, *a, **k):
            return None
        def try_route_across(self, *a, **k):
            return None
    monkeypatch.setattr(token_deepdive, "get_provider_router", lambda: _Router())
    bundle = asyncio.run(token_deepdive.build_token_evidence("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "solana"))
    assert "x_sentiment" not in {d.name for d in bundle.dimensions}
