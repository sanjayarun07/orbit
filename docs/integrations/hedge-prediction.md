# OpenLedger Hedge prediction

Orbit can answer explicit crypto price-forecast requests using the read-only
`hedge_token_prediction` tool. It calls `POST /api/v1/predict?wait=90&include=none`
and polls the returned job URL when analysis is still running. The poll URL is
restricted to the configured HTTPS API origin and prediction path.

Set `HEDGE_API_KEY` in the local `.env` or deployment secret store. Optional
settings are `HEDGE_BASE_URL`, `HEDGE_WAIT_SECONDS`, and
`HEDGE_TIMEOUT_SECONDS`. Never put the key in browser code or a tracked file.
The current local `.env` is ignored by Git. Restart the app after changing
settings.

Example prompts: “Predict SOL”, “Will BTC rise tomorrow?”, “Forecast BTCUSDT
short for 24h”, “Ethereum futures outlook”. A typed question contract selects
the futures-scenario intent and named asset; Binance's live USD-M futures
directory must confirm an active USDT perpetual before the API is called.
Explicit side and supported horizons are preserved. The tool does not resolve contract addresses to futures
contracts, infer position size from prose, answer prediction-market odds, or
place trades. Unsupported questions continue through normal research/tape.

The answer labels the exchange, futures instrument, timestamp, side, notional,
leverage, horizons, model signal and reported confidence. It lists API defaults
because a symbol-only call uses a **long, $1,000 notional, 5× leveraged**
scenario; that result is not a neutral price forecast. Provider failures fall
back to the existing market-tape answer. Progress status is emitted while the
analysis runs or polls.
