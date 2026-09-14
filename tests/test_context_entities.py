from app.context_entities import extract_token_reference, extract_wallet_reference, resolve_contextual_request


MINT = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


def test_extracts_markdown_solana_mint_from_answer():
    reference = extract_token_reference(f"**Solana mint address:** `{MINT}`")
    assert reference.address == MINT
    assert reference.chain == "solana"


def test_resolves_this_token_followup_from_chat_history():
    history = (
        "user: ANSEM token on Solana\n"
        f"assistant: The Black Bull. Solana mint address: `{MINT}`"
    )
    resolved = resolve_contextual_request("Show this token's top holders", history)
    assert f"token {MINT} on solana" in resolved


def test_does_not_override_an_explicit_address():
    current = "Show top holders for 0x1111111111111111111111111111111111111111"
    assert resolve_contextual_request(current, f"assistant: Solana mint address: {MINT}") == current


def test_resolves_wallet_followup_without_using_linked_token_contract():
    wallet = "0x8C7d8CA8DFDb1d55C2b2bc549B3b9f70cE5e0323"
    usdc = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    history = (
        f"user: {wallet} this address\n"
        f"assistant: Wallet [profile](https://app.nansen.ai/profiler?address={wallet}) "
        f"holds [USDC](https://app.nansen.ai/token-god-mode?tokenAddress={usdc}&chain=base) on Base."
    )
    reference = extract_wallet_reference(history)
    assert reference.address == wallet
    assert reference.chain == "base"
    resolved = resolve_contextual_request("Show this wallet's recent transactions", history)
    assert f"wallet {wallet} on base" in resolved


def test_article_identifier_is_not_a_solana_wallet_address():
    article = "https://example.com/article/4139b7912dbf1eebdcfde8c1cc12f23d"
    assert extract_wallet_reference(f"Recent transactions: {article}") is None


def _bonk_focus():
    return {"focus": {"kind": "token", "label": "BONK", "address": MINT, "chain": "solana"}}


def test_pronoun_trade_targets_resolve_against_canonical_focus():
    """Verified live this previously resolved to nothing at all -- unlike
    restating the exact symbol ("sell BONK"), which was already handled.
    """
    history = "user: What is the price of BONK?\nassistant: BONK is trading now."
    for request in ("sell it", "buy that", "trade this", "swap that one"):
        resolved = resolve_contextual_request(request, history, _bonk_focus())
        assert f"token BONK" in resolved and MINT in resolved, request


def test_pronoun_trade_target_handles_a_quantity_phrase():
    """Regression: _NAMED_TRADE_TARGET originally captured the quantity word
    ("half") instead of the pronoun for "sell half of it" -- verified live
    before the fix, group(1) was "half", not "it".
    """
    history = "user: What is the price of BONK?\nassistant: BONK is trading now."
    for request in ("sell half of it", "swap 50% of it", "sell all of that"):
        resolved = resolve_contextual_request(request, history, _bonk_focus())
        assert MINT in resolved, request


def test_pronoun_research_followups_resolve_against_canonical_focus():
    history = "user: What is the price of BONK?\nassistant: BONK is trading now."
    for request in ("how about that one", "tell me about it", "what about this", "analyze that", "check it"):
        resolved = resolve_contextual_request(request, history, _bonk_focus())
        # "check it" resolves via the pre-existing _TOKEN_FOLLOWUP branch,
        # which only ever included the mint, not the label -- matches its
        # existing behavior for "this token" etc., not a new inconsistency.
        assert MINT in resolved, request


def test_pronoun_resolution_requires_a_canonical_focus():
    """No focus at all -- must return the request unmodified, same as any
    other unresolvable follow-up, not guess.
    """
    history = "user: hello\nassistant: hi there"
    for request in ("sell it", "tell me about it", "how about that one"):
        assert resolve_contextual_request(request, history, {}) == request, request


def test_pronoun_resolves_against_the_latest_focus_not_a_stale_one():
    """focus already tracks only the single most recent subject
    (app/experience.py's advance_session_context) -- pronoun resolution
    inherits that for free, exercised here against a second, different
    token than the one in test_pronoun_trade_targets_resolve_against_canonical_focus.
    """
    wif_mint = "WifMint111111111111111111111111111111111"
    history = "user: What about WIF?\nassistant: WIF is trading now."
    focus = {"focus": {"kind": "token", "label": "WIF", "address": wif_mint, "chain": "solana"}}
    resolved = resolve_contextual_request("sell it", history, focus)
    assert wif_mint in resolved
    assert MINT not in resolved


def test_named_trade_target_still_resolves_when_symbol_is_restated():
    """Non-regression: the pre-existing exact-symbol-restated path must
    keep working after the trade-target branch was restructured to also
    accept pronouns.
    """
    history = "user: What is the price of BONK?\nassistant: BONK is trading now."
    resolved = resolve_contextual_request("sell BONK", history, _bonk_focus())
    assert "token BONK" in resolved and MINT in resolved


def test_pronoun_does_not_resolve_against_a_wallet_focus_as_a_token():
    """A pronoun trade target must not silently resolve to a wallet address
    -- the trade-target branch is gated on focus.kind == "token" already,
    this just confirms the pronoun path respects that same gate.
    """
    history = "user: Show my wallet\nassistant: Here it is."
    wallet_focus = {"focus": {"kind": "wallet", "address": "0xWALLET", "chain": "base"}}
    request = "sell it"
    assert resolve_contextual_request(request, history, wallet_focus) == request


def test_evm_wallet_is_never_tagged_solana_even_when_solana_appears_in_the_text():
    """A 0x address can never exist on Solana. _chain_for used to take the
    *last* chain word anywhere in the combined text with no regard for
    whether it was even a valid chain for this address -- a wallet whose
    portfolio answer separately mentions Hyperliquid/Solana elsewhere (e.g.
    a different section of the same reply) got mislabeled 'solana' in the
    context chip despite being an unambiguous EVM address.
    """
    wallet = "0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460"
    history = (
        f"user: Show current portfolio for {wallet}\n"
        f"assistant: Wallet [profile](https://app.nansen.ai/profiler?address={wallet}) -- "
        "Hyperliquid account value is $5.2k. Spot balances (via Bitquery, Solana only): none found."
    )
    reference = extract_wallet_reference(history)
    assert reference.address == wallet
    assert reference.chain != "solana"
