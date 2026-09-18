"""The home screen's Memes chip (user, 2026-09-18: "Add meme section and
suggest prompts here. Also get meme token news possible")."""
from app import home_highlights as hh


def test_the_news_parser_keeps_memecoin_stories():
    text = ('{"crypto":[{"headline":"BTC ETF inflows hit $1B","summary":"Inflows of $1B on Thursday.","source":"coindesk.com"}],'
            '"stocks":[{"headline":"Nvidia beats","summary":"Revenue $30B.","source":"reuters.com"}],'
            '"memes":[{"headline":"FARTCOIN reclaims $1B market cap","summary":"Up 18% in a day.","source":"dexscreener.com"},'
            '{"headline":"Pump.fun launches its own AMM","summary":"Volume $400M on day one.","source":"theblock.co"}]}')
    parsed = hh._parse_news(text)
    assert [m["headline"] for m in parsed["memes"]] == ["FARTCOIN reclaims $1B market cap", "Pump.fun launches its own AMM"]


def test_the_memes_chip_leads_with_todays_headlines_then_live_tool_prompts(monkeypatch):
    monkeypatch.setattr(hh, "get_highlights", lambda: {"cards": [], "meme_cards": [
        {"prompt": "What does this mean for memecoins: FARTCOIN reclaims $1B market cap"},
        {"prompt": "What does this mean for memecoins: Pump.fun launches its own AMM"}]})
    categories = hh.suggestions()
    memes = next(c for c in categories if c["id"] == "memes")
    assert memes["label"] == "Memes" and [c["id"] for c in categories][:2] == ["trending", "memes"]
    assert memes["rows"][:2] == ["What does this mean for memecoins: FARTCOIN reclaims $1B market cap",
                                 "What does this mean for memecoins: Pump.fun launches its own AMM"]
    assert "What's bonding on pump.fun right now?" in memes["rows"] and "Deep dive on FARTCOIN" in memes["rows"]
    assert len(memes["rows"]) <= 6


def test_without_news_the_memes_chip_still_has_its_prompts(monkeypatch):
    monkeypatch.setattr(hh, "get_highlights", lambda: {"cards": []})
    memes = next(c for c in hh.suggestions() if c["id"] == "memes")
    assert memes["rows"] == hh.CURATED["memes"]


def test_market_tiles_stay_four_wide_and_meme_cards_ride_separately(monkeypatch):
    monkeypatch.setattr(hh, "perplexity_available", lambda: True)
    monkeypatch.setattr(hh, "perplexity_invoke", lambda tool, q, instr: (
        '{"crypto":[{"headline":"A","summary":"a","source":"x.com"},{"headline":"B","summary":"b","source":"x.com"}],'
        '"stocks":[{"headline":"C","summary":"c","source":"x.com"},{"headline":"D","summary":"d","source":"x.com"}],'
        '"memes":[{"headline":"E","summary":"e","source":"x.com"},{"headline":"F","summary":"f","source":"x.com"}]}'))
    hh._cache.clear() if hasattr(hh, "_cache") else None
    data = hh.get_highlights()
    assert [c["title"] for c in data["cards"]] == ["A", "B", "C", "D"]
    assert [c["title"] for c in data["meme_cards"]] == ["E", "F"] and data["meme_cards"][0]["prompt"].startswith("What does this mean for memecoins: E")
