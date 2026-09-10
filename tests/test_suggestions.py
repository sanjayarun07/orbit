from app.suggestions import suggested_actions


def test_crypto_trends_get_requested_quick_actions():
    assert suggested_actions("What's trending in crypto right now?") == [
        "Show new pairs on Base",
        "Show trending tokens on Solana",
        "Show trending tokens on Robinhood",
        "View Portfolio",
    ]


def test_general_response_always_has_quick_actions():
    actions = suggested_actions("Explain proof of history")
    assert len(actions) == 4
    assert "View Portfolio" in actions


def test_current_action_is_not_repeated():
    actions = suggested_actions("Show the Bitcoin price now")
    assert "Show the Bitcoin price now" not in actions


def test_short_followup_uses_previous_wallet_context():
    actions = suggested_actions(
        "What are the risks?",
        "research",
        "user: Analyze 0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460\n"
        "assistant: The wallet has leveraged positions.",
    )
    address = "0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460"
    assert actions == [
        f"Show current portfolio for {address}",
        f"Review leverage risk for {address}",
        f"Show recent transactions for {address}",
        f"Check counterparties for {address}",
    ]


def test_wallet_actions_carry_address_and_chain_from_current_answer():
    wallet = "0x8C7d8CA8DFDb1d55C2b2bc549B3b9f70cE5e0323"
    actions = suggested_actions(
        f"{wallet} this address",
        "research",
        "",
        ["wallet_intelligence"],
        f"The wallet {wallet} holds USDC on Base.",
    )
    assert actions[2] == f"Show recent transactions for {wallet} on Base"


def test_explicit_new_topic_does_not_inherit_stale_context():
    actions = suggested_actions(
        "Explain the system architecture",
        "general",
        "user: Analyze 0x6DbA597fe4A47F97F1f0C32feEC4bf6Aea11460",
    )
    assert actions == [
        "Show the request routing flow",
        "Explain provider fallback",
        "Explain the wallet security model",
        "Open the admin dashboard",
    ]


def test_no_context_keeps_default_actions():
    assert suggested_actions("Explain proof of history", "general", "") == [
        "What's trending in crypto right now?",
        "Show the Bitcoin price now",
        "Research SOL token safety",
        "View Portfolio",
    ]


def test_price_followup_inherits_price_context_not_generic_news_context():
    actions = suggested_actions(
        "Tell me more",
        "research",
        "user: Bitcoin price now\nassistant: Bitcoin is trading at $78,000.",
    )
    assert actions[0] == "Compare Bitcoin and Ethereum"


def test_token_address_gets_token_actions_not_wallet_actions():
    address = "0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460"
    actions = suggested_actions(
        f"Show token details for {address}",
        "research",
        "",
        ["token_discovery", "token_security"],
    )
    assert actions == [
        f"Check token safety for {address}",
        f"Show liquidity and volume for {address}",
        f"Show top holders for {address}",
        f"Show recent DEX trades for {address}",
    ]
    assert all("wallet" not in action.lower() for action in actions)


def test_token_answer_puts_resolved_solana_mint_into_quick_actions():
    mint = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
    actions = suggested_actions(
        "ANSEM token on Solana",
        "research",
        "",
        ["web_research"],
        f"Solana mint address: `{mint}`",
    )
    assert actions[2] == f"Show top holders for {mint} on Solana"


def test_trade_plan_actions_prefer_output_token_over_input_token():
    input_mint = "So11111111111111111111111111111111111111112"
    output_mint = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
    actions = suggested_actions(
        "Swap .01 SOL to ANSEM with 50 bps slippage",
        "trade",
        f"assistant: Solana mint address: `{output_mint}`",
        ["swap", "token_resolve", "token_security"],
        f'Mint "{input_mint}" was not found uniquely',
        output_mint,
        "solana",
    )
    assert all(output_mint in action for action in actions)
    assert all(input_mint not in action for action in actions)


def test_equity_actions_keep_the_ticker_context():
    actions = suggested_actions(
        "What is driving $NVDA's recent move and what is the outlook?",
        "research",
        "",
        ["equity_research"],
    )
    assert actions == [
        "Show the latest earnings and guidance for NVDA",
        "Review valuation and fundamentals for NVDA",
        "Summarize recent news and catalysts for NVDA",
        "Compare NVDA with its closest listed peers",
    ]


def test_relay_explanation_actions_are_conceptual_and_contextual():
    assert suggested_actions("how relay bridge works?", "general") == [
        "Show supported Relay chains",
        "Compare bridge fees from Base to Solana",
        "Explain wallet approval security",
        "Start a cross-chain swap",
    ]


def test_relay_actions_keep_output_token_and_destination_chain():
    actions = suggested_actions(
        "Swap 0.1 SOL on Solana to USDC on Base",
        "cross_chain_swap",
        "",
        ["cross_chain_swap", "token_resolve", "token_security"],
        "",
        "USDC",
        "base",
    )
    assert actions[0] == "Check token safety for USDC on Base"
    assert actions[1] == "Show liquidity and volume for USDC on Base"


def test_new_named_token_does_not_inherit_old_address_in_actions():
    stale = "0x8260000000000000000000000000000000000087"
    actions = suggested_actions(
        "what is ANSEM token",
        "research",
        f"user: Check token safety for {stale} on Robinhood",
        ["token_discovery"],
    )
    assert all(stale not in action for action in actions)
    assert all("ANSEM" in action for action in actions)
