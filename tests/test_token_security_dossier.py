import pytest

from app import provider_registry


_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@pytest.fixture(autouse=True)
def _no_web(monkeypatch):
    # Keep the on-chain-dossier tests hermetic; the web section is exercised
    # separately with a mocked search.
    monkeypatch.setattr(provider_registry, "perplexity_available", lambda: False)


def _identity(**over):
    base = {
        "id": _MINT, "name": "USD Coin", "symbol": "USDC", "decimals": 6,
        "tokenProgram": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "isVerified": True, "tags": ["verified", "strict", "stable"],
        "organicScore": 100, "organicScoreLabel": "high", "holderCount": 5248202,
        "mintAuthority": "BJE5MMbqXjVwjAF7oxwPYXnTXDyspzZyt4vwenNw5ruG",
        "freezeAuthority": "7dGbd2QZcCKcTndnHcTL8q7SMVXAkp688NTQYwrRCrar",
        "audit": {"topHoldersPercentage": 25.63, "devBalancePercentage": 2.4e-07, "devMints": 1},
    }
    base.update(over)
    return base


def test_dossier_combines_identity_and_shield(monkeypatch):
    monkeypatch.setattr(provider_registry, "token_identity", lambda m: _identity())
    monkeypatch.setattr(provider_registry, "token_safety_warnings",
                        lambda m: {"warnings": {"freeze": [{"message": "can freeze your token account"}],
                                               "mint": [{"message": "can mint more tokens"}]}})
    out = provider_registry._solana_token_security(f"safety of USDC mint {_MINT}")
    # Identity
    assert "USD Coin (USDC)" in out and "Standard SPL Token" in out
    assert "Jupiter verified | Yes" in out
    assert "100/100 (high)" in out and "5,248,202" in out
    # Safety with the actual authority addresses + concentration + shield
    assert "BJE5MMbqXjVwjAF7oxwPYXnTXDyspzZyt4vwenNw5ruG" in out
    assert "7dGbd2QZcCKcTndnHcTL8q7SMVXAkp688NTQYwrRCrar" in out
    assert "~25.63%" in out
    assert "can freeze your token account" in out
    assert "Verification rule" in out


def test_dossier_reports_renounced_authorities_and_no_flags(monkeypatch):
    monkeypatch.setattr(provider_registry, "token_identity",
                        lambda m: _identity(mintAuthority=None, freezeAuthority=None))
    monkeypatch.setattr(provider_registry, "token_safety_warnings", lambda m: {"warnings": {}})
    out = provider_registry._solana_token_security(f"is {_MINT} safe")
    assert "None (supply is fixed)" in out
    assert "None (accounts cannot be frozen)" in out
    assert "No known-malicious/scam flag returned" in out


def test_dossier_degrades_to_shield_when_registry_misses(monkeypatch):
    # Registry lookup fails (unknown mint) -> still render Shield-only safety.
    monkeypatch.setattr(provider_registry, "token_identity", lambda m: None)
    monkeypatch.setattr(provider_registry, "token_safety_warnings",
                        lambda m: {"warnings": {"scam": [{"message": "flagged as scam"}]}})
    out = provider_registry._solana_token_security(f"safety of {_MINT}")
    assert "## Identity" not in out          # no registry data -> no identity block
    assert "flagged as scam" in out
    assert "Verification rule" in out


def test_dossier_appends_web_context_when_available(monkeypatch):
    monkeypatch.setattr(provider_registry, "perplexity_available", lambda: True)
    monkeypatch.setattr(provider_registry.settings, "token_security_web_context", True)
    monkeypatch.setattr(provider_registry, "token_identity", lambda m: _identity())
    monkeypatch.setattr(provider_registry, "token_safety_warnings", lambda m: {"warnings": {}})
    monkeypatch.setattr(provider_registry, "perplexity_web_search",
                        lambda q: "Issued by Circle; native Solana USDC. No depeg incidents reported.\n- [Circle](https://circle.com)")
    out = provider_registry._solana_token_security(f"safety of USDC mint {_MINT}")
    assert "## Latest context (web)" in out
    assert "Issued by Circle" in out and "circle.com" in out


def test_dossier_web_context_failure_is_safe(monkeypatch):
    monkeypatch.setattr(provider_registry, "perplexity_available", lambda: True)
    monkeypatch.setattr(provider_registry, "token_identity", lambda m: _identity())
    monkeypatch.setattr(provider_registry, "token_safety_warnings", lambda m: {"warnings": {}})
    def boom(q):
        raise RuntimeError("perplexity down")
    monkeypatch.setattr(provider_registry, "perplexity_web_search", boom)
    out = provider_registry._solana_token_security(f"safety of USDC mint {_MINT}")
    assert "## Latest context (web)" not in out   # failure -> silently omitted
    assert "## Identity" in out                    # rest of the dossier intact
