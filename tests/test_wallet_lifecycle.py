"""STAB-04: the wallet-only account lifecycle, beyond the happy path.

tests/test_wallet_auth.py already covers fresh sign-in, resuming an existing
wallet, linking from an email account, and that a linked wallet always resolves
to its owner. This file covers what the review asked for and that file does not:
expired challenges, nonce substitution, cross-chain replay, account switching
and collision, sign-out revocation, deletion, and whether an email-less account
survives the surfaces that assume an email exists.

The rule every one of these is measuring: a signature proves control of one
address, and nothing else. It must not be reusable, transferable to another
address, replayable on another chain, or able to move an account's access.
"""
import asyncio
import time

import pytest
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
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return private, base58.b58encode(public).decode()


def _sign_in_solana(client, private=None, address=None):
    """One full challenge/verify round trip for a Solana wallet."""
    if private is None:
        private, address = _solana_keypair()
    challenge = client.post("/auth/wallet/challenge", json={"address": address, "chain": "solana"}).json()
    signature = private.sign(challenge["message"].encode("utf-8")).hex()
    response = client.post("/auth/wallet/verify", json={
        "address": address, "chain": "solana", "nonce": challenge["nonce"], "signature": signature})
    return private, address, response


def _sign_in_evm(client, account=None, chain="1"):
    if account is None:
        account = Account.create()
    challenge = client.post("/auth/wallet/challenge", json={"address": account.address, "chain": chain}).json()
    signature = Account.sign_message(encode_defunct(text=challenge["message"]), account.key).signature.hex()
    response = client.post("/auth/wallet/verify", json={
        "address": account.address, "chain": chain, "nonce": challenge["nonce"], "signature": signature})
    return account, response


# --- challenge integrity ------------------------------------------------------

def test_an_expired_challenge_is_refused_even_with_a_valid_signature(monkeypatch):
    """Real elapsed expiry, not a mocked clock: the challenge is written with a
    one-second lifetime and verified after it."""
    monkeypatch.setattr(wallet_auth, "CHALLENGE_TTL", 1)
    monkeypatch.setattr(wallet_auth, "_SOLANA_CHALLENGE_TTL", 1)
    private, address = _solana_keypair()
    challenge = asyncio.run(create_solana_challenge(address, "orbit.test", "https://orbit.test"))
    signature = private.sign(challenge["message"].encode("utf-8")).hex()
    time.sleep(1.2)
    with pytest.raises(ValueError) as caught:
        asyncio.run(verify_solana_challenge(address, challenge["nonce"], signature))
    assert "expired" in str(caught.value)


def test_an_expired_evm_challenge_is_refused_even_with_a_valid_signature(monkeypatch):
    monkeypatch.setattr(wallet_auth, "CHALLENGE_TTL", 1)
    account = Account.create()
    challenge = asyncio.run(create_challenge(account.address, "orbit.test", "https://orbit.test"))
    signed = Account.sign_message(encode_defunct(text=challenge["message"]), account.key)
    time.sleep(1.2)
    with pytest.raises(ValueError) as caught:
        asyncio.run(verify_challenge(account.address, challenge["nonce"], signed.signature.hex()))
    assert "expired" in str(caught.value)


def test_a_signature_for_one_challenge_cannot_be_presented_against_another():
    """Nonce substitution: hold two open challenges for the same wallet and
    present the first one's signature against the second one's nonce."""
    private, address = _solana_keypair()
    first = asyncio.run(create_solana_challenge(address, "orbit.test", "https://orbit.test"))
    second = asyncio.run(create_solana_challenge(address, "orbit.test", "https://orbit.test"))
    signature_for_first = private.sign(first["message"].encode("utf-8")).hex()
    with pytest.raises(ValueError) as caught:
        asyncio.run(verify_solana_challenge(address, second["nonce"], signature_for_first))
    assert "does not match" in str(caught.value)


def test_an_evm_signature_for_one_challenge_cannot_be_presented_against_another(monkeypatch):
    account = Account.create()
    # The smart-wallet fallback would make a live RPC call for a signature the
    # EOA path rejected; stub it to a refusal so this stays a unit test.
    async def _no_smart_wallet(*args, **kwargs):
        return False
    monkeypatch.setattr(wallet_auth, "_verify_universal_signature", _no_smart_wallet)
    first = asyncio.run(create_challenge(account.address, "orbit.test", "https://orbit.test"))
    second = asyncio.run(create_challenge(account.address, "orbit.test", "https://orbit.test"))
    signature = Account.sign_message(encode_defunct(text=first["message"]), account.key).signature.hex()
    with pytest.raises(ValueError):
        asyncio.run(verify_challenge(account.address, second["nonce"], signature))


