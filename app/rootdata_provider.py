"""RootData project, VC and people search with credit-aware ID-map caching."""

from __future__ import annotations

from difflib import SequenceMatcher
import re
from threading import Lock
import time
from typing import Any

import httpx

from app.provider_router import ProviderRouter, ProviderTool
from app.settings import settings
from app.tool_results import compact_tool_result


_KINDS = {"project": 1, "vc": 2, "people": 3}
_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}
_cache_lock = Lock()
_STOP_WORDS = {
    "search", "find", "look", "lookup", "research", "analyze", "show", "tell", "me", "about",
    "for", "rootdata", "root", "data", "crypto", "web3", "project", "projects", "vc", "vcs",
    "venture", "capital", "investor", "investors", "fund", "funds", "person", "people", "founder",
}


def _matches(pattern: str):
    compiled = re.compile(pattern, re.IGNORECASE)
    return lambda request: bool(compiled.search(request))


def _query(request: str) -> str:
    words = re.findall(r"[A-Za-z0-9$._-]+", request)
    useful = [word for word in words if word.lower() not in _STOP_WORDS]
    if not useful:
        raise ValueError("Include the project, VC, or person name to search for")
    return " ".join(useful).strip(" .")


class RootDataProvider:
    name = "rootdata"

    def enabled(self) -> bool:
        return bool(settings.rootdata_api_key)

    def _id_map(self, entity_type: int) -> list[dict[str, Any]]:
        now = time.monotonic()
        with _cache_lock:
            cached = _cache.get(entity_type)
            if cached and cached[0] > now:
                return cached[1]
            with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
                response = client.post(
                    f"{settings.rootdata_base_url}/id_map",
                    headers={
                        "apikey": settings.rootdata_api_key or "",
                        "language": "en",
                        "Content-Type": "application/json",
                    },
                    json={"type": entity_type},
                )
                response.raise_for_status()
                payload = response.json()
            if payload.get("result") != 200:
                raise RuntimeError(str(payload.get("message") or "RootData search failed"))
            data = payload.get("data") or []
            rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
            rows = [row for row in rows if isinstance(row, dict) and row.get("name")]
            if not rows:
                raise RuntimeError("RootData returned an empty entity map")
            _cache[entity_type] = (now + settings.rootdata_map_cache_ttl_seconds, rows)
            return rows

    @staticmethod
    def _rank(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
        normalized = query.casefold()
        query_words = set(re.findall(r"[a-z0-9]+", normalized))

        def score(row: dict[str, Any]) -> float:
            name = str(row.get("name") or "").casefold()
            symbol = str(row.get("token_symbol") or "").casefold()
            name_words = set(re.findall(r"[a-z0-9]+", name))
            exact = 4.0 if normalized in {name, symbol} else 0.0
            contains = 2.0 if normalized in name or name in normalized else 0.0
            overlap = len(query_words & name_words) / max(1, len(query_words))
            fuzzy = SequenceMatcher(None, normalized, name).ratio()
            return exact + contains + overlap * 2 + fuzzy

        ranked = sorted(((score(row), row) for row in rows), key=lambda item: item[0], reverse=True)
        return [row for score_value, row in ranked[:10] if score_value >= 0.45]

    def search(self, request: str, kind: str) -> str:
        query = _query(request)
        rows = self._rank(self._id_map(_KINDS[kind]), query)
        label = {"project": "projects", "vc": "VCs", "people": "people"}[kind]
        lines = [
            f"# RootData {label} matching “{query}”",
            "", "| Name | Symbol | Status | RootData ID |", "|---|---|---|---:|",
        ]
        if rows:
            for row in rows:
                active = row.get("active")
                status = "Active" if active is True else "Inactive" if active is False else "—"
                lines.append(
                    f"| {row.get('name')} | {row.get('token_symbol') or '—'} | {status} | {row.get('id') or '—'} |"
                )
        else:
            lines.append("| No close matches | — | — | — |")
        lines.extend([
            "", "Source: [RootData API](https://www.rootdata.com/api/doc)",
            "The ID map is cached to reduce RootData credit usage. Search results identify candidates; verify the exact entity before relying on its profile.",
        ])
        return compact_tool_result("\n".join(lines))

    def search_projects(self, request: str) -> str:
        return self.search(request, "project")

    def search_vcs(self, request: str) -> str:
        return self.search(request, "vc")

    def search_people(self, request: str) -> str:
        return self.search(request, "people")

    def register(self, router: ProviderRouter) -> None:
        common = {
            "enabled": self.enabled,
            "cost_usd": settings.rootdata_request_cost_usd,
            "quota_per_minute": settings.rootdata_requests_per_minute,
            "cache_ttl_seconds": 300,
            "priority": 12,
        }
        router.register(ProviderTool(
            "rootdata_project_search", self.name, ("project_intelligence",), self.search_projects,
            matches=_matches(r"\b(?:crypto|web3)?\s*projects?\b"),
            keywords=("project", "crypto project", "web3 project"), **common,
        ))
        router.register(ProviderTool(
            "rootdata_vc_search", self.name, ("vc_intelligence",), self.search_vcs,
            matches=_matches(r"\b(?:crypto|web3)?\s*(?:vc|venture capital|investors?|funds?)\b"),
            keywords=("vc", "venture capital", "investor", "fund"), **common,
        ))
        router.register(ProviderTool(
            "rootdata_people_search", self.name, ("people_intelligence",), self.search_people,
            matches=_matches(r"\b(?:crypto|web3)\b.{0,50}\b(?:people|person|founders?)\b|\b(?:people|person|founders?)\b.{0,50}\b(?:crypto|web3)\b"),
            keywords=("crypto people", "web3 people", "founder"), **common,
        ))


ROOTDATA_PROVIDERS = (RootDataProvider,)
