"""x402 pay-per-message gate for POST /chat.

Off unless X402_ENABLED. When on, an unpaid request receives HTTP 402 with the
payment requirements (scheme "exact", USDC on X402_NETWORK, X402_PRICE, paid to
X402_PAY_TO); the browser signs an EIP-3009 authorization with the connected
EVM wallet and retries with the payment header, and the facilitator verifies
it before the chat handler runs and settles it after. Nothing here touches the
user's Solana wallet, trade plans, or signing -- it only gates the HTTP route.

The middleware is built lazily on the first protected request so settings can
be changed (and, in tests, a facilitator injected) without an import-time
side effect.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Awaitable, Callable

from app.settings import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_gate: Callable[..., Awaitable[Any]] | None = None
_facilitator_override: Any = None


def public_config() -> dict:
    """Browser-safe description of the payment requirement, for /config/public."""
    return {
        "enabled": bool(settings.x402_enabled and settings.x402_pay_to),
        "network": settings.x402_network,
        "price": settings.x402_price,
        "pay_to": settings.x402_pay_to if settings.x402_enabled else None,
        "facilitator_url": settings.x402_facilitator_url if settings.x402_enabled else None,
    }


def set_facilitator(client: Any) -> None:
    """Inject a facilitator client (tests); None restores the configured one."""
    global _facilitator_override, _gate
    with _lock:
        _facilitator_override = client
        _gate = None


def reset() -> None:
    set_facilitator(None)


def _build() -> Callable[..., Awaitable[Any]]:
    from x402 import x402ResourceServer
    from x402.http import FacilitatorConfig, HTTPFacilitatorClient
    from x402.http.middleware.fastapi import payment_middleware
    from x402.http.types import PaymentOption, RouteConfig
    from x402.mechanisms.evm.exact import ExactEvmServerScheme

    if not settings.x402_pay_to:
        raise RuntimeError("X402_PAY_TO is required when X402_ENABLED is true")
    facilitator = _facilitator_override or HTTPFacilitatorClient(FacilitatorConfig(url=settings.x402_facilitator_url))
    server = x402ResourceServer(facilitator)
    server.register(settings.x402_network, ExactEvmServerScheme())
    routes = {
        "POST /chat": RouteConfig(
            accepts=PaymentOption(
                scheme="exact",
                pay_to=settings.x402_pay_to,
                price=settings.x402_price,
                network=settings.x402_network,
                max_timeout_seconds=settings.x402_max_timeout_seconds,
            ),
            description=settings.x402_description,
            mime_type="application/json",
        )
    }
    return payment_middleware(routes, server, sync_facilitator_on_start=settings.x402_sync_facilitator_on_start)


async def chat_payment_gate(request, call_next):
    """FastAPI HTTP middleware: pass through unless x402 is enabled."""
    global _gate
    if not settings.x402_enabled:
        return await call_next(request)
    gate = _gate
    if gate is None:
        with _lock:
            if _gate is None:
                _gate = _build()
            gate = _gate
    return await gate(request, call_next)
