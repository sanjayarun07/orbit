"""Wallet-only sign-in: no email required at all. From the user's own words:
"user should be able to chat without wallet or email sign-in with limited
credits... After that user can sign in just with wallet... no email
mandatory... either email wallet or metamask or anything via wallet picker."

Covers both signature families (EVM via EIP-191/1271/6492, Solana via
Ed25519) through the one general /auth/wallet/challenge + /auth/wallet/verify
pair, and the account-resolution policy: a wallet already linked to an
account always signs the caller in as that account (never reassigned); an
unlinked wallet links to the caller's current session if signed in, or
creates a fresh, email-less account otherwise.
"""
import asyncio

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import base58
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from app import accounts, wallet_auth
from app.main import app
from app.wallet_auth import create_challenge, create_solana_challenge, verify_challenge, verify_solana_challenge
from tests.conftest import sign_in


def _solana_keypair():
    priv = Ed25519PrivateKey.generate()
    pub_bytes = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return priv, base58.b58encode(pub_bytes).decode()


def _evm_challenge_and_sign(client, path_prefix="/auth/wallet"):
    account = Account.create()
    body = {"address": account.address, "chain": "1"} if path_prefix == "/auth/wallet" else {"address": account.address, "chain_id": 1}
    challenge = client.post(f"{path_prefix}/challenge", json=body).json()
    signature = Account.sign_message(encode_defunct(text=challenge["message"]), account.key).signature.hex()
    return account, challenge, signature


def _solana_challenge_and_sign(client):
    priv, address = _solana_keypair()
    challenge = client.post("/auth/wallet/challenge", json={"address": address, "chain": "solana"}).json()
    signature = priv.sign(challenge["message"].encode("utf-8")).hex()
    return address, challenge, signature


# --- low-level challenge/verify (unit) ---------------------------------------

def test_evm_challenge_is_one_time():
    account = Account.create()
    challenge = asyncio.run(create_challenge(account.address, "orbit.test", "https://orbit.test"))
    signed = Account.sign_message(encode_defunct(text=challenge["message"]), account.key)
    asyncio.run(verify_challenge(account.address, challenge["nonce"], signed.signature.hex()))
    try:
        asyncio.run(verify_challenge(account.address, challenge["nonce"], signed.signature.hex()))
    except ValueError as exc:
        assert "already used" in str(exc)
    else:
        raise AssertionError("A login challenge must be single use")


def test_solana_challenge_verifies_a_real_ed25519_signature_and_is_one_time():
    priv, address = _solana_keypair()
    challenge = asyncio.run(create_solana_challenge(address, "orbit.test", "https://orbit.test"))
    signature = priv.sign(challenge["message"].encode("utf-8")).hex()
    asyncio.run(verify_solana_challenge(address, challenge["nonce"], signature))  # no exception
    try:
        asyncio.run(verify_solana_challenge(address, challenge["nonce"], signature))
    except ValueError as exc:
        assert "already used" in str(exc)
    else:
        raise AssertionError("A login challenge must be single use")


def test_solana_verify_rejects_a_bad_signature_and_the_wrong_address():
    priv, address = _solana_keypair()
    _, other_address = _solana_keypair()
    challenge = asyncio.run(create_solana_challenge(address, "orbit.test", "https://orbit.test"))
    try:
        asyncio.run(verify_solana_challenge(address, challenge["nonce"], "00" * 64))
        raise AssertionError("a garbage signature must not verify")
    except ValueError:
        pass
    challenge2 = asyncio.run(create_solana_challenge(address, "orbit.test", "https://orbit.test"))
    real_signature = priv.sign(challenge2["message"].encode("utf-8")).hex()
    try:
        asyncio.run(verify_solana_challenge(other_address, challenge2["nonce"], real_signature))
        raise AssertionError("a real signature for the wrong address must not verify")
    except ValueError:
        pass


