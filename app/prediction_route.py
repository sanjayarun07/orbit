"""Typed question contract and instrument eligibility for futures scenarios."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Literal

import dspy
import httpx
from pydantic import BaseModel, Field

from app import hedge_prediction

logger = logging.getLogger(__name__)

_FUTURES_DIRECTORY = "https://fapi.binance.com/fapi/v1/exchangeInfo"
_DIRECTORY_TTL = 600.0
_directory: tuple[float, frozenset[str]] | None = None
_directory_lock = threading.Lock()
_ADDRESS = re.compile(r"(?<![A-Za-z0-9])(?:0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})(?![A-Za-z0-9])")
_OTHER_VENUE = re.compile(r"\b(?:hyperliquid|aster|bybit|okx|coinbase|robinhood|kraken)\b", re.I)
_OTHER_CHAIN = re.compile(r"\bon\s+(?:the\s+)?(?:solana|base|ethereum|arbitrum|optimism|polygon|bsc|bnb\s+chain|avalanche|sui)\b", re.I)
_OTHER_MARKET = re.compile(r"\b(?:spot|options?|prediction\s+markets?|polymarket|equity|stock|analyst)\b", re.I)
_FUTURE_WINDOW = re.compile(r"\b(?:tomorrow|next\s+(?:\d+\s*)?(?:hours?|days?|weeks?)|later\s+this\s+week|by\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b", re.I)


class PredictionContract(BaseModel):
    kind: Literal["futures_scenario", "other"] = "other"
    symbol: str | None = None
    venue: str | None = None
    market: str | None = None
    side: Literal["long", "short"] | None = None
    horizons: list[Literal["4h", "24h", "3d", "7d"]] = Field(default_factory=list)
    ambiguity: str | None = None
    planner: Literal["model", "rules"] = "rules"

    def canonical_request(self) -> str:
        """A verified tool input; no model prose or user text enters the API."""
        parts = ["Predict", self.symbol or "", "on Binance futures"]
        if self.side:
            parts.append(self.side)
        if self.horizons:
            parts.extend(["for", " ".join(self.horizons)])
        return " ".join(parts)


class PlanPrediction(dspy.Signature):
    """Classify the user's CURRENT question, not earlier context.

    futures_scenario means the user asks for a directional or price scenario
    for ONE crypto instrument. It is not news, a live quote, a conceptual
    explanation, stock analyst target, options expiry, prediction-market odds,
    or a request to execute a trade. Extract only an asset actually named by
    the user; never invent a ticker. Preserve an explicit venue, spot/futures
    market, long/short side, and only explicitly stated supported horizons.
    Use other when the intent is not a futures scenario. Set ambiguity if two
    assets or the requested market cannot be distinguished.
    """

    request: str = dspy.InputField()
    contract: str = dspy.OutputField(desc='JSON {"kind":"futures_scenario|other","symbol":null,"venue":null,"market":null,"side":null,"horizons":[],"ambiguity":null}')


planner = dspy.Predict(PlanPrediction)


def _ask(request: str) -> str:
    from app.composition import split_notes
    return (split_notes(request)[0] or request or "").strip()


def candidate(request: str) -> bool:
    """Cheap *category* gate; the planner makes the actual routing decision."""
    ask = _ask(request)
    return (bool(hedge_prediction._FORECAST.search(ask))
            or bool(_FUTURE_WINDOW.search(ask) and _symbols(ask))) and not _ADDRESS.search(ask)


def _symbols(ask: str) -> list[str]:
    names = [m.group(1).removesuffix("USDT") for m in hedge_prediction._TICKER.finditer(ask)
             if m.group(1).removesuffix("USDT") not in hedge_prediction._IGNORE]
    for name, ticker in hedge_prediction._NAMES.items():
        if re.search(rf"\b{name}\b", ask, re.I):
            names.append(ticker)
    return list(dict.fromkeys(names))


def _rules(ask: str) -> PredictionContract:
    body = hedge_prediction.parse(ask)
    if not body:
        return PredictionContract()
    return PredictionContract(kind="futures_scenario", symbol=body["symbol"],
                              venue="binance" if re.search(r"\bbinance\b", ask, re.I) else None,
                              market="futures" if re.search(r"\b(?:futures?|perps?|perpetuals?)\b", ask, re.I) else None,
                              side=body.get("side"), horizons=_explicit_horizons(ask))


def _explicit_horizons(ask: str) -> list[str]:
    horizons = re.findall(r"\b(?:4h|24h|3d|7d)\b", ask, re.I)
    if re.search(r"\b(?:tomorrow|next\s+(?:24\s*hours?|day))\b", ask, re.I):
        horizons.append("24h")
    if re.search(r"\bnext\s+3\s+days?\b", ask, re.I):
        horizons.append("3d")
    if re.search(r"\bnext\s+(?:7\s+days?|week)\b", ask, re.I):
        horizons.append("7d")
    return list(dict.fromkeys(h.lower() for h in horizons))


def _model_contract(raw: str, ask: str) -> PredictionContract | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            return None
        # The model decides intent and identity. It cannot turn "rise" into
        # an explicitly requested long, invent a venue, or use a horizon the
        # API does not support. Those are read from the user's words below.
        data["side"] = None
        data["horizons"] = _explicit_horizons(ask)
        if not re.search(r"\bbinance\b", ask, re.I):
            data["venue"] = None
        if not re.search(r"\b(?:futures?|perps?|perpetuals?)\b", ask, re.I):
            data["market"] = None
        fields = PredictionContract.model_fields
        result = PredictionContract(**{k: v for k, v in data.items() if k in fields and v is not None}, planner="model")
    except Exception:
        logger.info("Prediction contract did not validate")
        return None
    if result.kind == "futures_scenario":
        # The model can classify intent; it cannot introduce an asset absent
        # from the user's words or change the explicitly named market.
        result.symbol = (result.symbol or "").upper().removesuffix("USDT") or None
        named_in_words = bool(result.symbol and re.search(rf"(?<![A-Za-z0-9])\$?{re.escape(result.symbol)}(?:USDT)?(?![A-Za-z0-9])", ask, re.I))
        if result.symbol not in _symbols(ask) and not named_in_words:
            return None
    return result


async def plan(request: str) -> PredictionContract | None:
    ask = _ask(request)
    if not candidate(ask):
        return None
    if _OTHER_MARKET.search(ask):
        return None
    from app.nodes import runtime
    result = _rules(ask)
    if runtime.planner_available():
        try:
            answer = await runtime._call_planner_lm(planner, request=ask)
            modelled = _model_contract(getattr(answer, "contract", ""), ask)
            if modelled is not None:
                result = modelled
        except Exception:
            logger.info("Prediction planner unavailable; using rules contract", exc_info=True)
    if result.kind != "futures_scenario":
        return None
    if _OTHER_VENUE.search(ask) or _OTHER_CHAIN.search(ask) or (result.venue and result.venue.lower() != "binance"):
        return None
    if result.market and result.market.lower() not in ("futures", "perpetuals", "perps"):
        return None
    if not result.symbol:
        return None
    # Explicit parameters win over a model that dropped or altered them.
    explicit = _rules(ask)
    if explicit.side:
        result.side = explicit.side
    if explicit.horizons:
        result.horizons = explicit.horizons
    return result


def listed_usdt_perpetual(symbol: str) -> bool | None:
    """True/False from Binance's live directory; None means it could not be read."""
    global _directory
    now = time.monotonic()
    with _directory_lock:
        if _directory and _directory[0] > now:
            return f"{symbol}USDT" in _directory[1]
    try:
        response = httpx.get(_FUTURES_DIRECTORY, timeout=8, headers={"User-Agent": "OrbitBackend/1.0"})
        response.raise_for_status()
        rows = response.json().get("symbols")
        if not isinstance(rows, list):
            return None
        names = frozenset(row["symbol"] for row in rows if isinstance(row, dict)
                          and row.get("status") == "TRADING" and row.get("contractType") == "PERPETUAL"
                          and row.get("quoteAsset") == "USDT" and isinstance(row.get("symbol"), str))
        if not names:
            return None
        with _directory_lock:
            _directory = (time.monotonic() + _DIRECTORY_TTL, names)
        return f"{symbol}USDT" in names
    except (httpx.HTTPError, ValueError, TypeError):
        logger.info("Binance futures directory unavailable", exc_info=True)
        return None
