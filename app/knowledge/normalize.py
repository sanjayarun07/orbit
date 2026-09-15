"""HTML -> clean markdown, content hashing, and structure-aware chunking.

Chunks follow the document's headings, not a token window: a chunk is one
section (heading + its paragraphs), split further only when a section is
long, and small trailing sections are merged into their predecessor. The
heading path ("Liquidations > Health Factor") travels with every chunk so
retrieval and citations stay legible.
"""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser

_DROP_TAGS = {"script", "style", "nav", "footer", "header", "aside", "noscript", "svg", "form", "iframe", "button", "head", "title"}
_BLOCK_TAGS = {"p", "div", "section", "article", "li", "ul", "ol", "table", "tr", "blockquote", "pre", "br", "hr", "main", "details", "summary"}


class _MarkdownExtractor(HTMLParser):
    """Keeps headings, paragraphs, list items, code and links; drops chrome."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._heading: int | None = None
        self._pre = False
        self._href: str | None = None
        self._in_main = False
        self._saw_main = False

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        attrs = dict(attrs)
        role = (attrs.get("role") or "").lower()
        if tag in ("main", "article") or role == "main":
            self._in_main = True
            self._saw_main = True
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading = int(tag[1])
            self.parts.append("\n\n" + "#" * self._heading + " ")
        elif tag == "pre":
            self._pre = True
            self.parts.append("\n\n```\n")
        elif tag == "code" and not self._pre:
            self.parts.append("`")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "a":
            self._href = attrs.get("href")
            self.parts.append("[")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        if tag in _DROP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading = None
            self.parts.append("\n\n")
        elif tag == "pre":
            self._pre = False
            self.parts.append("\n```\n\n")
        elif tag == "code" and not self._pre:
            self.parts.append("`")
        elif tag == "a":
            href = self._href or ""
            self.parts.append(f"]({href})" if href.startswith("http") else "]")
            self._href = None
        elif tag in ("main", "article"):
            self._in_main = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._pre:
            self.parts.append(data)
        else:
            self.parts.append(re.sub(r"\s+", " ", data))


def html_to_markdown(html: str) -> str:
    parser = _MarkdownExtractor()
    try:
        parser.feed(html or "")
    except Exception:
        pass
    text = "".join(parser.parts)
    return clean_markdown(text)


def clean_markdown(text: str) -> str:
    text = text.replace("\r", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    lines = [re.sub(r"(?:#|\s)?Copy\s*$", "", line.rstrip()) if line.lstrip().startswith("#") else line.rstrip() for line in text.split("\n")]
    # Drop boilerplate lines that survive extraction (cookie banners, "edit this page", nav crumbs).
    junk = re.compile(r"^(?:\s*(?:cookie|accept all|edit this page|last updated|previous|next|on this page|table of contents|skip to (?:main )?content|copy(?:right)?)\b.*)$", re.I)
    lines = [line for line in lines if not (len(line.strip()) <= 40 and junk.match(line))]
    text = "\n".join(lines)
    # Navigation "cards" survive as bracketed islands: "[\n\nYield Strategies\n\n]" -- drop them.
    text = re.sub(r"\[\s*\n\s*(?:\n\s*)?([^\]\n]{1,80})\n\s*(?:\n\s*)?\]", "", text)
    text = re.sub(r"^[\[\]\s]+$", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def content_hash(markdown: str) -> str:
    normalized = re.sub(r"\s+", " ", (markdown or "").strip().lower())
    return hashlib.sha256(normalized.encode()).hexdigest()


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def chunk_markdown(markdown: str, max_chars: int = 1800, min_chars: int = 200) -> list[dict]:
    """Section-aware chunks: [{heading, content, position}]. `heading` is the
    heading path ("Liquidations > Health Factor")."""
    sections: list[tuple[list[str], list[str]]] = []
    path: list[str] = []
    buffer: list[str] = []
    for line in (markdown or "").split("\n"):
        m = _HEADING.match(line)
        if m:
            if buffer and "".join(buffer).strip():
                sections.append((list(path), buffer))
            level = len(m.group(1))
            path = path[: level - 1] + [m.group(2).strip()]
            buffer = []
        else:
            buffer.append(line)
    if buffer and "".join(buffer).strip():
        sections.append((list(path), buffer))

    chunks: list[dict] = []
    for heading_path, lines in sections:
        heading = " > ".join(h for h in heading_path if h)
        text = "\n".join(lines).strip()
        if not text:
            continue
        for piece in _split_long(text, max_chars):
            if chunks and len(piece) < min_chars and chunks[-1]["heading"] == heading:
                chunks[-1]["content"] += "\n\n" + piece
            elif chunks and len(piece) < 80 and len(chunks[-1]["content"]) < max_chars // 2:
                # A stub section (a one-liner under its own heading) rides with its neighbour.
                chunks[-1]["content"] += "\n\n" + (f"**{heading}**\n" if heading else "") + piece
            else:
                chunks.append({"heading": heading, "content": piece, "position": len(chunks)})
    return chunks


def _split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    paragraphs = re.split(r"\n\s*\n", text)
    pieces: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 2 > max_chars and current:
            pieces.append(current.strip())
            current = ""
        if len(paragraph) > max_chars:  # a single huge paragraph: hard split on sentences
            for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
                if len(current) + len(sentence) + 1 > max_chars and current:
                    pieces.append(current.strip())
                    current = ""
                current += sentence + " "
        else:
            current += paragraph + "\n\n"
    if current.strip():
        pieces.append(current.strip())
    return pieces