def test_evm_smart_wallet_signature_uses_active_chain(monkeypatch):
    account = Account.create()
    calls = []

    async def verify_smart(address, message, signature, chain_id):
        calls.append((address, message, signature, chain_id))
        return True

    monkeypatch.setattr(wallet_auth, "_verify_universal_signature", verify_smart)
    challenge = asyncio.run(create_challenge(account.address, "orbit.test", "https://orbit.test", 8453))
    signature = "0x" + ("12" * 160) + ("6492" * 16)   # representative ERC-6492 payload, not an EOA signature
    asyncio.run(verify_challenge(account.address, challenge["nonce"], signature))
    assert calls[0][0] == account.address and calls[0][3] == 8453


# --- route-level: wallet-only sign-in, no email at all -----------------------

def test_a_new_solana_wallet_signs_in_with_no_email_at_all():
    client = TestClient(app)
    address, challenge, signature = _solana_challenge_and_sign(client)
    verified = client.post("/auth/wallet/verify", json={"address": address, "chain": "solana", "nonce": challenge["nonce"], "signature": signature})
    assert verified.status_code == 200
    body = verified.json()
    assert body["authenticated"] is True and body["created"] is True
    assert body["user"]["email"] is None
    # wallet_type is part of the payload now: EOA identities span every EVM
    # network, contract-wallet identities do not, so the link records which.
    assert body["wallets"] == [{"chain": "solana", "address": address, "wallet_type": "eoa",
                                "linked_at": body["wallets"][0]["linked_at"]}]
    assert "HttpOnly" in verified.headers["set-cookie"]
    assert client.get("/me").json()["user"]["id"] == body["user"]["id"]


def test_a_new_evm_wallet_signs_in_with_no_email_at_all():
    client = TestClient(app)
    account, challenge, signature = _evm_challenge_and_sign(client)
    verified = client.post("/auth/wallet/verify", json={"address": account.address, "chain": "1", "nonce": challenge["nonce"], "signature": signature})
    assert verified.status_code == 200
    body = verified.json()
    assert body["authenticated"] is True and body["created"] is True and body["user"]["email"] is None
    assert body["wallets"][0]["chain"] == "ethereum" and body["wallets"][0]["address"] == account.address.lower()


def test_signing_in_again_with_the_same_wallet_resumes_the_same_account_not_a_new_one():
    client = TestClient(app)
    priv, address = _solana_keypair()

    def _verify_once(target_client):
        challenge = target_client.post("/auth/wallet/challenge", json={"address": address, "chain": "solana"}).json()
        signature = priv.sign(challenge["message"].encode("utf-8")).hex()
        return target_client.post("/auth/wallet/verify", json={"address": address, "chain": "solana", "nonce": challenge["nonce"], "signature": signature}).json()

    first = _verify_once(client)
    assert first["created"] is True
    client.post("/auth/logout")
    second = _verify_once(client)
    assert second["created"] is False and second["user"]["id"] == first["user"]["id"]


def test_an_already_linked_wallet_always_resolves_to_its_existing_owner_even_from_a_different_signed_in_session():
    client = TestClient(app)
    owner_me = sign_in(client, email="owner@example.com")
    owner_id = owner_me["user"]["id"]
    priv, address = _solana_keypair()

    def _verify_once(target_client):
        challenge = target_client.post("/auth/wallet/challenge", json={"address": address, "chain": "solana"}).json()
        signature = priv.sign(challenge["message"].encode("utf-8")).hex()
        return target_client.post("/auth/wallet/verify", json={"address": address, "chain": "solana", "nonce": challenge["nonce"], "signature": signature}).json()

    linked = _verify_once(client)   # linked while signed in as the owner
    assert linked["user"]["id"] == owner_id and linked["created"] is False

    other = TestClient(app)
    sign_in(other, email="someone-else@example.com")
    # `other` presents the SAME real signature (the only one this wallet can ever
    # produce) while a DIFFERENT account's cookie is already on the request --
    # the wallet's existing owner must win; `other`'s session must switch to the
    # owner's account, never reassign the wallet to `other`.
    resolved = _verify_once(other)
    assert resolved["user"]["id"] == owner_id and resolved["created"] is False
    assert asyncio.run(accounts.get_user_by_wallet("solana", address))["id"] == owner_id


