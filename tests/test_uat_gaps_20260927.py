"""The three small UAT gaps from the regression of 2026-09-25: the deployer
question answered from Mobula's records, a venue pair's quote from the feed
("how is ASTER trading on Hyperliquid", "are HOOD tokens up"), and "which of
those" over a holders table."""
from __future__ import annotations

import asyncio

from app import context_entities, deployer_check, mobula_meme, tequity

MINT = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
BIG = "7oU9nR9VEvFPxxxxxxxxxxxxxxxxxxxxxxxxxxxMsDM"
DEPLOYER = "yHCxHBEaJW5tbndqC8JciSThr7U1cqLpdcsvHcx6Kq"


def test_deployer_questions_are_recognised():
    for q in ("Is the largest account the deployer? Show the transaction or say it is not established.", "Is the 49% ANSEM account the deployer?",
              "who deployed BONK", "Who is the deployer of WIF?"):
        assert deployer_check.is_deployer_question(q), q
    for q in ("Who are the top holders?", "deploy my capital into SOL", "What does the deployer wallet tool do?"):
        assert not deployer_check.is_deployer_question(q), q


def test_the_largest_holder_is_compared_with_the_deployer_by_address(monkeypatch):
    rows = [{"walletAddress": BIG, "percentageOfTotalSupply": 49.23}, {"walletAddress": "GV6U" + "1" * 40, "percentageOfTotalSupply": 9.17, "labels": ["proTrader"]}]
    monkeypatch.setattr(mobula_meme, "_get", lambda path, params: rows)
    monkeypatch.setattr(mobula_meme, "token_deployer", lambda mint, chain: DEPLOYER)
    text, traj = asyncio.run(deployer_check.answer(MINT, "solana", "ANSEM"))
    assert text.startswith("No: the largest ANSEM position, `7oU9…MsDM` (49.23% of supply), is not the deployer wallet Mobula's metadata names (`yHCx…x6Kq`).")
    assert "not established" in text and f"`{BIG}`" in text and f"`{DEPLOYER}`" in text and traj["tool_name_0"] == "mobula_token_holders"
    monkeypatch.setattr(mobula_meme, "token_deployer", lambda mint, chain: BIG)
    assert asyncio.run(deployer_check.answer(MINT, "solana", "ANSEM"))[0].startswith("Yes: the largest ANSEM position")
    monkeypatch.setattr(mobula_meme, "token_deployer", lambda mint, chain: None)
    assert asyncio.run(deployer_check.answer(MINT, "solana", "ANSEM"))[0].startswith("Not established: Mobula records no deployer for ANSEM")


def test_the_research_node_answers_the_deployer_question_from_state(monkeypatch):
    from tests.test_routing_rules_20260918 import _security_state, _stub_downstream
    from app.nodes import research as research_mod
    seen = {}
    _stub_downstream(monkeypatch, seen)

    async def fake(mint, chain, symbol=None):
        return f"No: compared {symbol} {mint[:4]} on {chain}", {"tool_name_0": "mobula_token_holders"}
    monkeypatch.setattr(research_mod.deployer_check, "answer", fake)
    focus = {"kind": "token", "label": "ANSEM", "address": MINT, "chain": "solana"}
    out = asyncio.run(research_mod.research_node(_security_state("Is the largest account the deployer? Show the transaction or say it is not established.", session_context={"focus": focus})))
    assert out["answer"] == "No: compared ANSEM 9cRC on solana" and "gathered" not in seen
    out = asyncio.run(research_mod.research_node(_security_state("Is the largest account the deployer?")))
    assert out["answer"].startswith("Which token?")


def test_a_venue_pair_in_a_sentence_and_a_tokenised_stock_reach_the_feeds_quote():
    assert tequity.quote_matches("How is ASTER trading on Hyperliquid?")
    assert tequity.quote_matches("Are HOOD tokens up?")
    assert tequity.quote_matches("hyperliquid btc price")
    assert not tequity.quote_matches("top movers on hyperliquid") and not tequity.quote_matches("what is my exposure to SOL")
    assert tequity.pair_of("How is ASTER trading on Hyperliquid?") == ("hyperliquid", "ASTER")
    assert tequity.pair_of("Are HOOD tokens up?") == ("hyperliquid", "HOOD")
    assert tequity.pair_of("TSLA price on Aster") == ("aster", "TSLA")


def test_which_of_those_reads_a_holders_table():
    card = ("**Taken together**\n\nText.\n\n---\n\n# Token holders\n**Provider**: Mobula\n\n| # | Wallet | Share | Value | Labels |\n|---:|---|---:|---:|---|\n"
            "| 1 | `7oU9…MsDM` | 49.23% | $81.67M | — |\n| 2 | `GV6U…dC52` | 9.17% | $15.21M | proTrader |\n| 3 | `FnzK…L3CC` | 0.78% | $1.30M | liquidityPool |\n")
    items = context_entities.answer_items(card)
    assert items == ["1 · 7oU9…MsDM · 49.23% · $81.67M", "2 · GV6U…dC52 · 9.17% · $15.21M", "3 · FnzK…L3CC · 0.78% · $1.30M"]
    note = context_entities.items_note("Which of those are exchanges or pools, and which are unknown?", {"last_items": items})
    assert "(1) 1 · 7oU9…MsDM · 49.23%" in note and "never substitute a different set" in note


def test_a_short_token_sentence_without_a_quote_word_is_not_a_quote_ask():
    assert not tequity.quote_matches("OPEN token unlock schedule") and not tequity.quote_matches("USELESS token pairs")
    assert tequity.quote_matches("hyperliquid btc price") and tequity.quote_matches("Are HOOD tokens up?")
