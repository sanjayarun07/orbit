"""Built-in API providers registered behind provider-neutral capabilities."""

from __future__ import annotations

import re
import json
from functools import lru_cache
from pathlib import Path

from app.additional_providers import ADDITIONAL_PROVIDERS
from app.market_providers import MARKET_PROVIDERS
from app.perplexity_tools import (
    perplexity_available,
    perplexity_fetch_url,
    perplexity_finance_search,
    perplexity_people_search,
    perplexity_web_search,
)
from app.provider_router import ProviderRouter, ProviderTool
from app.rootdata_provider import ROOTDATA_PROVIDERS
from app.settings import settings
from app.web_search import _openai_web_search


def _fetch_url(request: str) -> str:
    match = re.search(r"https?://[^\s<>]+", request)
    if not match:
        raise ValueError("No URL found")
    return perplexity_fetch_url(match.group(0).rstrip(".,)"))


@lru_cache(maxsize=1)
def get_provider_router() -> ProviderRouter:
    router = ProviderRouter()
    for provider_type in MARKET_PROVIDERS:
        provider_type().register(router)
    for provider_type in ADDITIONAL_PROVIDERS:
        provider_type().register(router)
    for provider_type in ROOTDATA_PROVIDERS:
        provider_type().register(router)
    router.register(ProviderTool(
        "perplexity_web_search", "perplexity", ("web_research", "equity_research", "finance_data", "project_intelligence", "vc_intelligence"), perplexity_web_search,
        enabled=perplexity_available, cost_usd=settings.perplexity_web_search_cost_usd,
        quota_per_minute=settings.perplexity_requests_per_minute, priority=4,
    ))
    router.register(ProviderTool(
        "perplexity_fetch_url", "perplexity", ("url_fetch",), _fetch_url,
        enabled=perplexity_available, cost_usd=settings.perplexity_fetch_url_cost_usd,
        quota_per_minute=settings.perplexity_requests_per_minute, priority=5,
    ))
    router.register(ProviderTool(
        "perplexity_people_search", "perplexity", ("people_intelligence",), perplexity_people_search,
        enabled=perplexity_available, cost_usd=settings.perplexity_people_search_cost_usd,
        quota_per_minute=settings.perplexity_requests_per_minute, priority=5,
    ))
    router.register(ProviderTool(
        "perplexity_finance_search", "perplexity", ("finance_data", "equity_research"), perplexity_finance_search,
        enabled=perplexity_available, cost_usd=settings.perplexity_finance_search_cost_usd,
        quota_per_minute=settings.perplexity_requests_per_minute, priority=5,
    ))
    router.register(ProviderTool(
        "openai_web_search", "openai", ("web_research", "url_fetch", "people_intelligence", "finance_data"),
        _openai_web_search, enabled=lambda: bool(settings.openai_api_key),
        cost_usd=settings.openai_web_search_cost_usd,
        quota_per_minute=settings.openai_web_search_requests_per_minute, priority=1,
    ))
    path = Path(settings.provider_overrides_path)
    if path.exists():
        try:
            value = json.loads(path.read_text())
            if isinstance(value, dict):
                router.apply_overrides(value)
        except (OSError, json.JSONDecodeError):
            pass
    return router


def save_provider_overrides() -> None:
    path = Path(settings.provider_overrides_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(get_provider_router().overrides(), indent=2, sort_keys=True))
    temporary.replace(path)