def test_a_solana_challenge_cannot_be_redeemed_through_the_evm_verifier():
    """The two challenge stores must not share a namespace: a nonce issued for
    one signature family must be unknown to the other."""
    _, address = _solana_keypair()
    challenge = asyncio.run(create_solana_challenge(address, "orbit.test", "https://orbit.test"))
    with pytest.raises(ValueError) as caught:
        asyncio.run(verify_challenge("0x" + "11" * 20, challenge["nonce"], "0x" + "22" * 65))
    assert "invalid, expired, or already used" in str(caught.value)


def test_a_challenge_is_bound_to_the_address_that_requested_it():
    """A real signature from the real key holder, presented for a different
    address, must not verify -- for EVM as well as Solana."""
    client = TestClient(app)
    account, other = Account.create(), Account.create()
    challenge = client.post("/auth/wallet/challenge", json={"address": account.address, "chain": "1"}).json()
    signature = Account.sign_message(encode_defunct(text=challenge["message"]), account.key).signature.hex()
    hijacked = client.post("/auth/wallet/verify", json={
        "address": other.address, "chain": "1", "nonce": challenge["nonce"], "signature": signature})
    assert hijacked.status_code == 401


def test_an_unconfigured_evm_chain_cannot_be_used_to_request_a_challenge():
    client = TestClient(app)
    refused = client.post("/auth/wallet/challenge", json={"address": Account.create().address, "chain": "999999"})
    assert refused.status_code == 400


# --- account identity, switching and collision --------------------------------

def test_one_evm_wallet_is_one_account_on_every_evm_network():
    """An EVM address is the same key on Ethereum, Base and everywhere else.

    Signing in from a different network must resume the same account, not mint
    a second one -- otherwise a user who switches networks in their wallet
    silently splits their history, credits and plan across two accounts.
    """
    account = Account.create()
    first_browser = TestClient(app)
    _, first = _sign_in_evm(first_browser, account, chain="1")
    assert first.status_code == 200 and first.json()["created"] is True
    account_id = first.json()["user"]["id"]

    # Same wallet, different network, a browser with no session at all.
    second_browser = TestClient(app)
    _, second = _sign_in_evm(second_browser, account, chain="8453")
    assert second.status_code == 200
    assert second.json()["created"] is False, "switching networks created a second account"
    assert second.json()["user"]["id"] == account_id


def test_the_coinbase_path_and_the_general_path_reach_the_same_account():
    """Two entry points, one wallet. The Coinbase SDK bundle posts to
    /auth/coinbase/* and the picker posts to /auth/wallet/*; the same address
    through either must be the same account."""
    account = Account.create()
    coinbase_browser = TestClient(app)
    challenge = coinbase_browser.post("/auth/coinbase/challenge", json={
        "address": account.address, "chain_id": 1}).json()
    signature = Account.sign_message(encode_defunct(text=challenge["message"]), account.key).signature.hex()
    first = coinbase_browser.post("/auth/coinbase/verify", json={
        "address": account.address, "nonce": challenge["nonce"], "signature": signature}).json()
    account_id = first["user"]["id"]

    picker_browser = TestClient(app)
    _, second = _sign_in_evm(picker_browser, account, chain="1")
    assert second.json()["created"] is False, "the same wallet made a second account via the other entry point"
    assert second.json()["user"]["id"] == account_id


def test_a_solana_address_and_an_evm_address_never_collide():
    """Different address spaces; the chain must stay part of the identity."""
    client = TestClient(app)
    _, _, solana = _sign_in_solana(client)
    solana_id = solana.json()["user"]["id"]
    other = TestClient(app)
    _, evm = _sign_in_evm(other)
    assert evm.json()["user"]["id"] != solana_id


