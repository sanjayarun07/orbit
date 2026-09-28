"""The three answer-quality gaps closed on the 5b85ec5a candidate (2026-09-28):
concentration figures keep their source and basis in the deep dive, "new
today" is judged by event date with recaps labelled, and a bare short ticker
on a lesser coin asks which asset is meant."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app import contracts, equities_data, fact_gate, tequity, token_deepdive, why_moving

EVIDENCE = ("# Token holders\n**Top 10 positions hold 38.37% of supply** (of the 50 largest positions indexed, pools, exchanges and burn addresses included)\n\n"
            "# Rug risk\n## Holders\n- **Top 10**: 30.92% of supply (Mobula's security profile, with its own exclusions; the holders table counts every position) · **Top 50**: 17.47% · **Top 100**: 22.10%\n")


def test_a_merged_or_unsourced_concentration_claim_is_removed_and_the_sourced_figures_stated():
    answer = ("Bonk is a verified SPL token.\n"
              "| Holder Structure | Top 10 hold 38.37%, top 50 hold 17.47% | mobula_token_holders | Elevated |\n"
              "- Top 10 holders hold approx. 38.37% (Mobula index), or 30.92% (Jupiter audit); elevated but not extreme concentration.\n"
              "- Top 50 hold ~50%+ combined.\n"
              "Liquidity is pullable.")
    assert token_deepdive.concentration_conflicts(answer) == ["| Holder Structure | Top 10 hold 38.37%, top 50 hold 17.47% | mobula_token_holders | Elevated |"]
    audited = token_deepdive.audit_concentration(answer, EVIDENCE)
    assert "top 50 hold 17.47%" not in audited and "Top 50 hold ~50%+" not in audited            # merged bases, and a figure no card carries
    assert "Top 10 holders hold approx. 38.37% (Mobula index), or 30.92% (Jupiter audit)" in audited   # both figures are the cards', with their sources
    assert "Bonk is a verified SPL token." in audited and "Liquidity is pullable." in audited
    assert "**Holder concentration, as the sources state it**" in audited
    assert "Top 10 positions hold 38.37% of supply (of the 50 largest positions indexed" in audited and "Top 50: 17.47%" in audited
    assert "removed from the written analysis: 2 statements" in audited
    assert token_deepdive.audit_concentration("No concentration figure here.", EVIDENCE) == "No concentration figure here."
    assert "never merge figures from two sources" in token_deepdive.ANALYSIS_RULES


def test_older_events_told_as_new_are_recaps_by_event_date():
    now = datetime(2026, 9, 27, 21, 0, tzinfo=timezone.utc)      # a Saturday
    answer = ("**Taken together**\n\nIn the last 24 hours on 2026-09-27, Bitcoin continued a strong rally with an intraday peak of $87,265 on Wednesday [1]. "
              "The biggest security event was a major hack on Bitget on Thursday, with losses of $387.5 million [1]. "
              "Bitcoin ETFs saw $2.4 billion in weekly net inflows through 25 September [8]. "
              "Recap: the SEC postponed its ETF options decision on September 24 [8]. "
              "CoinDesk reported Vitalik Buterin's Ethereum 2030 vision on September 27 [2].\n\n---\n\n# From the web\n[1] x · 2026-09-27")
    found = fact_gate.recap_sentences(answer, 24, now)
    assert [f.split(":")[0] for f in found] == ["2026-09-23", "2026-09-24", "2026-09-25"]
    assert all("Vitalik" not in f and "Recap" not in f for f in found)
    assert fact_gate.recap_sentences(answer, 24 * 30, now) == []            # a month's window: nothing is a recap
    plan = contracts.plan_by_rules("What's actually new in crypto today?")
    assert plan.kind == "recent_events" and plan.window_hours == 24
    gate = fact_gate.check(plan, [], answer, scope_satisfied=True, now=now)
    assert len(gate.recap) == 3
    assert fact_gate.prose_event_dates("it happened on Sunday", now)[0].date() == now.date()          # 2026-09-27 is a Sunday: today
    assert fact_gate.prose_event_dates("it happened on Saturday", now)[0].date() == datetime(2026, 9, 26, tzinfo=timezone.utc).date()


def test_a_bare_short_ticker_on_a_lesser_coin_asks_which_asset(monkeypatch):
    from app import symbol_registry
    monkeypatch.setattr(symbol_registry, "listed", lambda sym: [{"name": "Backpack", "symbol": "BP", "rank": 135}])
    monkeypatch.setattr(symbol_registry, "leader", lambda rows: rows[0])
    monkeypatch.setattr(equities_data, "enabled", lambda: True)
    monkeypatch.setattr(equities_data, "is_listed_ticker", lambda sym: False)      # the gateway's allowlist cannot say BP plc does not exist
    monkeypatch.setattr(tequity, "resolve_pair", lambda venue, sym: None)
    ask, hedge = asyncio.run(why_moving.namesake("BP", "Why is BP moving today?"))
    assert ask.startswith("**BP** can mean a listed company (ticker BP) or the token **Backpack**") and hedge is None
    assert asyncio.run(why_moving.namesake("BP", "why is BP token moving")) == (None, None)
    monkeypatch.setattr(symbol_registry, "listed", lambda sym: [{"name": "Ansem", "symbol": "ANSEM", "rank": 1400}])
    ask, hedge = asyncio.run(why_moving.namesake("ANSEM", "Why is ANSEM moving?"))
    assert ask is None and hedge.startswith("_ANSEM here is the token **Ansem**")        # five letters, no stock signal: the coin, under a lead line


def test_the_recap_check_reads_the_summary_never_the_cards_or_source_lists():
    now = datetime(2026, 9, 27, 21, 0, tzinfo=timezone.utc)
    answer = ("**Taken together**\n\nBitget resumed withdrawals on September 27 [1].\n[8] Cryptocurrency News · 2026-09-24\n\n---\n\n"
              "# From the web\nOn Wednesday Bitcoin peaked [1].\n\nSources:\n[1] [x](https://x) · 2026-09-23\n[9] Top News · 2024-12-09")
    assert fact_gate.recap_sentences(answer, 24, now) == []


def test_a_rounded_share_or_a_threshold_stands_and_only_the_offending_sentence_goes():
    answer = ("Bonk has a large holder base, but the top 10 holders control around 31–38% of supply, indicating elevated concentration (top 10 >30%). "
              "Top 50 hold ~50%+ combined. Liquidity is pullable.")
    audited = token_deepdive.audit_concentration(answer, EVIDENCE)
    assert "top 10 holders control around 31–38% of supply" in audited and "Liquidity is pullable." in audited
    assert "Top 50 hold ~50%+" not in audited and "removed from the written analysis: 1 statement " in audited
