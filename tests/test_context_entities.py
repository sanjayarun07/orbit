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
