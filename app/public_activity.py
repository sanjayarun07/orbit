"""Allowlisted activity metadata; never forward raw provider payloads by default."""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_SENSITIVE_QUERY_KEY = re.compile(r"^(?:api[_-]?key|access[_-]?token|auth(?:orization)?|secret|password|signature|sig|session(?:_id)?|jwt)$", re.I)


def _research_progress(trajectory: dict) -> dict | None:
    """Public, bounded work status; never expose model plans or page text."""
    # A turn stores the allowlisted form; history applies this function again.
    # Preserve and revalidate that public form instead of dropping its status.
    raw = trajectory.get("research_loop") or trajectory.get("research_progress")
    if not isinstance(raw, dict):
        return None
    verdicts = []
    for item in (raw.get("verdicts") or raw.get("sources") or [])[:8]:
        if not isinstance(item, dict):
            continue
        verdict = item.get("verdict")
        provenance = item.get("provenance")
        if verdict not in {"qualifies", "related_but_different", "not_established", "date_verified", "attributed"} or provenance not in {"page", "reader", "unreadable", "uninspected"}:
            continue
        name = item.get("name")
        url = item.get("url")
        if not isinstance(name, str) or not isinstance(url, str):
            continue
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            continue
        safe_query = urlencode([(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                                if not _SENSITIVE_QUERY_KEY.search(key)])
        public_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, safe_query, ""))[:2048]
        verdicts.append({"name": name[:100], "url": public_url, "verdict": verdict, "provenance": provenance})
    calls = raw.get("calls")
    completed_calls = (sum(isinstance(call, str) and ": skipped (" not in call for call in calls)
                       if isinstance(calls, list) else raw.get("provider_calls", 0))
    checked_pages = (len({call.split(": ", 1)[1] for call in calls
                          if isinstance(call, str) and call.startswith("page_read (url_reader): ")})
                     if isinstance(calls, list) else raw.get("checked_pages", 0))
    bounded = lambda value: min(max(value, 0), 20) if type(value) is int else 0
    kind = raw.get("kind") if raw.get("kind") in {"calendar", "attributed_source"} else None
    result = {"status": "complete", "provider_calls": bounded(completed_calls),
            "checked_pages": bounded(checked_pages), "sources": verdicts}
    if kind:
        result["kind"] = kind
    return result


def public_activity(trajectory: dict | None) -> dict | None:
    if not isinstance(trajectory, dict):
        return None
    result = {}
    progress = _research_progress(trajectory)
    if progress is not None:
        result["research_progress"] = progress
    for key, name in trajectory.items():
        match = re.fullmatch(r"tool_name_(\d+)", key)
        if not match or not isinstance(name, str) or not re.fullmatch(r"[\w.-]{1,120}", name) or name == "finish":
            continue
        index = match.group(1)
        observation = trajectory.get(f"observation_{index}")
        failed = isinstance(observation, str) and (
            "failed:" in observation.lower() or "error" in observation[:120].lower()
        )
        result[key] = name
        result[f"observation_{index}"] = (
            "Tool call failed: provider details are hidden." if failed
            else "Tool call completed. See the answer for results."
        )
    return result or None
