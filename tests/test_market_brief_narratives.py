"""'What are the trending narratives right now?' is a story question: the
brief leads with the market-wide narratives from web research, keeps the
DEX Screener metas as the on-chain lens, and drops the chain-volume table and
promoted tokens that headed the old answer."""
from types import SimpleNamespace

from app import market_brief


def _stub_http(monkeypatch):
    def fake_get(url):
        if "prices/current" in url:
            return {"coins": {"coingecko:bitcoin": {"price": 75500}, "coingecko:ethereum": {"price": 2400}}}
        if url.endswith("overview/dexs?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"):
            return {"total24h": 10_850_000_000, "change_1d": 4.5, "change_7d": -7.1}
        if "token-boosts" in url:
            return []
        return {"total24h": 1_000_000_000, "change_1d": 1.0, "change_7d": 2.0}

    monkeypatch.setattr(market_brief, "_get_json", fake_get)


class _Router:
    def __init__(self):
        self.calls = []

    def try_route(self, request, capability, chains):
        self.calls.append((capability, request))
        if capability == "web_research":
            return SimpleNamespace(output="**AI agents** lead the week as launchpads rotate; **RWA** inflows continue [1]; **perp DEXs** set volume records [2].")
        return SimpleNamespace(output="# Trending crypto narratives\n| Narrative | Volume |\n|---|---:|\n| Cat | $42.4M |")


def test_narrative_ask_leads_with_the_story_and_keeps_metas(monkeypatch):
    _stub_http(monkeypatch)
    router = _Router()
    monkeypatch.setattr(market_brief, "get_provider_router", lambda: router)
    market_brief._cache.clear()
    out = market_brief.crypto_market_brief("What are the trending narratives right now in crypto?")
    assert out.startswith("# Crypto narratives right now")
    assert out.index("## What the market is trading on") < out.index("AI agents") < out.index("## On-chain metas") < out.index("| Cat |") < out.index("## Pulse")
    assert "Where the volume is" not in out and "Promoted-token attention" not in out
    assert "BTC $75.5K" in out and "DEX volume $10.85B" in out
    assert [c for c, _ in router.calls] == ["token_discovery", "web_research"] or [c for c, _ in router.calls] == ["web_research", "token_discovery"]


def test_token_asks_keep_the_full_brief(monkeypatch):
    _stub_http(monkeypatch)
    router = _Router()
    monkeypatch.setattr(market_brief, "get_provider_router", lambda: router)
    market_brief._cache.clear()
    out = market_brief.crypto_market_brief("trending tokens on solana")
    assert out.startswith("# SOLANA crypto market brief") and "## Narratives and protocols" in out
    assert [c for c, _ in router.calls] == ["token_discovery"]        # no web call for a token ask
    assert market_brief._narratives_mode("what narratives are hot in crypto") and not market_brief._narratives_mode("trending narrative tokens")


def test_story_outage_degrades_to_the_on_chain_view(monkeypatch):
    _stub_http(monkeypatch)

    class Down(_Router):
        def try_route(self, request, capability, chains):
            if capability == "web_research":
                raise RuntimeError("perplexity down")
            return super().try_route(request, capability, chains)

    monkeypatch.setattr(market_brief, "get_provider_router", lambda: Down())
    market_brief._cache.clear()
    out = market_brief.crypto_market_brief("crypto narratives this week")
    assert "Web research is unavailable" in out and "| Cat |" in out
