"""The frozen trust re-run of 2026-09-23 (reports/trust-rerun-7343e407): a
conversation's subject follows a follow-up into the answer check, an
indefinite noun is not the conversation's asset, and a question about how
wallets are read is a product answer."""
from app import product_actions
from app.routing import subject_probe


def test_an_indefinite_noun_is_a_generic_subject_not_the_focus():
    # "Can I see a wallet without giving you control of it?" after PEPE turns
    # fetched the portfolio of the PEPE contract in focus.
    assert not subject_probe.continues_subject("Can I see a wallet without giving you control of it?")
    assert not subject_probe.continues_subject("is any token safe to hold for a year?")
    assert subject_probe.continues_subject("Who controls the supply? Separate pools, exchanges, burn addresses and deployer-linked wallets. Show evidence for each label.")
    assert subject_probe.continues_subject("Were early buys bundled or sniped?")


def test_how_a_wallet_is_read_is_a_product_question():
    q = "Can I see a wallet without giving you control of it?"
    assert product_actions.is_product_question(q) and product_actions.matches(q)
    assert product_actions.answer(q).startswith("**Yes: a wallet can be read from its public address alone.**")
    assert product_actions.answer("Can I look at someone's portfolio read-only?") == product_actions.WALLET_VIEW
    for data_ask in ("show me the wallet's holdings", "what does this wallet hold", "is the token safe"):
        assert not product_actions.is_product_question(data_ask)


def test_the_answer_gate_reads_the_request_with_its_resolved_subject():
    # "Who controls the supply?" after an ANSEM turn: judged on the bare text,
    # a correct holders answer was called off-subject and replaced by a web
    # reply that could not know the token.
    import inspect
    from app.nodes import research
    src = inspect.getsource(research.research_node)
    assert "answer_gate.gate(_effective_request(state), result)" in src and 'answer_gate.gate(state["request"]' not in src


def test_what_happens_on_a_failed_quote_is_a_product_answer_from_the_code():
    # Answered with the trading-policy card in the frozen run.
    q = "What happens if the quote provider times out or there is no route? Does your system treat either as zero holdings or authorize a retry trade?"
    assert product_actions.is_product_question(q) and product_actions.answer(q) == product_actions.QUOTE_FAILURE
    assert "never zero" in product_actions.QUOTE_FAILURE and "Nothing retries a trade" in product_actions.QUOTE_FAILURE
    for ask in ("quote me 1 SOL to USDC", "why did my quote fail yesterday on jupiter", "best route for 100 USDC to SOL"):
        assert not product_actions.is_product_question(ask)


def test_a_tokens_pools_with_a_liquidity_floor_are_state_not_web():
    # "Find BONK pools on Solana with at least $1000000 liquidity" went to the
    # web and repeated a $239.5M pool from a tracker page.
    from app import contracts, fact_gate, facts, tool_catalog
    c = contracts.plan_by_rules("Find BONK pools on Solana with at least $1000000 liquidity. Explain how yields change.")
    assert (c.kind, c.metric, c.subject.symbol, c.filters.get("min_liquidity_usd")) == ("market_ranking", "liquidity", "BONK", 1_000_000.0)
    assert [n for n, r in tool_catalog.eligible_tools(c) if r == "eligible"] == ["dexscreener_token_pairs"]
    chain_wide = contracts.plan_by_rules("top gainers on Base in the last 24h")
    assert dict(tool_catalog.eligible_tools(chain_wide))["dexscreener_token_pairs"] == "reads one named token, no token in the ask"
    card = ("# Pairs for BONK\n**Provider**: DEX Screener · **Checked**: 2026-09-23 18:00 UTC\n\n"
            "| Pair | Chain / DEX | Priced token | Price | 24h volume | Liquidity | 24h price change |\n|---|---|---|---:|---:|---:|---:|\n"
            "| BONK/SOL | solana / orca | BONK | $0.00002 | $1.2M | $4.03M | -1.2% |\n| BONK/USDC | solana / meteora | BONK | $0.00002 | $0.1M | $416.47K | -1.1% |\n"
            "| BONK/SOL | solana / raydium | BONK | $0.00002 | $2.0M | $2.9M | -1.3% |\n| BONK/USDC | solana / meteora | BONK | $0.00002 | $0.9M | $3.24M | -1.0% |\n")
    rows = facts.facts_from_card("dexscreener_token_pairs", card, "market_ranking")
    assert fact_gate.check(c, rows).ok
    thin = [r for r in rows if r.attrs["liquidity_usd"] < 1_000_000] * 3
    assert not fact_gate.check(c, thin).ok
