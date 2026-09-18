"""A linked page is read three ways and summarized by our own model
(app/url_reader.py); only when all fail does the answer say so."""
from types import SimpleNamespace

import pytest

from app import url_reader
from app.provider_registry import get_provider_router

HTML = """<html><head><title>Coin Bureau: TradingView ships an MCP server</title><style>.x{}</style></head>
<body><nav>Home · Markets · Login</nav><script>window.x=1</script>
<article><h1>TradingView ships an official MCP server</h1>
<p>TradingView announced on September 17, 2026 an official Model Context Protocol server covering crypto and equities.</p>
<p>It exposes symbol search, quotes, technicals and news; access is per user through OAuth. Pricing was not announced.</p>
<ul><li>Crypto and stock coverage</li><li>OAuth 2.1 with dynamic client registration</li></ul>
<p>Analysts noted the move brings charting data to agents without scraping.</p></article>
<footer>© 2026</footer></body></html>""" + "<p>filler line for length</p>" * 20


def test_readable_text_keeps_the_article_and_drops_the_chrome():
    title, text = url_reader.extract_text(HTML)
    assert title == "Coin Bureau: TradingView ships an MCP server"
    assert "TradingView announced on September 17, 2026" in text and "Crypto and stock coverage" in text
    assert "window.x" not in text and "Home · Markets" not in text and "© 2026" not in text


def test_a_page_that_fetches_is_summarized_by_our_model(monkeypatch):
    monkeypatch.setattr(url_reader, "fetch", lambda url: ("Coin Bureau: TradingView ships an MCP server", "TradingView announced ... " * 40))
    seen = {}

    def summarize(ask, url, title, text):
        seen.update(ask=ask, url=url, title=title)
        return "**Coin Bureau** reports TradingView's official MCP server (Sep 17, 2026): crypto and equities, OAuth per user."

    monkeypatch.setattr(url_reader, "summarize", summarize)
    monkeypatch.setattr(url_reader, "perplexity_fetch_url", lambda url: (_ for _ in ()).throw(AssertionError("not needed")))
    out = url_reader.read("what does this say about pricing? https://x.com/coinbureau/status/2100682502344241411")
    assert out.startswith("**Coin Bureau** reports") and out.endswith("Source: [Coin Bureau: TradingView ships an MCP server](https://x.com/coinbureau/status/2100682502344241411)")
    assert seen == {"ask": "what does this say about pricing", "url": "https://x.com/coinbureau/status/2100682502344241411", "title": "Coin Bureau: TradingView ships an MCP server"}


def test_a_blocked_page_falls_to_perplexitys_reader_then_to_search(monkeypatch):
    monkeypatch.setattr(url_reader, "fetch", lambda url: None)
    monkeypatch.setattr(url_reader, "perplexity_available", lambda: True)
    monkeypatch.setattr(url_reader, "perplexity_fetch_url", lambda url: "I cannot access the page at " + url)
    monkeypatch.setattr(url_reader, "perplexity_web_search", lambda q: "The post says TradingView launched an MCP server on Sep 17, 2026 with crypto and equity data.\n\nSources:\n- [x.com](https://x.com/coinbureau/status/1)")
    out = url_reader.read("https://x.com/coinbureau/status/1")
    assert out.startswith("The post says TradingView launched") and "could not be opened" in out

    monkeypatch.setattr(url_reader, "perplexity_fetch_url", lambda url: "TradingView launched an MCP server, per the post at https://x.com/coinbureau/status/1.")
    out = url_reader.read("https://x.com/coinbureau/status/1")
    assert out.startswith("TradingView launched an MCP server")


def test_when_nothing_can_read_it_the_answer_says_what_was_tried(monkeypatch):
    monkeypatch.setattr(url_reader, "fetch", lambda url: None)
    monkeypatch.setattr(url_reader, "perplexity_available", lambda: True)
    monkeypatch.setattr(url_reader, "perplexity_fetch_url", lambda url: "Unable to access the requested URL.")
    monkeypatch.setattr(url_reader, "perplexity_web_search", lambda q: "There is no copy of this page in the search results.")
    out = url_reader.read("summarize https://example.com/private")
    assert out.startswith("I couldn't read https://example.com/private: example.com did not serve readable page text; Perplexity's reader could not open it; no copy of it in search results.")
    assert "paste its text" in out


def test_the_reader_is_the_first_url_tool_and_never_claims_a_link_free_ask():
    router = get_provider_router()
    ranked = [t.name for t in router._ranked_union("summarize https://www.coindesk.com/markets/2026/09/18/story", ("url_fetch",), (), None)]
    assert ranked and ranked[0] == "url_reader" and "perplexity_fetch_url" in ranked
    assert "url_reader" not in [t.name for t in router._ranked_union("summarize the crypto market today", ("web_research",), (), None)]
