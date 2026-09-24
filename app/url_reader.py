"""Read and summarize a web page the user linked.

Until 2026-09-18 a pasted link went straight to Perplexity's fetch tool,
and a page it could not read (CoinMarketCap, X, most JS-heavy sites) came
back as "I couldn't access the page". Now the page is read in three ways,
in order, and summarized by our own model against what the user asked:

1. fetched directly (a browser-like request; the text of the article is
   extracted with the standard-library HTML parser),
2. Perplexity's fetch tool,
3. a Perplexity web search about the link itself (a post or page the
   search index has seen, even when the site blocks readers).

Only when all three fail does the answer say so, naming what was tried.
Token pages (CoinMarketCap coin pages, explorers) never come here: the
research node turns those into token questions first (app/token_pages.py).
"""
from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import dspy
import httpx

from app.perplexity_tools import perplexity_available, perplexity_fetch_url, perplexity_web_search

logger = logging.getLogger(__name__)

URL = re.compile(r"https?://[^\s<>]+", re.I)
MIN_TEXT_CHARS = 400
MAX_TEXT_CHARS = 24_000
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class _Text(HTMLParser):
    """The readable text of a page: title, headings, paragraphs, list items
    and table cells; scripts, styles, navigation and footers left out."""

    _SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form", "button", "iframe", "template"}
    _BLOCK = {"p", "h1", "h2", "h3", "h4", "li", "td", "th", "blockquote", "pre", "article", "section", "div", "br", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def extract_text(html: str) -> tuple[str, str]:
    """(title, text) from an HTML document; whitespace collapsed, one line
    per block."""
    parser = _Text()
    try:
        parser.feed(html)
    except Exception:
        pass
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    text = "\n".join(line for line in lines if line)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return parser.title.strip(), text[:MAX_TEXT_CHARS]


_MAX_REDIRECTS = 5
_MAX_BYTES = 2_000_000


def resolve_public(url: str) -> tuple[str, int, str, str] | None:
    """(host, port, address, scheme) for a URL that points at a public web
    host -- http(s), a hostname whose every address is public: never
    loopback, private, link-local (the cloud metadata range), multicast or
    reserved space -- else None. The address returned is the one the request
    connects to, so no second lookup can answer differently (review of
    0b360f9e: a hostname changing answers between the check and the request)."""
    import ipaddress
    import socket
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.strip("[]").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return None
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return None
    addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addresses or not all(ip.is_global and not ip.is_multicast for ip in addresses):
        return None
    return host, port, str(addresses[0]), parts.scheme


def public_destination(url: str) -> bool:
    """Whether a URL points at a public web host (see resolve_public)."""
    return resolve_public(url) is not None


def _get_pinned(url: str, host: str, port: int, address: str, scheme: str) -> tuple[int, dict, str]:
    """One GET to the resolved address with the hostname for SNI, certificate
    verification and the Host header: (status, lower-cased headers, body)."""
    import http.client
    import socket
    import ssl
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    raw = socket.create_connection((address, port), timeout=15)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if scheme == "https" else raw
    conn = http.client.HTTPConnection(host, port, timeout=15)
    conn.sock = sock
    try:
        host_header = host if port in (80, 443) else f"{host}:{port}"
        conn.request("GET", path, headers={**_HEADERS, "Host": host_header})
        response = conn.getresponse()
        body = response.read(_MAX_BYTES)
        headers = {k.lower(): v for k, v in response.getheaders()}
    finally:
        conn.close()
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", headers.get("content-type", ""), re.I)
    if m:
        charset = m.group(1)
    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    return response.status, headers, text


def fetch(url: str) -> tuple[str, str] | None:
    """(title, text) of the page fetched directly, or None when the site
    refuses, answers with something that is not HTML, has no readable text
    (an app shell), or is not a public destination on any hop. Each hop is
    resolved once and fetched at that address."""
    from urllib.parse import urljoin
    try:
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            pinned = resolve_public(current)
            if pinned is None:
                logger.info("fetch refused, not a public destination: %s", current[:120])
                return None
            status, headers, text = _get_pinned(current, *pinned)
            if status in (301, 302, 303, 307, 308):
                target = headers.get("location")
                if not target:
                    return None
                current = urljoin(current, target)
                continue
            break
        else:
            return None
        if status >= 400 or "html" not in headers.get("content-type", ""):
            return None
        title, body = extract_text(text)
        return (title, body) if len(body) >= MIN_TEXT_CHARS else None
    except Exception:
        logger.info("direct fetch failed for %s", url[:120], exc_info=True)
        return None


class PageSummary(dspy.Signature):
    """Summarize a web page for a crypto/markets user, strictly from its
    text. Lead with what the page is (the outlet or site, the piece's
    subject, its date if stated), then the substance in a few short
    paragraphs or bullets: claims, figures, names, dates, exactly as the
    page states them. If the user asked something specific about the page,
    answer that first from the text. Never add facts the page does not
    contain; say when the text does not cover part of the ask. No advice."""

    ask: str = dspy.InputField(desc="What the user asked about the page ('summarize' when nothing more)")
    url: str = dspy.InputField()
    title: str = dspy.InputField()
    text: str = dspy.InputField(desc="The page's readable text (may be truncated)")
    summary: str = dspy.OutputField(desc="Markdown")


page_summarizer = dspy.Predict(PageSummary)


def summarize(ask: str, url: str, title: str, text: str) -> str:
    """The page summarized on the primary model, synchronously (this runs
    inside a router tool, on a worker thread)."""
    from app.nodes import runtime

    result = runtime._run_program(page_summarizer, runtime._primary_lm, {"ask": ask or "summarize", "url": url, "title": title or url, "text": text})
    return (getattr(result, "summary", "") or "").strip()


def _ask_from(request: str, url: str) -> str:
    rest = request.replace(url, " ")
    rest = re.sub(r"\s+", " ", rest).strip(" :-,.?!")
    return rest or "summarize"


def read(request: str) -> str:
    """The router tool: the linked page, read and summarized against the
    user's ask, with the source named; or an honest account of why not."""
    match = URL.search(request or "")
    if not match:
        raise ValueError("No URL found")
    url = match.group(0).rstrip(".,)")
    host = urlsplit(url).netloc.removeprefix("www.")
    ask = _ask_from(request, url)
    tried: list[str] = []

    page = fetch(url)
    if page:
        title, text = page
        try:
            summary = summarize(ask, url, title, text)
        except Exception:
            logger.warning("page summary failed for %s", url[:120], exc_info=True)
            summary = ""
        if summary:
            return f"{summary}\n\nSource: [{title or host}]({url})"
        tried.append("read the page but could not summarize it")
    else:
        tried.append(f"{host} did not serve readable page text")

    if perplexity_available():
        try:
            fetched = perplexity_fetch_url(url)
            if fetched and not re.search(r"\b(?:cannot|could not|couldn't|unable to)\s+(?:be\s+)?(?:access|fetch|retrieve|open|load)", fetched[:400], re.I):
                return fetched if url in fetched else f"{fetched}\n\nSource: {url}"
            tried.append("Perplexity's reader could not open it")
        except Exception:
            logger.info("perplexity fetch failed for %s", url[:120], exc_info=True)
            tried.append("Perplexity's reader could not open it")
        try:
            searched = perplexity_web_search(
                f"What does this page or post say? {url} (site: {host}). Summarize its content and claims with dates and figures; "
                f"if the search index has no copy of it, say so plainly. The user asked: {ask}"
            )
            if searched and not re.search(r"\b(?:no|not)\b[^.\n]{0,40}\b(?:copy|record|index|access)", searched[:300], re.I):
                return f"{searched}\n\n_Read from search results about the link; the page itself could not be opened._"
            tried.append("no copy of it in search results")
        except Exception:
            logger.info("web search about %s failed", url[:120], exc_info=True)
            tried.append("no copy of it in search results")
    else:
        tried.append("web search is not configured")

    return (f"I couldn't read {url}: " + "; ".join(tried) + ". "
            "If it is an article or post, paste its text or the key lines and I'll work from that; "
            "if it is a token, name the token or paste its contract address and I'll pull the data directly.")
