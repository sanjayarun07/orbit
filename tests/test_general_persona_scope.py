"""The general-chat persona names the product's real scope.

The signature docstring is the model's only framing for greetings; when it said
"conceptual questions about Solana", the live greeting became "How can I assist
you with Solana or crypto-related questions today?". Orbit is multi-chain.
"""
from app.nodes.runtime import GeneralAnswer


def test_the_general_persona_is_multi_chain_markets_not_solana_only():
    doc = GeneralAnswer.__doc__
    assert "across chains" in doc and "never frame the assistant as Solana-only" in doc
    assert "conceptual questions about Solana" not in doc
    for scope in ("market data", "wallets", "news", "swaps"):
        assert scope in doc, f"the persona no longer mentions {scope}"
