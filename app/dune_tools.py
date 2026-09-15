"""Dune SQL API client for the wash-trading detector.

Hand-rolled httpx client, matching this codebase's convention of no provider
SDKs (see app/market_providers.py, app/additional_providers.py). This is
called only from the /admin/wash-trading/* route handlers in app/main.py via
app/wash_trading/pipeline.py -- never registered as a ProviderTool and never
reachable from the chat graph. See app/settings.py for dune_* config.

Verified live against the real API (2026-09-13, real DUNE_API_KEY):
  POST {base}/sql/execute        {"sql": "...", "performance": "small"}
                                  -> {"execution_id": "...", "state": "QUERY_STATE_PENDING"}
  GET  {base}/execution/{id}/status  -> {"state": "QUERY_STATE_PENDING"|"QUERY_STATE_COMPLETED"|
                                          "QUERY_STATE_FAILED"|"QUERY_STATE_CANCELLED", ...}
  GET  {base}/execution/{id}/results -> row data once completed

IMPORTANT -- "performance" is NOT "small"|"medium"|"large" as Dune's public
docs (https://docs.dune.com/api-reference/executions/endpoint/execute-sql)
claim: live-tested, "medium"/"large"/"standard"/"basic"/"pro" all return
{"error": "Invalid performance tier"}. Only "small" (and omitting the field)
were accepted syntactically on this account -- settings.dune_query_performance
defaults to "small" accordingly. Re-verify if Dune's API changes.

Also live-verified: this account's requests (even a bare "SELECT 1") were
rejected with {"error": "This api request would exceed your configured
datapoint limit per billing cycle..."} -- a real, external account/billing
limit on dune.com, not a bug here. No query has actually returned rows yet.
Raise the datapoint limit in the Dune dashboard, then re-run
scripts/verify_dune_schema.py before trusting anything below.

IMPORTANT -- what is NOT yet verified (blocked on the above): the exact
column names/semantics of dex_solana.trades (launchpad/pool filter column,
whether tx_id is shared across legs of one routed swap, whether amount_usd
on a leg is the leg's own notional or the full route's). Do not trust
build_top_traders_sql / build_wallet_trades_sql's column choices until
scripts/verify_dune_schema.py has actually returned data and the findings
here are updated to match.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
import time
from typing import Any

import httpx

from app.settings import settings


_TERMINAL_STATES = {"QUERY_STATE_COMPLETED", "QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"}

# Dune's API takes SQL as a text string (no bound-parameter facility), so
# every value spliced into a query below is validated to a safe shape first
# rather than parameterized. dex_solana.trades is Solana-only, so trader_ids
# and token mints are base58 addresses (32-44 chars, no quote/backslash/
# whitespace) -- the check makes an injected literal structurally impossible,
# not merely unlikely. Kept here (not only at the API model) so these builders
# stay safe no matter who calls them.
_BASE58_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _safe_solana_address(value: str, field: str) -> str:
    if not isinstance(value, str) or not _BASE58_ADDRESS.match(value):
        raise ValueError(f"Invalid Solana {field}: must be a base58 address (32-44 chars)")
    return value


class DuneQueryError(RuntimeError):
    """A Dune execution reached a terminal, non-successful state, or timed out."""


def _headers() -> dict[str, str]:
    if not settings.dune_api_key:
        raise RuntimeError("DUNE_API_KEY is not configured")
    return {"X-Dune-Api-Key": settings.dune_api_key, "Content-Type": "application/json"}


def execute_sql(sql: str, performance: str | None = None) -> str:
    """Submit arbitrary SQL for execution; returns the execution_id.

    Dune's execute-SQL endpoint runs SQL that isn't a pre-saved query --
    exactly what a per-token/per-window dynamic investigation needs. Its
    query_id is always 0 for this endpoint (documented Dune behavior); the
    execution_id is what matters for polling/fetching.
    """
    with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
        response = client.post(
            f"{settings.dune_base_url}/sql/execute",
            headers=_headers(),
            json={"sql": sql, "performance": performance or settings.dune_query_performance},
        )
        response.raise_for_status()
        payload = response.json()
    execution_id = payload.get("execution_id")
    if not execution_id:
        raise DuneQueryError(f"Dune did not return an execution_id: {payload}")
    return execution_id


def poll_execution(execution_id: str) -> dict[str, Any]:
    """Block (synchronously) until the execution reaches a terminal state.

    Called from a worker thread via asyncio.to_thread at the admin-route
    layer (app/main.py), matching how other slow, synchronous provider calls
    in this codebase are dispatched off the event loop.
    """
    deadline = time.monotonic() + settings.dune_poll_timeout_seconds
    with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
        while True:
            response = client.get(
                f"{settings.dune_base_url}/execution/{execution_id}/status", headers=_headers(),
            )
            response.raise_for_status()
            payload = response.json()
            state = payload.get("state")
            if state in _TERMINAL_STATES:
                if state != "QUERY_STATE_COMPLETED":
                    raise DuneQueryError(f"Dune execution {execution_id} ended in {state}: {payload}")
                return payload
            if time.monotonic() >= deadline:
                raise DuneQueryError(
                    f"Dune execution {execution_id} did not complete within "
                    f"{settings.dune_poll_timeout_seconds}s (last state: {state})"
                )
            time.sleep(settings.dune_poll_interval_seconds)


def fetch_results(execution_id: str) -> list[dict[str, Any]]:
    with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
        response = client.get(
            f"{settings.dune_base_url}/execution/{execution_id}/results", headers=_headers(),
        )
        response.raise_for_status()
        payload = response.json()
    rows = ((payload.get("result") or {}).get("rows")) or payload.get("rows") or []
    return rows if isinstance(rows, list) else []


def run_sql(sql: str, performance: str | None = None) -> list[dict[str, Any]]:
    """Execute, poll, and fetch results in one call -- what pipeline.py uses."""
    execution_id = execute_sql(sql, performance)
    poll_execution(execution_id)
    return fetch_results(execution_id)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def build_top_traders_sql(
    token_mint: str, pool_filter: str | None, window_start: datetime, window_end: datetime, top_n: int,
) -> str:
    """UNVERIFIED column names -- see module docstring. Placeholder based on
    the query shape supplied by the requester; do not trust results until
    scripts/verify_dune_schema.py has confirmed dex_solana.trades' real
    schema and this docstring/query has been updated to match.

    The GROUP BY trader_id with COUNT(DISTINCT tx_id) is a best-effort
    routed-leg dedup: it assumes tx_id is shared across legs of one routed
    swap (so COUNT(DISTINCT tx_id) counts transactions, not legs) while
    SUM(amount_usd) still sums every leg's amount_usd. If Phase 0 finds
    amount_usd is per-leg notional (not per-transaction), this SUM will
    overcount routed swaps and must change to a per-tx_id aggregation first.

    SECURITY: token_mint is validated to a base58 shape before interpolation,
    so it cannot break out of its quoted literal. pool_filter, by contrast, is
    spliced in as a RAW SQL FRAGMENT by design (an analyst writing e.g.
    "project = 'raydium'") -- it is deliberately not sanitized, so this builder
    trusts its caller completely. That trust is bounded to the admin-only
    /admin/wash-trading route (app/main.py's _require_admin); never expose a
    path that lets an unauthenticated or chat-driven value reach pool_filter.
    """
    token_mint = _safe_solana_address(token_mint, "token mint")
    pool_clause = f"\n  AND {pool_filter}" if pool_filter else "\n  -- AND <pool/project filter here -- see scripts/verify_dune_schema.py>"
    return f"""SELECT
    trader_id,
    COUNT(DISTINCT tx_id) AS transactions,
    SUM(amount_usd) AS volume_usd,
    AVG(amount_usd) AS avg_trade_usd,
    MIN(block_time) AS first_trade,
    MAX(block_time) AS last_trade
FROM dex_solana.trades
WHERE block_time >= TIMESTAMP '{_iso(window_start)}'
  AND block_time <  TIMESTAMP '{_iso(window_end)}'
  AND (token_bought_mint_address = '{token_mint}' OR token_sold_mint_address = '{token_mint}'){pool_clause}
GROUP BY trader_id
ORDER BY volume_usd DESC
LIMIT {top_n};"""


def build_wallet_trades_sql(
    wallets: list[str], token_mint: str, window_start: datetime, window_end: datetime,
) -> str:
    """UNVERIFIED column names -- see module docstring.

    wallets are trader_ids returned by build_top_traders_sql's own results,
    not user input, but they are validated to a base58 shape here anyway
    (defense in depth) so this builder cannot be turned into an injection
    sink by a future caller that sources them differently.
    """
    token_mint = _safe_solana_address(token_mint, "token mint")
    wallet_list = ", ".join(f"'{_safe_solana_address(wallet, 'wallet')}'" for wallet in wallets)
    return f"""SELECT
    block_time,
    trader_id,
    token_bought_mint_address,
    token_sold_mint_address,
    token_bought_amount,
    token_sold_amount,
    amount_usd,
    project,
    tx_id
FROM dex_solana.trades
WHERE trader_id IN ({wallet_list})
  AND (token_bought_mint_address = '{token_mint}' OR token_sold_mint_address = '{token_mint}')
  AND block_time >= TIMESTAMP '{_iso(window_start)}'
  AND block_time <  TIMESTAMP '{_iso(window_end)}'
ORDER BY trader_id, block_time;"""