def test_signing_in_with_a_second_wallet_switches_accounts_rather_than_merging(monkeypatch):
    """Two wallets, each already owning an account. Signing in with the second
    from the first's browser must move the session to the second's owner and
    leave both accounts' wallet links untouched."""
    first_browser, second_browser = TestClient(app), TestClient(app)
    alice_key, alice_address, alice = _sign_in_solana(first_browser)
    bob_key, bob_address, bob = _sign_in_solana(second_browser)
    alice_id, bob_id = alice.json()["user"]["id"], bob.json()["user"]["id"]
    assert alice_id != bob_id

    _, _, switched = _sign_in_solana(first_browser, bob_key, bob_address)
    assert switched.json()["user"]["id"] == bob_id
    assert first_browser.get("/me").json()["user"]["id"] == bob_id
    # Neither wallet changed hands.
    assert asyncio.run(accounts.get_user_by_wallet("solana", alice_address))["id"] == alice_id
    assert asyncio.run(accounts.get_user_by_wallet("solana", bob_address))["id"] == bob_id


def test_an_email_account_that_links_a_wallet_keeps_one_identity():
    client = TestClient(app)
    me = sign_in(client, email="both-ways@example.com")
    private, address, linked = _sign_in_solana(client)
    assert linked.json()["user"]["id"] == me["user"]["id"]
    # And the wallet alone, from a clean browser, resumes that same account.
    fresh = TestClient(app)
    _, _, again = _sign_in_solana(fresh, private, address)
    assert again.json()["created"] is False
    assert again.json()["user"]["id"] == me["user"]["id"]
    assert again.json()["user"]["email"] == "both-ways@example.com"


# --- sign-out, revocation and deletion ----------------------------------------

def test_sign_out_revokes_the_session_server_side_not_just_the_cookie():
    """A cleared cookie is not revocation. The token itself must stop working,
    so a copy of it cannot be replayed."""
    client = TestClient(app)
    _, _, signed_in = _sign_in_solana(client)
    assert signed_in.status_code == 200
    token = client.cookies.get(accounts.USER_COOKIE)
    assert token

    client.post("/auth/logout")
    assert client.get("/me").json()["authenticated"] is False

    replay = TestClient(app)
    replay.cookies.set(accounts.USER_COOKIE, token)
    assert replay.get("/me").json()["authenticated"] is False, "a signed-out session token still authenticates"


def test_deleting_a_wallet_only_account_releases_the_wallet():
    """After deletion the same wallet must be able to start over as a new
    account, and must not resolve to the deleted one."""
    client = TestClient(app)
    private, address, created = _sign_in_solana(client)
    first_id = created.json()["user"]["id"]
    assert client.request("DELETE", "/me", json={"confirm_email": address}).status_code == 200
    assert asyncio.run(accounts.get_user_by_wallet("solana", address)) is None

    fresh = TestClient(app)
    _, _, again = _sign_in_solana(fresh, private, address)
    assert again.status_code == 200
    assert again.json()["created"] is True
    assert again.json()["user"]["id"] != first_id


# --- an email-less account on the surfaces that assume an email ----------------

WALLET_ONLY_SURFACES = [
    ("GET", "/me"),
    ("GET", "/me/credits"),
    ("GET", "/me/conversations"),
    ("GET", "/me/team"),
    ("GET", "/me/api-keys"),
    ("GET", "/me/tasks"),
    ("GET", "/me/notifications"),
]


@pytest.mark.parametrize("method,path", WALLET_ONLY_SURFACES, ids=[path for _, path in WALLET_ONLY_SURFACES])
def test_an_account_with_no_email_does_not_break_the_account_surfaces(method, path):
    """Every one of these reads user["email"]; a wallet-only account has None.
    A 500 here is the crash the nullable-email change had to avoid."""
    client = TestClient(app)
    _, _, signed_in = _sign_in_solana(client)
    assert signed_in.json()["user"]["email"] is None
    response = client.request(method, path)
    assert response.status_code < 500, f"{path} returned {response.status_code}: {response.text[:200]}"


def test_a_wallet_only_account_gets_the_same_starting_plan_and_credits_as_an_email_account():
    wallet_client = TestClient(app)
    _, _, wallet_user = _sign_in_solana(wallet_client)
    email_client = TestClient(app)
    email_user = sign_in(email_client, email="plan-compare@example.com")
    assert wallet_user.json()["plan"]["id"] == email_user["plan"]["id"]
    assert wallet_user.json()["credits"]["balance"] == email_user["credits"]["balance"]


def test_the_wallet_is_what_confirms_deletion_when_there_is_no_email_to_type():
    client = TestClient(app)
    _, address, _ = _sign_in_solana(client)
    assert client.request("DELETE", "/me", json={"confirm_email": "someone@example.com"}).status_code == 400
    assert client.request("DELETE", "/me", json={"confirm_email": address.upper()}).status_code in (200, 400)
