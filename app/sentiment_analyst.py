"""The X sentiment analyst: Jev's typed questions over a tweet sample, as a Signal.

Taken from brainstormity/Jev-X-Sentiment-Analysis (user decision, 2026-09-21)
and reshaped to Orbit's rules: the judge answers typed questions about the
CROWD -- which way it leans, how it feels, whether there is a catalyst, and
whether the discussion reads as organic -- and never a trade action, a
price level or a confidence it did not compute. The stance's probability
distribution is the conviction: p(bullish) - p(bearish), in [-1, +1]. No
tweets, too few tweets, or a failed call is an abstain, not a neutral.

Three surfaces read it:
- the social sentiment card ("what is X saying about BONK"),
- the token deep-dive, as the `x_sentiment` dimension plus its vote on the
  receipt,
- the trading desk, as evidence under the market data.

One judge call per symbol per `social_sentiment_ttl_seconds`; the tweets
themselves are cached by id (app/x_tweets.py), so a re-ask costs the new
tweets and one Jev call at most.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app import x_tweets
from app.metrics import increment
from app.routing.jev_backend import JEV_URL, DEFAULT_MODEL, JevError, _api_key
from app.settings import settings
from app.signals import Signal, Subject

logger = logging.getLogger(__name__)

MODEL_NAME = "x_sentiment"
MOOD_LEVELS = ["Extreme fear / capitulation", "Cautious / bearish", "Mixed / neutral", "Optimistic / bullish", "Euphoric / greedy"]
CATALYST_LEVELS = ["No news, retail noise", "Rumour or minor update", "Moderate milestone or listing", "Major market-moving event"]
MIN_SAMPLE = 10
ORGANIC_DAMPING_BELOW = 0.4    # a sample that reads as a push carries half the weight

_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def enabled() -> bool:
    return x_tweets.enabled() and bool(getattr(settings, "typesafe_api_key", None))


def reset_for_test() -> None:
    with _lock:
        _cache.clear()


def questions(symbol: str) -> dict:
    return {
        "stance": {"type": "choice",
                   "instructions": (f"From `representative_tweets` and `social_stats`, which way is crypto X leaning on ${symbol} right now? "
                                    "Judge the crowd's stance from what the tweets say, not your own view of the asset. Sarcasm and mockery of "
                                    "the token count as bearish; mockery of doubters counts as bullish."),
                   "criteria": {"bullish": "Most weight of the sample expects the price to rise or is accumulating.",
                                "bearish": "Most weight expects the price to fall, calls it a scam or rug, or is exiting.",
                                "neutral": "Split, informational, or off-topic mentions with no lean."}},
        "mood": {"type": "score", "instructions": "Rate the prevailing emotional temperature of the sample.", "criteria": list(MOOD_LEVELS)},
        "catalyst": {"type": "score", "instructions": "Rate the significance of any concrete event, news or announcement the tweets describe (not price action itself).",
                     "criteria": list(CATALYST_LEVELS)},
        "organic": {"type": "noul", "instructions": ("Does the sample read as organic discussion by distinct people, rather than a coordinated push "
                                                     "(near-identical wording, the same few accounts, engagement farming)? Use `social_stats.author_diversity_pct` "
                                                     "and `top_author_share_pct` as evidence."),
                    "criteria": {"true": "Organic, diverse discussion", "false": "Coordinated, repetitive or bot-driven"}},
    }


def _post(body: dict, timeout: float) -> dict:
    with httpx.Client(timeout=timeout) as client:
        response = client.post(JEV_URL, json=body, headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"})
    if response.status_code != 200:
        raise JevError(f"Jev answered {response.status_code}: {response.text[:300]}")
    return response.json()


def judge(symbol: str, social_stats: dict, *, timeout: float = 25.0) -> dict:
    """One Jev call. Returns the typed answers folded into plain numbers."""
    state = {"asset": symbol, "social_stats": {k: v for k, v in social_stats.items() if k != "sample"},
             "representative_tweets": social_stats.get("sample") or []}
    t0 = time.perf_counter()
    data = _post({"state": state, "model": DEFAULT_MODEL, "questions": questions(symbol)}, timeout)
    answers = data.get("answers") or {}
    stance = answers.get("stance") or {}
    probs = {k: float(v) for k, v in (stance.get("probabilities") or {}).items()}
    mood = answers.get("mood") or {}
    catalyst = answers.get("catalyst") or {}
    organic = answers.get("organic") or {}
    if stance.get("choice") not in ("bullish", "bearish", "neutral"):
        raise JevError(f"stance not parsed: {stance.get('choice')!r}")
    increment("sentiment_jev_calls")
    return {
        "stance": stance["choice"], "stance_probabilities": probs, "stance_confidence": float(stance.get("confidence") or 0.0),
        "mood_score": float(mood.get("score") or 2.0), "mood": MOOD_LEVELS[min(max(0, int(round(float(mood.get("score") or 2.0)))), len(MOOD_LEVELS) - 1)],
        "catalyst_score": float(catalyst.get("score") or 0.0),
        "catalyst": CATALYST_LEVELS[min(max(0, int(round(float(catalyst.get("score") or 0.0)))), len(CATALYST_LEVELS) - 1)],
        "organic_probability": float(organic.get("noul") if organic.get("noul") is not None else 0.5),
        "model": data.get("model"), "latency_ms": round((time.perf_counter() - t0) * 1000), "usage": data.get("usage", {}),
    }


def to_signal(subject: Subject, as_of: str, social_stats: dict, judgement: dict | None, reason: str | None = None) -> Signal:
    """The analyst's vote. Conviction is the stance distribution's lean,
    p(bullish) - p(bearish), halved when the sample does not read as organic."""
    if judgement is None:
        return Signal.abstain(MODEL_NAME, subject, as_of, reason or "no judgement")
    probs = judgement.get("stance_probabilities") or {}
    lean = float(probs.get("bullish", 0.0)) - float(probs.get("bearish", 0.0))
    if not probs:
        lean = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}[judgement["stance"]] * float(judgement.get("stance_confidence") or 0.5)
    organic = float(judgement.get("organic_probability", 0.5))
    damped = organic < ORGANIC_DAMPING_BELOW
    value = max(-1.0, min(1.0, lean * (0.5 if damped else 1.0)))
    return Signal(
        model_name=MODEL_NAME, subject=subject, as_of=as_of, value=value,
        reasoning=(f"X leans {judgement['stance']} on {social_stats.get('sample_size', 0)} tweets ({social_stats.get('author_diversity_pct', 0)}% distinct authors); "
                   f"mood {judgement['mood'].lower()}; catalyst: {judgement['catalyst'].lower()}" + ("; sample reads as a coordinated push, weight halved" if damped else "")),
        components={"p_bullish": float(probs.get("bullish", 0.0)), "p_bearish": float(probs.get("bearish", 0.0)), "p_neutral": float(probs.get("neutral", 0.0)),
                    "mood_score": float(judgement["mood_score"]), "catalyst_score": float(judgement["catalyst_score"]), "organic_probability": organic,
                    "sample_size": float(social_stats.get("sample_size", 0)), "author_diversity_pct": float(social_stats.get("author_diversity_pct", 0.0))},
        metadata={"abstained": False, "stance": judgement["stance"], "mood": judgement["mood"], "catalyst": judgement["catalyst"], "damped": damped,
                  "source": "twitterapi.io + jev", "model": judgement.get("model")},
    )


def render_card(symbol: str, fetched: dict, social_stats: dict, judgement: dict | None, note: str | None = None) -> str:
    """The X sentiment card: numbers first, the judge's read, then the tweets
    that carried the most weight. Evidence, never a trade."""
    n = social_stats.get("sample_size", 0)
    lines = ["# X sentiment", f"**Source**: TwitterAPI.io search · Jev judge · **Token**: ${symbol} · **Checked**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", ""]
    if judgement is not None:
        probs = judgement.get("stance_probabilities") or {}
        lines += [f"**Crowd stance: {judgement['stance']}** "
                  f"(bullish {probs.get('bullish', 0) * 100:.0f}% · bearish {probs.get('bearish', 0) * 100:.0f}% · neutral {probs.get('neutral', 0) * 100:.0f}%) · "
                  f"**Mood**: {judgement['mood']} · **Catalyst**: {judgement['catalyst']} · **Organic**: {judgement['organic_probability'] * 100:.0f}%", ""]
        if judgement["organic_probability"] < ORGANIC_DAMPING_BELOW:
            lines += ["⚠ The sample reads as a coordinated push rather than organic discussion; weigh it accordingly.", ""]
    elif note:
        lines += [f"_{note}_", ""]
    lines += [f"**Sample**: {n} tweets" + (f" over {social_stats['span_hours']} h" if social_stats.get("span_hours") is not None else "") +
              f" · {social_stats.get('unique_authors', 0)} distinct authors ({social_stats.get('author_diversity_pct', 0)}%) · "
              f"busiest author {social_stats.get('top_author_share_pct', 0)}% of posts · verified {social_stats.get('verified_share_pct', 0)}% · "
              f"{social_stats.get('total_likes', 0):,} likes, {social_stats.get('total_retweets', 0):,} reposts · "
              f"{fetched.get('new', 0)} new since last check" + (" (stopped at a known tweet)" if fetched.get("early_stop") else ""), ""]
    sample = [s for s in (social_stats.get("sample") or []) if s.get("kind") == "high_engagement"][:6]
    if sample:
        lines += ["| Author | Likes | Says |", "|---|---:|---|"]
        for s in sample:
            text = (s.get("text") or "").replace("\n", " ").replace("|", "/")
            lines.append(f"| @{s.get('author') or '?'}{' ✓' if s.get('verified') else ''} | {s.get('likes', 0)} | {text[:140]} |")
        lines.append("")
    lines += [f"Query: `{fetched.get('query', '')}`. A crowd's lean is evidence about the crowd, not about the token; sentiment lags price as often as it leads it."]
    return "\n".join(lines)


async def analyze(symbol: str, *, subject: Subject | None = None, sample: int | None = None, name: str | None = None) -> dict:
    """Fetch, measure, judge. Returns {card, signal, stats, judgement, fetched};
    `signal` abstains with the reason when the judge could not run."""
    sym = symbol.strip().lstrip("$").upper()
    now = time.monotonic()
    with _lock:
        cached = _cache.get(sym)
        if cached and cached[0] > now:
            out = dict(cached[1])
            if subject is not None:
                out["signal"] = to_signal(subject, datetime.now(timezone.utc).isoformat(), out["stats"], out["judgement"], out.get("abstain_reason"))
            return out
    subject = subject or Subject(kind="token", id=sym, chain=None, symbol=sym)
    as_of = datetime.now(timezone.utc).isoformat()
    fetched = await x_tweets.fetch(sym, sample, name=name)
    social_stats = x_tweets.stats(fetched["tweets"])
    judgement, reason = None, None
    if social_stats["sample_size"] < MIN_SAMPLE:
        reason = f"only {social_stats['sample_size']} tweets matched ${sym} (need {MIN_SAMPLE})"
    else:
        try:
            judgement = await asyncio.to_thread(judge, sym, social_stats)
        except Exception as exc:
            reason = f"judge failed: {type(exc).__name__}"
            logger.warning("sentiment_analyst: Jev failed for %s", sym, exc_info=True)
    out = {"symbol": sym, "fetched": {k: v for k, v in fetched.items() if k != "tweets"}, "stats": social_stats, "judgement": judgement, "abstain_reason": reason,
           "card": render_card(sym, fetched, social_stats, judgement, reason), "signal": to_signal(subject, as_of, social_stats, judgement, reason)}
    with _lock:
        _cache[sym] = (now + max(60, settings.social_sentiment_ttl_seconds), out)
    return out


_loop: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """The app's event loop, so a synchronous router tool (run in a worker
    thread) can run `analyze` where the database pool lives."""
    global _loop
    _loop = loop


def analyze_blocking(symbol: str, timeout: float = 60.0, **kwargs) -> dict:
    """`analyze` from a thread without a running loop. Uses the app loop when
    one is registered (the pool is bound to it); otherwise a private loop,
    which is fine for the memory store and tests."""
    coro = analyze(symbol, **kwargs)
    if _loop is not None and _loop.is_running():
        return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout)
    return asyncio.run(coro)


class XSentimentModel:
    """AlphaModel: the X crowd's lean on a subject, as a Signal."""

    @property
    def name(self) -> str:
        return MODEL_NAME

    def predict(self, subject: Subject, as_of: str, data: Any = None) -> Signal:
        if not subject.symbol:
            return Signal.abstain(MODEL_NAME, subject, as_of, "no symbol to search X for")
        try:
            out = asyncio.run(analyze(subject.symbol, subject=subject)) if not asyncio.get_event_loop().is_running() else None
        except RuntimeError:
            out = None
        if out is None:
            return Signal.abstain(MODEL_NAME, subject, as_of, "call analyze() from async code")
        return out["signal"]
