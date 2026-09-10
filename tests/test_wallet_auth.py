import asyncio

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from app import wallet_auth
from app.main import app
from app.wallet_auth import create_challenge, verify_challenge


def test_coinbase_challenge_is_one_time_and_creates_session(monkeypatch):
    account = Account.create()
    challenge = asyncio.run(create_challenge(account.address, "orbit.test", "https://orbit.test"))
    signed = Account.sign_message(encode_defunct(text=challenge["message"]), account.key)
    token, session = asyncio.run(
        verify_challenge(account.address, challenge["nonce"], signed.signature.hex())
    )
    assert token
    assert session["address"] == account.address
    assert session["provider"] == "coinbase"

    try:
        asyncio.run(verify_challenge(account.address, challenge["nonce"], signed.signature.hex()))
    except ValueError as exc:
        assert "already used" in str(exc)
    else:
        raise AssertionError("A login challenge must be single use")


def test_coinbase_login_sets_httponly_session_cookie():
    account = Account.create()
    client = TestClient(app)
    challenge = client.post("/auth/coinbase/challenge", json={"address": account.address}).json()
    signature = Account.sign_message(encode_defunct(text=challenge["message"]), account.key).signature.hex()
    verified = client.post(
        "/auth/coinbase/verify",
        json={"address": account.address, "nonce": challenge["nonce"], "signature": signature},
    )
    assert verified.status_code == 200
    assert verified.json()["authenticated"] is True
    assert "HttpOnly" in verified.headers["set-cookie"]
    assert client.get("/auth/session").json()["address"] == account.address
    assert client.post("/auth/logout").json() == {"authenticated": False}


def test_coinbase_smart_wallet_signature_uses_active_chain(monkeypatch):
    account = Account.create()
    calls = []

    async def verify_smart(address, message, signature, chain_id):
        calls.append((address, message, signature, chain_id))
        return True

    monkeypatch.setattr(wallet_auth, "_verify_universal_signature", verify_smart)
    challenge = asyncio.run(
        create_challenge(account.address, "orbit.test", "https://orbit.test", 8453)
    )
    # Representative variable-length ERC-6492 payload, not an EOA signature.
    signature = "0x" + ("12" * 160) + ("6492" * 16)
    token, session = asyncio.run(
        verify_challenge(account.address, challenge["nonce"], signature)
    )

    assert token
    assert session["provider"] == "coinbase"
    assert calls[0][0] == account.address
    assert calls[0][2] == signature
    assert calls[0][3] == 8453
