"""One door to Mobula for every module that calls it.

Review of 2026-09-20: the bundle check, the deep dive and the wallet tools
each called Mobula directly, so a "single" router call could be twenty-six
requests, retries repeated the whole sequence, and three concurrent checks
could exceed the configured per-minute limit while the router's quota,
cost and breaker never saw them. The same review found the configured base
URL ignored by hardcoded hosts.

Every Mobula request now goes through `get()`: the base comes from
settings, a process-wide token bucket enforces `mobula_requests_per_minute`
across all callers, at most `MAX_CONCURRENT` requests are in flight, and
each request is counted for the provider metrics. A caller that would
exceed the budget waits (bounded) rather than bursting; a caller that would
wait too long fails fast with a clear error rather than stalling a turn.
"""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlsplit

import httpx

from app.metrics import increment
from app.settings import settings

logger = logging.getLogger(__name__)

MAX_CONCURRENT = 6
MAX_WAIT_SECONDS = 8.0


class MobulaBudgetExceeded(RuntimeError):
    """The per-minute Mobula budget is spent; the caller should degrade."""


def origin() -> str:
    parts = urlsplit(settings.mobula_base_url)
    return f"{parts.scheme}://{parts.netloc}"


def url(version: int, path: str) -> str:
    return f"{origin()}/api/{version}{path}"


class _Bucket:
    """Token bucket: `rate` tokens per minute, refilled continuously."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens = float(settings.mobula_requests_per_minute)
        self._stamp = time.monotonic()

    def take(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                rate = float(settings.mobula_requests_per_minute)
                self._tokens = min(rate, self._tokens + (now - self._stamp) * rate / 60.0)
                self._stamp = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                wait = (1.0 - self._tokens) * 60.0 / max(rate, 1.0)
            if time.monotonic() + wait > deadline:
                return False
            time.sleep(min(wait, 0.25))

    def reset(self) -> None:
        with self._lock:
            self._tokens = float(settings.mobula_requests_per_minute)
            self._stamp = time.monotonic()


_bucket = _Bucket()
_inflight = threading.BoundedSemaphore(MAX_CONCURRENT)


def get(version: int, path: str, params: dict, timeout: float | None = None) -> dict | list:
    """GET one Mobula endpoint under the shared budget; the unwrapped data."""
    if not settings.mobula_api_key:
        raise RuntimeError("Mobula is not configured")
    if not _bucket.take(MAX_WAIT_SECONDS):
        increment("mobula_budget_exceeded")
        raise MobulaBudgetExceeded(f"Mobula budget of {settings.mobula_requests_per_minute}/min is spent; try again in a moment")
    increment("mobula_requests")
    with _inflight:
        with httpx.Client(timeout=timeout or settings.provider_request_timeout_seconds) as client:
            response = client.get(url(version, path), params=params, headers={"Authorization": settings.mobula_api_key})
            response.raise_for_status()
            payload = response.json()
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def reset_for_test() -> None:
    _bucket.reset()
