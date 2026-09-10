"""Read-only LI.FI backup quoting.

This module never signs or submits transactions.  Its transaction request must
pass through the same wallet approval and policy path as every other executor.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.settings import settings


def _headers() -> dict[str, str]:
    return {"x-lifi-api-key": settings.lifi_api_key} if settings.lifi_api_key else {}


def get_quote(params: dict[str, str]) -> dict[str, Any]:
    if not settings.lifi_enabled:
        raise RuntimeError("LI.FI backup routing is disabled")
    with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
        response = client.get(f"{settings.lifi_base_url}/quote", params=params, headers=_headers())
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or not payload.get("transactionRequest"):
        raise RuntimeError("LI.FI returned no executable quote")
    return payload


def get_status(tx_hash: str, *, from_chain: str | None = None, to_chain: str | None = None) -> dict[str, Any]:
    params = {"txHash": tx_hash}
    if from_chain:
        params["fromChain"] = from_chain
    if to_chain:
        params["toChain"] = to_chain
    with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
        response = client.get(f"{settings.lifi_base_url}/status", params=params, headers=_headers())
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("LI.FI returned an invalid status response")
    return payload
