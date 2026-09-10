from app.source_cards import extract_source_cards


def test_extracts_deduplicated_markdown_and_bare_sources():
    text = (
        "Published September 9, 2026: [Nvidia filing](https://www.sec.gov/nvda.htm)\n"
        "Duplicate: [SEC](https://www.sec.gov/nvda.htm)\n"
        "More reporting: https://www.reuters.com/technology/nvidia-story"
    )
    cards = extract_source_cards(text)
    assert cards == [
        {
            "url": "https://www.sec.gov/nvda.htm",
            "title": "Nvidia filing",
            "domain": "sec.gov",
            "date": "September 9, 2026",
        },
        {
            "url": "https://www.reuters.com/technology/nvidia-story",
            "title": "www.reuters.com",
            "domain": "reuters.com",
            "date": None,
        },
    ]
