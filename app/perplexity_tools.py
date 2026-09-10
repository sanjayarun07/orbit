"""Cost-aware adapters for Perplexity Agent API hosted tools.

Only read-only retrieval tools are exposed. Perplexity's sandbox tool is
intentionally absent from both this module and the capability catalog.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
import hashlib
import json
from threading import Condition
import time
from typing import Any

import httpx

from app.metrics import increment
from app.provider_router import is_usable_provider_output
from app.settings import settings
from app.tool_results import compact_tool_result


_TOOL_CAPABILITIES = {
    "web_search": "web_research",
    "fetch_url": "url_fetch",
    "people_search": "people_intelligence",
    "finance_search": "finance_data",
}


def _costs() -> dict[str, float]:
    return {
        "web_search": settings.perplexity_web_search_cost_usd,
        "fetch_url": settings.perplexity_fetch_url_cost_usd,
        "people_search": settings.perplexity_people_search_cost_usd,
        "finance_search": settings.perplexity_finance_search_cost_usd,
    }


_condition = Condition()
_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
_inflight: set[str] = set()


def perplexity_available() -> bool:
    return bool(settings.perplexity_api_key)


def perplexity_catalog() -> list[dict[str, Any]]:
    """Return safe metadata for observability and cost-aware routing."""
    costs = _costs()
    return [
        {
            "name": f"perplexity_{name}",
            "provider": "perplexity",
            "capabilities": [capability],
            "risk": "read_only",
            "cost_usd_per_invocation": costs[name],
            "configured": perplexity_available(),
        }
        for name, capability in _TOOL_CAPABILITIES.items()
    ]


def _cache_key(tool_type: str, prompt: str) -> str:
    value = json.dumps([tool_type, prompt.strip()], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(value.encode()).hexdigest()


def _extract_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                value = content.get("text")
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
    return "\n\n".join(parts).strip()


def _extract_sources(payload: Any) -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            url = value.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                title = value.get("title") or value.get("name") or url
                if all(existing != url for _, existing in sources):
                    sources.append((str(title), url))
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str) and value.startswith(("http://", "https://")):
            url = value.rstrip(".,;:!?)]")
            if all(existing != url for _, existing in sources):
                sources.append((url, url))

    visit(payload)
    return sources


def _invoke(tool_type: str, prompt: str, instructions: str) -> str:
    if tool_type not in _TOOL_CAPABILITIES:
        raise ValueError(f"Unsupported Perplexity tool: {tool_type}")
    if not settings.perplexity_api_key:
        raise RuntimeError("PERPLEXITY_API_KEY is not configured")

    key = _cache_key(tool_type, prompt)
    now = time.monotonic()
    with _condition:
        cached = _cache.get(key)
        if cached and cached[0] > now:
            _cache.move_to_end(key)
            increment("perplexity_cache_hits")
            return cached[1]
        while key in _inflight:
            _condition.wait(timeout=settings.perplexity_timeout_seconds)
            cached = _cache.get(key)
            if cached and cached[0] > time.monotonic():
                increment("perplexity_coalesced_calls")
                return cached[1]
        _inflight.add(key)

    increment(f"perplexity_{tool_type}_calls")
    increment("perplexity_estimated_cost_microusd", round(_costs()[tool_type] * 1_000_000))
    try:
        today = datetime.now().astimezone().date().isoformat()
        with httpx.Client(timeout=settings.perplexity_timeout_seconds) as client:
            response = client.post(
                settings.perplexity_agent_url,
                headers={
                    "Authorization": f"Bearer {settings.perplexity_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.perplexity_model,
                    "input": prompt,
                    "tools": [{"type": tool_type}],
                    "instructions": (
                        f"Today is {today}. You must use the supplied {tool_type} tool. "
                        f"{instructions} Be concise, preserve exact dates and numbers, and cite sources."
                    ),
                    "max_output_tokens": 2200,
                },
            )
            response.raise_for_status()
            payload = response.json()
        answer = _extract_text(payload)
        if not answer:
            raise RuntimeError("Perplexity returned no answer text")
        if not is_usable_provider_output(prompt, answer, require_live_quote=tool_type == "finance_search"):
            raise RuntimeError("Perplexity returned an unusable or ungrounded response")
        sources = _extract_sources(payload)
        if sources and not any(url in answer for _, url in sources):
            links = "\n".join(f"- [{title}]({url})" for title, url in sources[:8])
            answer = f"{answer}\n\nSources:\n{links}"
        result = compact_tool_result(answer)
        with _condition:
            _cache[key] = (time.monotonic() + settings.perplexity_cache_ttl_seconds, result)
            _cache.move_to_end(key)
            while len(_cache) > settings.perplexity_cache_max_entries:
                _cache.popitem(last=False)
        return result
    except Exception:
        increment(f"perplexity_{tool_type}_errors")
        raise
    finally:
        with _condition:
            _inflight.discard(key)
            _condition.notify_all()


def perplexity_web_search(query: str) -> str:
    """Search the current web for recent, source-grounded information."""
    return _invoke(
        "web_search",
        query,
        "Prefer primary and authoritative sources. For news, distinguish event dates from publication dates.",
    )


def perplexity_fetch_url(url: str) -> str:
    """Fetch and extract the useful content from one public URL."""
    return _invoke(
        "fetch_url",
        f"Fetch and summarize this URL: {url}",
        "Use the fetched page as the primary source and state if it cannot be accessed.",
    )


def perplexity_people_search(query: str) -> str:
    """Find public professional information about people and employees."""
    return _invoke(
        "people_search",
        query,
        "Return only relevant public professional information; do not infer sensitive personal details.",
    )


def perplexity_finance_search(query: str) -> str:
    """Retrieve current financial and market information."""
    return _invoke(
        "finance_search",
        query,
        "For a live quote, return the exact numeric price, quote currency, and observation timestamp. "
        "Report the instrument unambiguously and avoid presenting data as investment advice.",
    )


PERPLEXITY_FUNCTIONS = (
    perplexity_web_search,
    perplexity_fetch_url,
    perplexity_people_search,
    perplexity_finance_search,
)
