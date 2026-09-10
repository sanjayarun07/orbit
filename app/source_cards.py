"""Extract compact, UI-safe source metadata from grounded provider output."""

from __future__ import annotations

import re
from urllib.parse import urlparse


_MARKDOWN_LINK = re.compile(r"\[([^\]]+)]\((https?://[^)\s]+)\)")
_BARE_LINK = re.compile(r"(?<!\()https?://[^\s<>]+")
_DATE = re.compile(
    r"\b(?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}|"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2},\s+20\d{2})\b",
    re.IGNORECASE,
)


def _date_near(text: str, start: int, end: int) -> str | None:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    next_line_end = text.find("\n", line_end + 1)
    if next_line_end < 0:
        next_line_end = len(text)
    nearby = text[line_start:next_line_end]
    dates = _DATE.findall(nearby)
    return dates[-1] if dates else None


def extract_source_cards(text: str, limit: int = 12) -> list[dict[str, str | None]]:
    """Return deduplicated source cards from Markdown links and bare URLs."""
    cards: list[dict[str, str | None]] = []
    seen: set[str] = set()

    def add(url: str, title: str, start: int, end: int) -> None:
        url = url.rstrip(".,;:!?)]")
        if url in seen or not url.startswith(("http://", "https://")):
            return
        seen.add(url)
        domain = urlparse(url).netloc.removeprefix("www.") or "Source"
        clean_title = re.sub(r"[*_`]", "", title).strip() or domain
        cards.append({
            "url": url,
            "title": clean_title,
            "domain": domain,
            "date": _date_near(text, start, end),
        })

    markdown_ranges: list[tuple[int, int]] = []
    for match in _MARKDOWN_LINK.finditer(text):
        markdown_ranges.append(match.span())
        add(match.group(2), match.group(1), match.start(), match.end())
        if len(cards) >= limit:
            return cards
    for match in _BARE_LINK.finditer(text):
        if any(start <= match.start() < end for start, end in markdown_ranges):
            continue
        url = match.group(0)
        add(url, urlparse(url.rstrip(".,;:!?)]")).netloc, match.start(), match.end())
        if len(cards) >= limit:
            break
    return cards
