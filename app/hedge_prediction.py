"""Read-only, explicitly scoped Binance futures scenario from OpenLedger Hedge."""

from __future__ import annotations

import json
import re
import time
from urllib.parse import urljoin, urlsplit

import httpx

from app import streaming
from app.settings import settings


_FORECAST = re.compile(r"\b(?:predict(?:ion)?|forecast|outlook|price\s+target|future\s+price|where\s+(?:(?:will|could|is)\s+\w+\s+(?:go|be|head|headed))|(?:will|could|might)\s+\w+\s+(?:(?:go|move)\s+(?:up|down)|rise|fall|rally|drop|dip|crash|pump|dump)|is\s+\w+\s+(?:bullish|bearish))\b", re.I)
_CONTRACT = re.compile(r"\b0x[0-9a-fA-F]{40}\b|\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
_TICKER = re.compile(r"(?<![\w$])\$?([A-Z][A-Z0-9]{1,9})(?:USDT)?(?!\w)")
_NAMES = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL"}
_IGNORE = {"USDT", "USD", "USDC", "ROI", "TP", "SL", "API", "AI"}
_HORIZONS = ("4h", "24h", "3d", "7d")


def enabled() -> bool:
    return bool(settings.hedge_api_key)


def parse(request: str) -> dict | None:
    """Only explicit asset forecasts; never infer an arbitrary mint's venue."""
    ask = (request or "").split("\nResolved from conversation context:", 1)[0]
    if not _FORECAST.search(ask) or _CONTRACT.search(ask):
        return None
    if re.search(r"\b(?:polymarket|prediction\s+market|election|weather)\b", ask, re.I):
        return None
    names = list(dict.fromkeys(m.group(1).removesuffix("USDT") for m in _TICKER.finditer(ask)
                               if m.group(1).removesuffix("USDT") not in _IGNORE))
    for name, ticker in _NAMES.items():
        if re.search(rf"\b{name}\b", ask, re.I) and ticker not in names:
            names.append(ticker)
    if len(names) != 1:
        return None
    body: dict = {"symbol": names[0]}
    side = re.search(r"\b(long|short)\b", ask, re.I)
    if side:
        body["side"] = side.group(1).lower()
    horizon = re.findall(r"\b(?:4h|24h|3d|7d)\b", ask, re.I)
    if horizon:
        body["horizons"] = list(dict.fromkeys(h.lower() for h in horizon))
    # Do not infer size, leverage, TP or SL from conversational numbers.
    return body


def matches(request: str) -> bool:
    return parse(request) is not None


def _poll_url(value: str, base: str) -> str:
    url = urljoin(base.rstrip("/") + "/", value)
    parsed, expected = urlsplit(url), urlsplit(base)
    if (parsed.scheme != "https" or parsed.netloc != expected.netloc
            or not parsed.path.startswith(expected.path.rstrip("/") + "/v1/predict/")):
        raise ValueError("Prediction service returned an invalid poll URL")
    return url


def _request(client: httpx.Client, method: str, url: str, **kwargs) -> dict:
    response = client.request(method, url, **kwargs)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Prediction service returned an invalid response")
    return data


def _number(value: object, suffix: str = "") -> str:
    try:
        return f"{float(value):,.2f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def _render(data: dict) -> str:
    inp, result = data.get("input") or {}, data.get("result") or {}
    verdict, plan, scores = result.get("verdict") or {}, result.get("plan") or {}, result.get("scores") or {}
    if not isinstance(inp, dict) or not isinstance(result, dict) or not isinstance(verdict, dict):
        raise ValueError("Prediction result has an invalid shape")
    symbol = str(inp.get("symbol") or "unknown")
    exchange = str(inp.get("exchange") or "unknown")
    market = str(inp.get("market") or "unknown")
    horizons = ", ".join(map(str, inp.get("horizons") or [])) or "unspecified"
    defaults = ", ".join(map(str, data.get("defaultsApplied") or [])) or "none"
    lines = [
        f"### {symbol} · futures scenario",
        f"**Market:** {exchange} {market} · **As of:** {data.get('generatedAt') or 'not provided'}",
        f"**Assumptions:** {inp.get('side', 'unspecified')} · ${_number(inp.get('notionalUsd'))} notional · {_number(inp.get('leverage'))}× leverage · horizons {horizons}",
        f"**Service signal:** {verdict.get('signal') or 'unavailable'} · **reported confidence:** {_number(verdict.get('confidence'), '%')}",
    ]
    if verdict.get("summary"):
        lines.append(str(verdict["summary"]))
    if isinstance(plan, dict) and plan:
        lines.append(f"**Scenario entry:** ${_number(plan.get('entry'))} · **expected ROI:** {_number(plan.get('expectedRoiPct'), '%')}")
    if isinstance(scores, dict) and scores:
        numeric_scores = [(key, value) for key, value in scores.items() if isinstance(value, (int, float)) and not isinstance(value, bool)]
        if numeric_scores:
            lines.append("**Factor scores:** " + ", ".join(f"{key} {_number(value)}" for key, value in numeric_scores))
    lines += [f"**API defaults applied:** {defaults}.",
              "This is a model scenario for a leveraged futures position, not a neutral price forecast or an executable quote. "
              "The signal, confidence and expected ROI are the service's estimates; losses can exceed the displayed scenario. No trade was prepared or placed."]
    return "\n\n".join(lines)


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if -1e15 < number < 1e15 else None
    except (TypeError, ValueError):
        return None


def compact_card(data: dict) -> dict:
    """Small, allowlisted UI payload derived only from this completed API run."""
    inp, result = data.get("input") or {}, data.get("result") or {}
    verdict, forecast, plan, market, scores = (result.get(name) or {} for name in ("verdict", "forecast", "plan", "market", "scores"))
    if not all(isinstance(part, dict) for part in (inp, result, verdict, forecast, plan, market, scores)):
        raise ValueError("Prediction result has an invalid shape")
    horizon_data = forecast.get("horizons")
    factor_data = verdict.get("keyFactors")
    risk_data = verdict.get("risks")
    requested_horizons = inp.get("horizons")
    defaults_applied = data.get("defaultsApplied")
    horizons = []
    for row in (horizon_data if isinstance(horizon_data, list) else [])[:8]:
        if not isinstance(row, dict) or str(row.get("label")) not in {"1h", "4h", "12h", "24h", "3d", "7d"}:
            continue
        horizons.append({"label": str(row["label"]), **{key: _finite_number(row.get(key)) for key in
                         ("p10", "p25", "p50", "p75", "p90", "upProbPct", "expectedMovePct")}})
    factors = []
    for row in (factor_data if isinstance(factor_data, list) else [])[:7]:
        if not isinstance(row, dict):
            continue
        impact = str(row.get("impact") or "neutral").lower()
        factors.append({"label": str(row.get("label") or "Factor")[:90],
                        "impact": impact if impact in {"bullish", "bearish", "neutral"} else "neutral",
                        "weight": _finite_number(row.get("weight")), "detail": str(row.get("detail") or "")[:280]})
    probabilities = plan.get("probabilities") or {}
    if not isinstance(probabilities, dict):
        probabilities = {}
    return {
        "kind": "futures_scenario_v1", "symbol": str(inp.get("symbol") or "")[:24],
        "exchange": str(inp.get("exchange") or "")[:30], "market": str(inp.get("market") or "")[:30],
        "generated_at": str(data.get("generatedAt") or "")[:40],
        "input": {"side": str(inp.get("side") or "")[:12], "notional_usd": _finite_number(inp.get("notionalUsd")),
                  "leverage": _finite_number(inp.get("leverage")), "horizons": [str(h)[:8] for h in (requested_horizons if isinstance(requested_horizons, list) else [])[:8]],
                  "defaults": [str(h)[:30] for h in (defaults_applied if isinstance(defaults_applied, list) else [])[:16]]},
        "verdict": {"signal": str(verdict.get("signal") or "unavailable")[:30], "confidence": _finite_number(verdict.get("confidence")),
                    "summary": str(verdict.get("summary") or "")[:700],
                    "risks": [str(s)[:300] for s in (risk_data if isinstance(risk_data, list) else [])[:3]], "factors": factors},
        "forecast": {"method": str(forecast.get("method") or "")[:140], "horizons": horizons},
        "scores": {k: _finite_number(scores.get(k)) for k in ("technical", "momentum", "derivatives", "orderbook", "sentiment", "fundamentals", "forecast")},
        "plan": {"entry": _finite_number(plan.get("entry")), "expected_roi_pct": _finite_number(plan.get("expectedRoiPct")),
                 "expected_pnl_usd": _finite_number(plan.get("expectedPnlUsd")), "margin_usd": _finite_number(plan.get("marginUsd")),
                 "probabilities": {k: _finite_number(probabilities.get(k)) for k in ("tpFirstPct", "slFirstPct", "liquidationPct", "openAtHorizonPct")}},
        "market_snapshot": {k: _finite_number(market.get(k)) for k in ("price", "markPrice", "change24hPct", "volume24hQuote", "fundingRatePct", "openInterestUsd")},
        "llm_used": bool((result.get("llm") or {}).get("used")) if isinstance(result.get("llm") or {}, dict) else False,
    }


def _fetch(request: str) -> dict:
    body = parse(request)
    if body is None:
        raise ValueError("Name one asset and ask for a Binance futures forecast")
    if not enabled():
        raise RuntimeError("Prediction service is not configured")
    base = settings.hedge_base_url.rstrip("/")
    if urlsplit(base).scheme != "https":
        raise ValueError("Prediction service requires HTTPS")
    deadline = time.monotonic() + settings.hedge_timeout_seconds
    headers = {"Authorization": f"Bearer {settings.hedge_api_key}", "Accept": "application/json", "User-Agent": "OrbitBackend/1.0"}
    streaming.emit("status", text=f"Running {body['symbol']} futures scenario")
    with httpx.Client(headers=headers, timeout=min(settings.hedge_timeout_seconds, 100)) as client:
        data = _request(client, "POST", f"{base}/v1/predict", params={"wait": settings.hedge_wait_seconds, "include": "none"}, json=body)
        while data.get("status") not in ("done", "error"):
            poll = data.get("pollUrl")
            if not poll or time.monotonic() >= deadline:
                raise TimeoutError("Prediction analysis did not finish within the configured timeout")
            streaming.emit("status", text=f"Analyzing {body['symbol']} · {data.get('progressPct', 0)}%")
            time.sleep(min(2, max(0, deadline - time.monotonic())))
            data = _request(client, "GET", _poll_url(str(poll), base), timeout=min(30, max(1, deadline - time.monotonic())))
    if data.get("status") == "error":
        raise RuntimeError("Prediction analysis failed")
    return data


def predict(request: str) -> str:
    """Plain-text consumer of the same read-only analysis."""
    return _render(_fetch(request))


def predict_tool(request: str) -> str:
    """Router output carries prose plus a bounded card, never the raw 56 KB run."""
    data = _fetch(request)
    return json.dumps({"answer": _render(data), "card": compact_card(data)}, separators=(",", ":"))
