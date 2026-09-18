"""Transcript of 2026-09-18: a Hyperliquid portfolio turn offered "When did
the 0x6DbA…1460 wallet open the ONDO long position at 10x leverage?" as a
related question. No tool can answer it -- clearinghouseState and every
balances endpoint report current state, never history -- so the gate
refused, and the refusal read: "I couldn't find The answer lacks the date
or time ... for what you asked", then told a user who had already pasted an
address to name a token.
"""
from app import answer_gate, followups

WALLET = "0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460"
POSITIONS = ("# Hyperliquid positions\nAccount value $5.3K, total notional $60.4K.\n"
             "| BTC | Short | 24x | entry $70.4K | liq $159.2K |\n| ONDO | Long | 10x | entry $0.3948 |\n"
             "| HYPE | Long | 10x | entry $78.99 |\nSpot: 38,141 USDC, 16.99 HYPE.")


# --- the follow-ups the card may offer --------------------------------------

def test_a_positions_card_never_offers_a_question_about_when_it_opened():
    for line in [
        f"When did the {WALLET} wallet open the ONDO long position at 10x leverage?",
        "When did this wallet enter the BTC short?",
        "How long has the wallet held the HYPE long?",
        "What date did the wallet buy its HYPE spot balance?",
    ]:
        assert not followups.grounded(line, "wallet portfolio", POSITIONS), line


def test_the_current_state_questions_it_may_still_offer():
    for line in [
        "What is the liquidation price of the BTC short position?",
        "What is the entry price of the ONDO long position?",
        "How much USDC does the wallet hold in spot?",
    ]:
        assert followups.grounded(line, "wallet portfolio", POSITIONS), line


# --- the refusal, when the gate has to write one -----------------------------

def test_the_missing_phrase_reads_inside_a_sentence():
    assert answer_gate._missing_phrase("The answer lacks the date or time when the wallet opened the ONDO long position.") == "the date or time when the wallet opened the ONDO long position"
    assert answer_gate._missing_phrase("The data does not include the token's USD value.") == "the token's USD value"
    assert answer_gate._missing_phrase("The position's open time") == "the position's open time"
    assert answer_gate._missing_phrase("Mercury the token") == "Mercury the token", "a name keeps its capital"
    assert answer_gate._missing_phrase("") == "what you asked for"


def test_a_question_that_already_names_an_address_is_not_asked_to_name_a_token():
    out = answer_gate._could_not_find(
        f"When did the {WALLET} wallet open the ONDO long position at 10x leverage?",
        {"subject": f"{WALLET} wallet ONDO long position leverage",
         "missing": "The answer lacks the date or time when the wallet opened the ONDO long position at 10x leverage."})
    assert out.startswith("I couldn't find the date or time when the wallet opened the ONDO long position at 10x leverage.")
    assert "0x6DbA…1460" in out and "current state" in out and "not its history" in out
    assert "Name the token" not in out
    assert "What I could pull is about" not in out, "a subject that restates the question is noise"


def test_a_question_with_no_address_still_gets_the_naming_step():
    out = answer_gate._could_not_find("Mercury token outlook", {"subject": "Mercury Systems, a defense company", "missing": "Mercury the token"})
    assert out == ("I couldn't find Mercury the token. What I could pull is about Mercury Systems, a defense company. "
                   "Name the token, protocol or company precisely (a $ticker, the full name, or a contract address) and say what you want to know, and I'll look again.")
