"""Signing in on a phone (2026-09-18).

The installed app cannot use the emailed link: on iOS the link opens Safari,
whose cookies the home-screen app does not share, so the app stayed signed
out. The same email now carries a six-digit code that the app's sign-in
sheet accepts. And the Privy email wallet connected but never signed the
account in: the embedded Solana provider wants a base64 message and answers
with a base64 signature, and the page had been passing raw bytes, failing
silently. Both are fixed here.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import accounts, emailer, main


@pytest.fixture(autouse=True)
def _memory_stores(monkeypatch):
    async def no_redis():
        return None

    monkeypatch.setattr(accounts, "get_redis", no_redis)   # the in-memory stores, so the issued code can be read back
    accounts._magic_tokens.clear()
    accounts._magic_codes.clear()
    yield
    accounts._magic_tokens.clear()
    accounts._magic_codes.clear()


def _issued_code(email: str) -> str:
    return accounts._magic_codes[accounts.normalize_email(email)][1]["code"]


def test_the_email_carries_a_six_digit_code_behind_the_same_token(monkeypatch):
    captured = {}

    async def send(to, subject, html, text=None):
        captured.update(to=to, html=html, text=text)
        return True

    monkeypatch.setattr(emailer, "send_email", send)
    out = asyncio.run(accounts.start_email_signin("Code@Example.com", "https://orbit.example"))
    assert out["sent"] is True
    code = _issued_code("code@example.com")
    assert len(code) == 6 and code.isdigit()
    assert code in captured["html"] and code in captured["text"] and "signin=" in captured["text"]
    token = asyncio.run(accounts.consume_magic_code("code@example.com", code))
    assert token in accounts._magic_tokens, "the code resolves to the link's own one-time token"
    with pytest.raises(ValueError, match="expired or was already used"):
        asyncio.run(accounts.consume_magic_code("code@example.com", code))


def test_five_wrong_codes_burn_it(monkeypatch):
    monkeypatch.setattr(emailer, "send_email", lambda *a, **k: asyncio.sleep(0, result=True))
    asyncio.run(accounts.start_email_signin("guess@example.com", "https://orbit.example"))
    right = _issued_code("guess@example.com")
    wrong = "000000" if right != "000000" else "111111"
    for _ in range(4):
        with pytest.raises(ValueError, match="not right"):
            asyncio.run(accounts.consume_magic_code("guess@example.com", wrong))
    with pytest.raises(ValueError, match="Too many"):
        asyncio.run(accounts.consume_magic_code("guess@example.com", wrong))
    with pytest.raises(ValueError, match="expired or was already used"):
        asyncio.run(accounts.consume_magic_code("guess@example.com", right)), "the right code is dead after the fifth wrong guess"


def test_the_code_route_signs_the_browser_in_and_the_link_token_is_spent(monkeypatch):
    monkeypatch.setattr(emailer, "send_email", lambda *a, **k: asyncio.sleep(0, result=True))
    client = TestClient(main.app)
    assert client.post("/auth/email/start", json={"email": "phone@example.com"}).json()["sent"] is True
    code = _issued_code("phone@example.com")
    assert client.post("/auth/email/code", json={"email": "phone@example.com", "code": "999999" if code != "999999" else "000000"}).status_code == 401
    r = client.post("/auth/email/code", json={"email": "phone@example.com", "code": code})
    assert r.status_code == 200 and r.json()["authenticated"] is True and r.json()["user"]["email"] == "phone@example.com"
    assert client.get("/me").json()["authenticated"] is True, "the session cookie is set on the app's own origin"
    assert not accounts._magic_tokens, "the link cannot be used a second time after the code was"
    assert client.post("/auth/email/code", json={"email": "phone@example.com", "code": "12"}).status_code == 422


def test_the_sheet_offers_the_code_after_sending_and_accepts_it():
    from tests.test_ui_swap_flow import run_case
    r = run_case("signin_sheet_offers_and_accepts_the_emailed_code")
    assert r["afterSend"] == {"codeRowHidden": False, "codeBtnHidden": False, "sendLabel": "Resend"}, r["afterSend"]
    assert "six-digit" in r["shortCode"]
    assert r["posts"] == [{"email": "a@b.co", "code": "123456"}]


def test_privy_signs_the_challenge_as_base64_and_hands_the_server_hex():
    from tests.test_ui_swap_flow import run_case
    r = run_case("privy_sign_message_uses_base64_in_and_out")
    assert r["method"] == "signMessage" and r["message"] == "aGk="        # base64("hi")
    assert r["hex"] == "0102ff"