def test_linking_a_new_wallet_while_signed_in_attaches_it_to_the_current_account():
    client = TestClient(app)
    me = sign_in(client, email="link-me@example.com")
    address, challenge, signature = _solana_challenge_and_sign(client)
    verified = client.post("/auth/wallet/verify", json={"address": address, "chain": "solana", "nonce": challenge["nonce"], "signature": signature}).json()
    assert verified["user"]["id"] == me["user"]["id"] and verified["created"] is False
    assert verified["wallets"][0]["chain"] == "solana" and verified["wallets"][0]["address"] == address


def test_evm_chain_id_resolves_to_a_readable_chain_name():
    client = TestClient(app)
    account, challenge, signature = _evm_challenge_and_sign(client)
    # request the challenge on Base (8453) this time
    base_challenge = client.post("/auth/wallet/challenge", json={"address": account.address, "chain": "8453"}).json()
    base_signature = Account.sign_message(encode_defunct(text=base_challenge["message"]), account.key).signature.hex()
    verified = client.post("/auth/wallet/verify", json={"address": account.address, "chain": "8453", "nonce": base_challenge["nonce"], "signature": base_signature}).json()
    assert verified["wallets"][0]["chain"] == "base"


def test_delete_account_accepts_a_linked_wallet_address_when_there_is_no_email():
    client = TestClient(app)
    address, challenge, signature = _solana_challenge_and_sign(client)
    client.post("/auth/wallet/verify", json={"address": address, "chain": "solana", "nonce": challenge["nonce"], "signature": signature})
    assert client.request("DELETE", "/me", json={"confirm_email": "not-the-wallet"}).status_code == 400
    deleted = client.request("DELETE", "/me", json={"confirm_email": address})
    assert deleted.status_code == 200 and deleted.json() == {"deleted": True}


# --- the Coinbase Wallet SDK bundle calls these two paths directly, by name,
# right after every connect (app/static/coinbase.js's kc()) -- verified via
# static read of the bundle: POST /auth/coinbase/challenge {address, chain_id},
# then POST /auth/coinbase/verify {address, nonce, signature}. Keeping this
# path and body shape exactly as it already expects means Coinbase sign-in
# works with no frontend change at all, once the backend behavior is real.

def test_coinbase_bundle_shape_signs_in_with_no_prior_session():
    client = TestClient(app)
    account, challenge, signature = _evm_challenge_and_sign(client, "/auth/coinbase")
    verified = client.post("/auth/coinbase/verify", json={"address": account.address, "nonce": challenge["nonce"], "signature": signature})
    assert verified.status_code == 200
    body = verified.json()
    assert body["authenticated"] is True and body["created"] is True and body["user"]["email"] is None
    assert "HttpOnly" in verified.headers["set-cookie"]
    assert client.get("/me").json()["wallets"][0]["address"] == account.address.lower()
    assert client.get("/me").json()["wallets"][0]["chain"] == "evm"   # this path doesn't carry a chain_id back out of verify_challenge


def test_auth_logout_is_a_real_full_signout_not_just_the_dead_wallet_cookie():
    """The Coinbase bundle calls /auth/logout (not /auth/signout) on disconnect
    or account switch -- it must actually sign the user out."""
    client = TestClient(app)
    account, challenge, signature = _evm_challenge_and_sign(client, "/auth/coinbase")
    client.post("/auth/coinbase/verify", json={"address": account.address, "nonce": challenge["nonce"], "signature": signature})
    assert client.get("/me").json()["authenticated"] is True
    assert client.post("/auth/logout").json() == {"authenticated": False}
    assert client.get("/me").json()["authenticated"] is False
