"""Normalize external tool observations before they enter an LLM prompt/UI."""

from typing import Any

from app.settings import settings


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
