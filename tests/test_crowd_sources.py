"""The crowd-source additions from the last30days review (2026-09-22): entity
grounding and an author cap in the tweet sample (app/x_tweets.py), Reddit
with real numbers (app/reddit_crowd.py), Polymarket odds (app/polymarket_odds.py)."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import polymarket_odds, reddit_crowd, x_tweets
from app.provider_router import NoData
from app.settings import settings

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _tweet(i, author="alice", likes=5, text="$BONK looks strong", minutes_ago=0):
    return x_tweets.normalize({"id": str(1000 + i), "text": text, "author": {"userName": author, "followers": 10}, "likeCount": likes, "retweetCount": 0,
                               "createdAt": (NOW - timedelta(minutes=minutes_ago)).strftime("%a %b %d %H:%M:%S +0000 %Y")})


# ---- 4. the tweet sample: grounding and the author cap ----

def test_a_tweet_must_actually_name_the_token():
    assert x_tweets.grounded({"text": "loading up on $BONK today"}, "BONK")
    assert x_tweets.grounded({"text": "#bonk to the moon"}, "BONK")
    assert x_tweets.grounded({"text": "Bonk Inu is back"}, "BONK", "Bonk Inu")
    assert not x_tweets.grounded({"text": "gonna bonk my head on this desk"}, "BONK")       # the verb, not the token
    assert not x_tweets.grounded({"text": "OPEN the door"}, "OPEN")


def test_stats_drop_ungrounded_tweets_and_cap_one_author(monkeypatch):
    rows = [_tweet(i, author="loud", likes=100 - i) for i in range(6)]                       # one account, six tweets
    rows += [_tweet(10 + i, author=f"u{i}", likes=1) for i in range(4)]
    rows += [_tweet(20, author="slang", likes=999, text="bonk bonk bonk on the head")]        # viral and off-topic
    s = x_tweets.stats(rows, symbol="BONK")
    assert s["dropped_ungrounded"] == 1 and s["capped_by_author"] == 3                        # 6 -> 3 for the loud account
    assert s["sample_size"] == 7 and s["unique_authors"] == 5
    assert all(x["author"] != "slang" for x in s["sample"])
    assert x_tweets.stats(rows)["dropped_ungrounded"] == 0                                    # without a symbol nothing is judged


# ---- 2. Reddit with real numbers ----

def _reddit_http(search_children, thread_comments, calls):
    class Response:
        def __init__(self, payload, status=200):
            self.payload, self.status_code = payload, status
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
        def post(self, url, data=None, auth=None):
            calls.append(("token", url))
            return Response({"access_token": "tok", "expires_in": 3600})
        def get(self, url, params=None):
            calls.append(("get", url))
            if "/search" in url:
                return Response({"data": {"children": search_children}})
            return Response([{}, {"data": {"children": thread_comments}}])
    return Client


def _post(i, sub="solana", score=100, comments=20, title="BONK just flipped", author="u1", text=""):
    return {"kind": "t3", "data": {"id": f"p{i}", "subreddit": sub, "title": title, "selftext": text, "author": author, "score": score, "upvote_ratio": 0.9,
                                   "num_comments": comments, "permalink": f"/r/{sub}/comments/p{i}/slug/", "created_utc": (NOW - timedelta(days=i)).timestamp()}}


def test_reddit_gathers_grounded_threads_enriches_the_most_discussed_and_measures(monkeypatch):
    monkeypatch.setattr(settings, "reddit_client_id", "id")
    monkeypatch.setattr(settings, "reddit_client_secret", "secret")
    calls = []
    children = [_post(0, score=500, comments=80), _post(1, sub="CryptoCurrency", score=40, comments=5, author="u2"),
                _post(2, title="Bonk my head", score=9000, comments=1, author="u3")]                 # ungrounded: the verb
    comments = [{"kind": "t1", "data": {"body": "Strong hands here", "score": 55, "author": "c1"}},
                {"kind": "t1", "data": {"body": "[removed]", "score": 999}}, {"kind": "t1", "data": {"body": "sticky", "score": 1, "stickied": True}},
                {"kind": "t1", "data": {"body": "dumping it", "score": 12, "author": "c2"}}]
    monkeypatch.setattr(reddit_crowd.httpx, "Client", _reddit_http(children, comments, calls))
    out = reddit_crowd.gather("BONK")
    assert out["query"] == '"$BONK" OR "BONK"'
    assert [p["id"] for p in out["posts"]] == ["p0", "p1"]                                    # the verb thread is out
    assert out["posts"][0]["top_comments"][0] == {"score": 55, "author": "c1", "text": "Strong hands here"}
    s = out["stats"]
    assert s["threads"] == 2 and s["unique_authors"] == 2 and s["total_upvotes"] == 540 and s["subreddits"] == ["solana", "CryptoCurrency"]
    assert s["sample"][0]["title"] == "BONK just flipped" and s["sample"][0]["top_comments"][0]["text"] == "Strong hands here"
    assert sum(1 for c in calls if c[0] == "token") == 1 and sum(1 for c in calls if c[0] == "get") == 3   # one search + two threads
    card = reddit_crowd.render_card("BONK", out)
    assert "**2 threads** by 2 authors in r/solana, r/CryptoCurrency" in card and "| r/solana | 500 | 80 | Strong hands here (+55) |" in card
    n = len(calls)
    reddit_crowd.gather("BONK")
    assert len(calls) == n                                                                    # cached


def test_reddit_card_says_no_data_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(settings, "reddit_client_id", "id")
    monkeypatch.setattr(settings, "reddit_client_secret", "secret")
    monkeypatch.setattr(reddit_crowd.httpx, "Client", _reddit_http([], [], []))
    with pytest.raises(NoData):
        reddit_crowd.reddit_card("what is reddit saying about WIF")
    assert reddit_crowd.REDDIT_ASK.search("what is reddit saying about WIF") and not reddit_crowd.REDDIT_ASK.search("what is CT saying about WIF")


# ---- 3. Polymarket odds ----

_EVENTS = {"events": [
    {"id": "1", "title": "Will Bitcoin hit $150k in 2026?", "slug": "btc-150k", "endDate": "2026-12-31T00:00:00Z", "volume": 4200000.0, "closed": False, "active": True,
     "markets": [{"question": "Will Bitcoin hit $150k in 2026?", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.27", "0.73"]', "volume": "4200000", "volume24hr": 51000.0, "closed": False, "endDate": "2026-12-31T00:00:00Z"}]},
    {"id": "2", "title": "Bitcoin above $80k on September 30?", "slug": "btc-80k-sep", "endDate": "2026-09-30T12:00:00Z", "volume": 900000.0, "closed": False, "active": True,
     "markets": [{"question": "Bitcoin above $80k on September 30?", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.81", "0.19"]', "volume": "900000", "volume24hr": 12000.0, "closed": False, "endDate": "2026-09-30T12:00:00Z"}]},
    {"id": "3", "title": "Will Ethereum flip Bitcoin?", "slug": "flippening", "endDate": "2026-12-31T00:00:00Z", "volume": 100.0, "closed": False, "active": True,
     "markets": [{"question": "Will Ethereum flip Bitcoin?", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.02", "0.98"]', "volume": "100", "closed": False}]},
    {"id": "4", "title": "Old resolved market", "slug": "old", "endDate": "2025-01-01T00:00:00Z", "volume": 5000000.0, "closed": True, "active": False, "markets": []},
]}


def _pm_http(payload, calls):
    class Response:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return payload

    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, url, params=None):
            calls.append(dict(params or {}))
            return Response()
    return Client


def test_polymarket_lists_open_markets_about_the_asset_by_money_at_stake(monkeypatch):
    calls = []
    monkeypatch.setattr(polymarket_odds.httpx, "Client", _pm_http(_EVENTS, calls))
    out = polymarket_odds.markets_for("BTC", "Bitcoin")
    assert calls[0]["q"] == "Bitcoin"
    assert [m["question"][:20] for m in out] == ["Will Bitcoin hit $15", "Bitcoin above $80k o"]    # open, about the asset, by volume; dust and closed dropped
    assert out[0]["yes"] == 0.27 and out[1]["yes"] == 0.81 and out[0]["volume"] == 4200000.0
    card = polymarket_odds.render_card("BTC", out)
    assert "| [Will Bitcoin hit $150k in 2026?](https://polymarket.com/event/btc-150k) | 27% | $4.20M | 2026-12-31 |" in card and "money-backed" in card.lower()
    assert polymarket_odds.render_card("BONK", []) is None


def test_polymarket_handler_needs_a_named_asset_and_reports_no_markets(monkeypatch):
    monkeypatch.setattr(polymarket_odds.httpx, "Client", _pm_http({"events": []}, []))
    with pytest.raises(NoData):
        polymarket_odds.polymarket_card("polymarket odds for BONK")
    assert polymarket_odds.matches("what are the polymarket odds on BTC") and polymarket_odds.matches("prediction market for ETH")
    assert not polymarket_odds.matches("price of BTC")
