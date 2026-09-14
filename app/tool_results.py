"""Normalize external tool observations before they enter an LLM prompt/UI."""

import re
from typing import Any

from app.settings import settings


# Hosted-search providers (OpenAI's Responses API, and defensively any other
# using the same convention) sometimes embed inline citation annotations
# directly in their answer text using Unicode Private Use Area sentinel
# characters -- a paired U+E200 ... U+E201 span wrapping a "cite" marker, an
# internal turn/search reference, and the source URLs -- meant to be resolved
# via the response's structured annotations array, never shown to a user.
# Confirmed live leaking verbatim into a user-facing answer (and, once cached,
# replayed identically to every subsequent caller until eviction). Callers
# rebuild a clean, clickable source list separately from the structured
# fields, so the raw span is pure noise: drop the whole span, then sweep any
# stray delimiter left over from an unpaired open. The E200-E20F band is not
# used for any legitimate glyph, so this never removes real content.
_INLINE_CITATION_SPAN = re.compile(".*?", re.DOTALL)
_STRAY_ANNOTATION_CHARS = re.compile("[-]")


def strip_inline_citation_markers(text: str) -> str:
    if not text:
        return text
    return _STRAY_ANNOTATION_CHARS.sub("", _INLINE_CITATION_SPAN.sub("", text))


def text_from_mcp_content(content: list[Any]) -> str:
    """Convert MCP content blocks to stable text without leaking Python reprs."""
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(str(getattr(block, "text", "")))
        elif getattr(block, "type", None) == "resource":
            resource = getattr(block, "resource", None)
            value = getattr(resource, "text", None)
            if value:
                parts.append(str(value))
    return "\n".join(part for part in parts if part).strip()


def compact_tool_result(value: str, max_chars: int | None = None) -> str:
    """Bound observations while retaining their beginning and conclusion.

    Tool output is often the largest token consumer. Keeping both ends preserves
    headings/summary fields and pagination metadata better than a hard prefix cut.
    """
    limit = max_chars or settings.mcp_result_max_chars
    text = value.strip()
    if len(text) <= limit:
        return text
    marker = "\n\n… tool result compacted for context efficiency …\n\n"
    available = max(0, limit - len(marker))
    head = int(available * 0.75)
    tail = available - head
    return f"{text[:head].rstrip()}{marker}{text[-tail:].lstrip()}"
