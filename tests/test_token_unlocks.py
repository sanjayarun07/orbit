"""Transcript of 2026-09-18: "OPEN token unlock schedule" was answered with a
DEX pair search. The deep dive knew DefiLlama's emissions data, but no tool
claimed an unlock question on its own; defillama_token_unlocks does. It finds
the slug by ticker in DefiLlama's index (ARB is arbitrum, never the
arbitrum-exchange namesake), never lends a namesake's schedule to a token
whose contract is known, and when DefiLlama does not list the token at all
it answers from the web the way Perplexity does (OpenLedger's vesting terms
from its own docs) instead of "not tracked"."""
import time

import pytest

from app import token_unlocks


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    token_unlocks.reset()
    token_unlocks._unlock_list_cache = None
    monkeypatch.setattr(token_unlocks, "_web_lookup", lambda symbol, request: None)
    yield
    token_unlocks.reset()
    token_unlocks._unlock_list_cache = None


@pytest.mark.parametrize("text,expected", [
    ("OPEN token unlock schedule", True), ("when is the next ARB unlock", True), ("JUP vesting", True), ("$WIF cliff", True),
    ("unlock schedule for 0x1234567890abcdef1234567890abcdef12345678", True),
    ("price of OPEN", False), ("unlock schedule", False), ("open the vault", False),
])
def test_an_unlock_ask_names_a_token(text, expected):
    assert token_unlocks.matches(text) is expected


def _schedule(token_ref, future_days=(7, 30, 90), past_days=(30,)):
    now = time.time()
    events = [{"timestamp": now + d * 86400, "noOfTokens": [1_000_000 * (i + 1)], "unlockType": "cliff", "category": "investors"} for i, d in enumerate(future_days)]
    events += [{"timestamp": now - d * 86400, "noOfTokens": [500_000], "unlockType": "linear", "category": "team"} for d in past_days]
    return {"metadata": {"token": token_ref, "events": events}}


LEDGER = "0x" + "ab" * 20
OPEN_ETH = "0x" + "cd" * 20
INDEX = [
    {"slug": "open", "name": "Open", "symbol": "OPEN", "token": f"ethereum:{OPEN_ETH}"},
    {"slug": "openledger", "name": "OpenLedger", "symbol": "OPEN", "token": f"bsc:{LEDGER}"},
    {"slug": "arbitrum", "name": "Arbitrum", "symbol": "ARB", "token": "arbitrum:0x912ce59144191c1204e64559fe8253a0e49e6548"},
    {"slug": "arbitrum-exchange", "name": "Arbitrum Exchange", "symbol": "ARX", "token": "arbitrum:0xd5954c3084a1ccd70b4da011e67b7c9a1cd9fad7"},
]
SCHEDULES = {"open": _schedule(f"ethereum:{OPEN_ETH}"), "openledger": _schedule(f"bsc:{LEDGER}"),
             "arbitrum": _schedule("arbitrum:0x912ce59144191c1204e64559fe8253a0e49e6548"), "arbitrum-exchange": _schedule("arbitrum:0xd5954c3084a1ccd70b4da011e67b7c9a1cd9fad7")}


def _use_index(monkeypatch, calls=None):
    monkeypatch.setattr(token_unlocks, "_unlock_index", lambda: INDEX)

    def fetch(url, timeout=20.0):
        if calls is not None:
            calls.append(url)
        return SCHEDULES[url.rsplit("/", 1)[1]]

    monkeypatch.setattr(token_unlocks, "_http_json", fetch)


def test_the_schedule_is_a_dated_table_verified_against_the_contract(monkeypatch):
    calls = []
    _use_index(monkeypatch, calls)
    card = token_unlocks.unlock_schedule(f"OPEN token unlock schedule {LEDGER} on bsc")
    assert card.startswith("# Unlock schedule — OPEN") and "`openledger`" in card and "verified" in card
    assert "| cliff | investors |" in card and "1,000,000 tokens" in card and "Recent unlocks" in card
    assert "`open`" not in card, "the namesake slug whose token is not this contract is never shown"


def test_a_ticker_finds_its_own_slug_never_a_prefix_namesake(monkeypatch):
    """Live (2026-09-18): "when is the next ARB unlock" matched the slug
    arbitrum-exchange by prefix and showed 0-token events for a namesake."""
    calls = []
    _use_index(monkeypatch, calls)
    card = token_unlocks.unlock_schedule("when is the next ARB unlock")
    assert "`arbitrum`" in card and "arbitrum-exchange" not in card
    assert all("arbitrum-exchange" not in url for url in calls), "the namesake is not even fetched"


def test_without_a_contract_every_matching_entry_is_shown_with_its_token(monkeypatch):
    _use_index(monkeypatch)
    card = token_unlocks.unlock_schedule("OPEN token unlock schedule")
    assert f"`ethereum:{OPEN_ETH}`" in card and f"`bsc:{LEDGER}`" in card and "Several DefiLlama entries match" in card


def test_an_untracked_token_is_answered_from_the_web_like_perplexity(monkeypatch):
    """DefiLlama lists no OpenLedger entry at all; Perplexity's answer to the
    same question was the project's own vesting table. Ours now is too."""
    monkeypatch.setattr(token_unlocks, "_unlock_index", lambda: [r for r in INDEX if r["symbol"] != "OPEN"])
    monkeypatch.setattr(token_unlocks, "_http_json", lambda url, timeout=20.0: (_ for _ in ()).throw(AssertionError("nothing to fetch")))
    asked = []

    def web(symbol, request):
        asked.append((symbol, request))
        return "OpenLedger (OPEN): 1B total supply; investors 182.9M with a 12-month cliff then 36 months monthly vesting.\n\nSources:\n- [docs](https://docs.openledger.foundation)"

    monkeypatch.setattr(token_unlocks, "_web_lookup", web)
    card = token_unlocks.unlock_schedule("OPEN token unlock schedule")
    assert asked == [("OPEN", "OPEN token unlock schedule")]
    assert card.startswith("# Unlock schedule — OPEN") and "web search" in card and "12-month cliff" in card and "docs.openledger.foundation" in card
    assert "DefiLlama does not track an unlock schedule for OPEN" in card


def test_an_untracked_token_without_web_search_says_so_instead_of_guessing(monkeypatch):
    monkeypatch.setattr(token_unlocks, "_unlock_index", lambda: [r for r in INDEX if r["symbol"] == "ARB"])
    monkeypatch.setattr(token_unlocks, "_http_json", lambda url, timeout=20.0: (_ for _ in ()).throw(AssertionError("nothing to fetch")))
    card = token_unlocks.unlock_schedule("BONK unlock schedule")
    assert "does not track an unlock schedule for BONK" in card
