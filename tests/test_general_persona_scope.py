"""The general-chat persona names the product's real scope.

The signature docstring is the model's only framing for greetings; when it said
"conceptual questions about Solana", the live greeting became "How can I assist
you with Solana or crypto-related questions today?". Orbit is multi-chain.
"""
from app.nodes.runtime import GeneralAnswer


def test_the_general_persona_is_multi_chain_markets_not_solana_only():
    doc = " ".join(GeneralAnswer.__doc__.split())   # the docstring wraps; match on words, not line breaks
    assert "across chains" in doc and "never frame the assistant as Solana-only or crypto-only" in doc
    assert "conceptual questions about Solana" not in doc
    # Equities are a real capability (equity_research, the instrument registry,
    # "why is NVDA moving"), so the persona names them too.
    for scope in ("market data", "equities", "stocks", "wallets", "news", "swaps"):
        assert scope in doc, f"the persona no longer mentions {scope}"
