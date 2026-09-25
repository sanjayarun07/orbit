"""The Telegram transport: who it runs as, what it renders, what it refuses.

The bot is an adapter over `execute_chat_turn`, so these tests do not re-prove
the agent. They prove the four things the adapter alone decides -- identity,
conversation, authorization of a button, and rendering -- plus the two ways a
webhook can be abused.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import accounts, execution_policy, sessions
from app.main import app
from app.settings import settings
from app.telegram import bot, client as tg_client, identity as tg_identity, render
from app.telegram import webhook as tg_webhook
from tests.conftest import sign_in

TOKEN = "123456:test-bot-token"
SOL = "5CEbueQnq1Ym2uSSx2xXds3jQAqT1BDnkA59RZobSPAG"


@pytest.fixture(autouse=True)
def _telegram_configured(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", TOKEN)
    monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cret-for-tests")
    monkeypatch.setattr(settings, "public_base_url", "https://orbit.example.com")
    bot._seen_updates.clear()
    bot._BOT_USERNAME = "orbit_test_bot"
    tg_identity._link_tokens.clear()
    yield
    bot._BOT_USERNAME = None


@pytest.fixture
def sent(monkeypatch):
    """Capture every outbound Bot API call instead of making it."""
    calls = []

    async def fake_call(method, **params):
        calls.append((method, params))
        if method == "sendMessage":
            return {"message_id": 100 + len(calls), "chat": {"id": params.get("chat_id")}}
        if method == "getMe":
            return {"username": "orbit_test_bot"}
        return {"ok": True}

    monkeypatch.setattr(tg_client, "call", fake_call)
    return calls


def _message(text, chat_id=555, user_id=999, chat_type="private", **extra):
    return {
        "update_id": extra.pop("update_id", 1),
        "message": {
            "message_id": 7, "text": text,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": user_id, "username": "trader", "first_name": "T"},
            **extra,
        },
    }


def _turns(monkeypatch, answer="ok", **response_fields):
    """Replace the agent turn with a recorder; returns the recorded calls."""
    from app.models import AgentResponse

    seen = []

    async def fake_turn(body, identity):
        seen.append((body, identity))
        return AgentResponse(answer=answer, session_id=body.session_id or "s", **response_fields)

    monkeypatch.setattr(execution_policy, "execute_chat_turn", fake_turn)
    return seen


# ----------------------------------------------------------------- the webhook

def test_webhook_requires_both_the_path_secret_and_the_header():
    http = TestClient(app)
    secret = tg_client.webhook_secret()
    update = {"update_id": 1}
    assert http.post(f"/telegram/webhook/{secret}", json=update,
                     headers={"X-Telegram-Bot-Api-Secret-Token": secret}).status_code == 200
    # Right path, no header: a request that learned the URL is still refused.
    assert http.post(f"/telegram/webhook/{secret}", json=update).status_code == 403
    assert http.post(f"/telegram/webhook/{secret}", json=update,
                     headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 403
    # Wrong path is a 404, so the secret cannot be probed for existence.
    assert http.post("/telegram/webhook/guessed", json=update,
                     headers={"X-Telegram-Bot-Api-Secret-Token": secret}).status_code == 404


def test_webhook_secret_is_derived_when_unset_and_never_the_bare_token(monkeypatch):
    monkeypatch.setattr(settings, "telegram_webhook_secret", None)
    derived = tg_client.webhook_secret()
    assert derived and TOKEN not in derived and len(derived) == 32


def test_a_redelivered_update_is_not_run_twice(monkeypatch, sent):
    seen = _turns(monkeypatch)
    update = _message("what is BONK", update_id=42)
    asyncio.run(bot.handle_update(update))
    asyncio.run(bot.handle_update(update))
    assert len(seen) == 1


# ---------------------------------------------------------------- the identity

def test_first_message_creates_one_account_and_the_next_resolves_it(monkeypatch, sent):
    seen = _turns(monkeypatch)
    asyncio.run(bot.handle_update(_message("hello", update_id=1)))
    asyncio.run(bot.handle_update(_message("and again", update_id=2)))

    identities = [identity for _, identity in seen]
    assert identities[0].kind == "user"
    assert identities[0].user["id"] == identities[1].user["id"]
    assert identities[0].user["email"] is None            # no sign-in screen, still a real account
    owner = asyncio.run(accounts.find_identity_owner("telegram", "999"))
    assert owner["id"] == identities[0].user["id"]


def test_the_conversation_is_derived_from_the_chat_and_survives_a_restart():
    assert tg_identity.session_id(555) == "tg:555"
    assert tg_identity.session_id(555, 12) == "tg:555:12"       # a forum topic is its own conversation
    assert tg_identity.session_id(555) == tg_identity.session_id(555)


def test_linking_moves_the_pointer_and_discards_the_shell(monkeypatch, sent):
    _turns(monkeypatch)
    tg_user = {"id": 999, "username": "trader"}
    shell, _ = asyncio.run(tg_identity.account_for(tg_user))

    web = asyncio.run(accounts.get_or_create_user("web@example.com"))[0]
    token = asyncio.run(tg_identity.start_link(web["id"]))
    asyncio.run(bot.handle_update(_message(f"/start {token}")))

    assert asyncio.run(accounts.find_identity_owner("telegram", "999"))["id"] == web["id"]
    assert asyncio.run(accounts.get_user(shell["id"])) is None   # the throwaway account is gone
    assert asyncio.run(tg_identity.consume_link_token(token)) is None   # single use


def test_a_link_token_is_single_use_and_a_bad_one_is_refused(monkeypatch, sent):
    _turns(monkeypatch)
    asyncio.run(bot.handle_update(_message("/start not-a-real-token")))
    text = " ".join(str(params.get("text", "")) for method, params in sent if method == "sendMessage")
    assert "expired" in text.lower()
    assert asyncio.run(accounts.find_identity_owner("telegram", "999")) is None


def test_linking_keeps_an_account_that_is_more_than_a_shell(monkeypatch, sent):
    """A prior account with a wallet is the user's; linking must not delete it."""
    _turns(monkeypatch)
    tg_user = {"id": 999, "username": "trader"}
    prior, _ = asyncio.run(tg_identity.account_for(tg_user))
    asyncio.run(accounts.link_wallet(prior["id"], "solana", SOL))

    web = asyncio.run(accounts.get_or_create_user("web@example.com"))[0]
    asyncio.run(tg_identity.link_to_account(tg_user, web["id"]))

    assert asyncio.run(accounts.find_identity_owner("telegram", "999"))["id"] == web["id"]
    assert asyncio.run(accounts.get_user(prior["id"])) is not None


def test_link_endpoint_needs_a_browser_session(sent):
    http = TestClient(app)
    assert http.post("/me/telegram/link").status_code in (401, 403)
    sign_in(http)
    body = http.post("/me/telegram/link").json()
    assert body["url"].startswith("https://t.me/orbit_test_bot?start=")


def test_unlink_checks_ownership_not_knowledge_of_the_id():
    http = TestClient(app)
    sign_in(http, email="owner@example.com")
    other = asyncio.run(accounts.get_or_create_user("other@example.com"))[0]
    asyncio.run(accounts.link_identity(other["id"], "telegram", "4242"))
    # A Telegram user id is public; knowing it must not unlink someone else's.
    assert http.delete("/me/telegram/4242").status_code == 404
    assert asyncio.run(accounts.find_identity_owner("telegram", "4242"))["id"] == other["id"]


# ------------------------------------------------------------------- the group

def test_a_group_message_is_ignored_unless_it_is_addressed_to_us(monkeypatch, sent):
    seen = _turns(monkeypatch)
    asyncio.run(bot.handle_update(_message("gm everyone", chat_type="supergroup", update_id=1)))
    assert seen == []

    asyncio.run(bot.handle_update(_message("@orbit_test_bot what is BONK", chat_type="supergroup", update_id=2)))
    assert len(seen) == 1 and seen[0][0].message == "what is BONK"

    reply = _message("and its holders?", chat_type="supergroup", update_id=3)
    reply["message"]["reply_to_message"] = {"from": {"username": "orbit_test_bot"}}
    asyncio.run(bot.handle_update(reply))
    assert len(seen) == 2


def test_a_command_in_a_group_is_always_ours(monkeypatch, sent):
    _turns(monkeypatch)
    asyncio.run(bot.handle_update(_message("/help@orbit_test_bot", chat_type="supergroup")))
    assert any("Commands" in str(params.get("text", "")) for method, params in sent if method == "sendMessage")


# ------------------------------------------------------------------ the wallet

def test_wallet_command_binds_a_public_address_to_the_conversation(monkeypatch, sent):
    _turns(monkeypatch)
    asyncio.run(bot.handle_update(_message(f"/wallet {SOL}")))
    context = asyncio.run(sessions.get_session_context("tg:555"))
    assert context["mcp_wallet_address"] == SOL


def test_wallet_command_refuses_something_shaped_like_a_seed_phrase(monkeypatch, sent):
    _turns(monkeypatch)
    asyncio.run(bot.handle_update(_message("/wallet " + "abandon " * 12, chat_id=556)))
    text = " ".join(str(params.get("text", "")) for method, params in sent if method == "sendMessage")
    assert "key material" in text and "compromised" in text
    context = asyncio.run(sessions.get_session_context("tg:556"))
    assert "mcp_wallet_address" not in context


# ------------------------------------------------------------------- callbacks

def test_a_button_names_a_position_and_the_action_comes_from_the_server(monkeypatch, sent):
    """The callback payload is client-supplied; the quick action is not."""
    from app.models import QuickAction

    stored = QuickAction(id="holders", prompt="top holders of BONK", intent="research",
                         capabilities=["token_discovery"], context_revision=3).model_dump()

    async def fake_messages(session_id):
        return [{"role": "assistant", "content": "…", "quick_actions": [stored], "suggestions": ["what moved it?"]}]

    monkeypatch.setattr(sessions, "get_messages", fake_messages)
    monkeypatch.setattr(bot.sessions, "get_messages", fake_messages)
    seen = _turns(monkeypatch)

    asyncio.run(bot.handle_update({
        "update_id": 5,
        "callback_query": {"id": "cb1", "data": f"qa:{render.answer_key('…')}:0", "from": {"id": 999},
                           "message": {"message_id": 3, "chat": {"id": 555, "type": "private"}}},
    }))
    body, _ = seen[-1]
    assert body.message == "top holders of BONK"
    assert body.quick_action.model_dump() == stored     # byte for byte what the server persisted

    asyncio.run(bot.handle_update({
        "update_id": 6,
        "callback_query": {"id": "cb2", "data": f"sg:{render.answer_key('…')}:0", "from": {"id": 999},
                           "message": {"message_id": 3, "chat": {"id": 555, "type": "private"}}},
    }))
    assert seen[-1][0].message == "what moved it?"


def test_a_callback_for_an_index_the_server_never_wrote_is_refused(monkeypatch, sent):
    async def fake_messages(session_id):
        return [{"role": "assistant", "content": "…", "quick_actions": []}]

    monkeypatch.setattr(bot.sessions, "get_messages", fake_messages)
    seen = _turns(monkeypatch)
    asyncio.run(bot.handle_update({
        "update_id": 7,
        "callback_query": {"id": "cb3", "data": "qa:4", "from": {"id": 999},
                           "message": {"message_id": 3, "chat": {"id": 555, "type": "private"}}},
    }))
    assert seen == []


# ------------------------------------------------------------------- rendering

def test_markdown_becomes_telegram_html_without_losing_the_content():
    html = render.to_html(
        "## Verdict\n\n**BONK** is *liquid*. See [docs](https://x.com/a).\n\n"
        "| Metric | Value |\n|---|---|\n| Top-10 | 38.3% |\n\n- concentrated\n\n`solana_rpc` and snake_case_word\n"
    )
    assert "<b>Verdict</b>" in html and "<b>BONK</b>" in html and "<i>liquid</i>" in html
    assert '<a href="https://x.com/a">docs</a>' in html
    assert "<pre>" in html and "38.3%" in html          # the table survives as monospace
    assert "• concentrated" in html
    assert "<code>solana_rpc</code>" in html
    assert "snake_case_word" in html                     # not mangled into italics


def test_hostile_text_cannot_inject_markup():
    html = render.to_html("<script>alert(1)</script> & <b>not bold</b>")
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "&amp;" in html


def test_a_long_answer_splits_on_paragraphs_and_never_inside_a_tag():
    long_answer = "\n\n".join(f"**Section {i}** with some body text. " * 12 for i in range(40))
    chunks = render.render_answer(_response(long_answer))
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= tg_client.MAX_MESSAGE_CHARS
        assert chunk.count("<b>") == chunk.count("</b>")


def test_a_code_fence_is_never_split_in_half():
    body = "intro\n\n```\n" + "\n".join(f"line {i}" for i in range(400)) + "\n```\n\nafter"
    chunks = render.split_markdown(body, limit=800)
    fenced = [chunk for chunk in chunks if "```" in chunk]
    assert all(chunk.count("```") % 2 == 0 for chunk in fenced)


def test_the_footer_names_the_providers_and_flags_an_unverified_answer():
    from datetime import datetime, timezone
    from app.models import AnswerValidation, EvidenceSummary

    response = _response(
        "answer",
        evidence=EvidenceSummary(generated_at=datetime.now(timezone.utc), providers=["Mobula", "Birdeye"], confidence="high"),
        validation=AnswerValidation(status="warn", as_of="14:32 UTC"),
    )
    footer = render.render_footer(response)
    assert "Mobula, Birdeye" in footer and "as of 14:32 UTC" in footer and "unverified" in footer


def test_a_quote_renders_a_card_that_does_not_claim_to_sign():
    response = _response("here is your quote", trade_plan=_plan())
    card = render.render_trade_plan(response)
    assert "Swap quote" in card and "Nothing is signed yet" in card

    keyboard = render.keyboard(response, app_url="https://orbit.example.com/ui/?session=tg:1")
    buttons = [button for row in keyboard["inline_keyboard"] for button in row]
    # A plain link to the real browser, where the session and the wallet are.
    # A web_app button opens Telegram's webview: its own cookie jar (so the
    # user lands signed out) and no extension wallets at all.
    assert any(button.get("url") for button in buttons)
    assert not any("web_app" in button for button in buttons)
    # And signing is never a callback the bot itself could act on.
    assert not any("confirm" in str(button.get("callback_data", "")) for button in buttons)


# ----------------------------------------------------------------- the failure

def test_a_refused_turn_tells_the_user_why_and_charges_nothing_more(monkeypatch, sent):
    from app.service_errors import ServiceError

    async def refuse(body, identity):
        raise ServiceError(402, "You are out of credits.")

    monkeypatch.setattr(execution_policy, "execute_chat_turn", refuse)
    asyncio.run(bot.handle_update(_message("deep dive on BONK")))
    text = " ".join(str(params.get("text", "")) for method, params in sent if method in ("sendMessage", "editMessageText"))
    assert "out of credits" in text


def test_a_message_longer_than_the_contract_is_truncated_not_refused(monkeypatch, sent):
    seen = _turns(monkeypatch)
    asyncio.run(bot.handle_update(_message("x" * 4000)))
    assert len(seen) == 1 and len(seen[0][0].message) == bot.MAX_INBOUND_CHARS


# --------------------------------------------------------------------- helpers

def _response(answer, **fields):
    from app.models import AgentResponse

    return AgentResponse(answer=answer, session_id="tg:555", **fields)


def _plan():
    from datetime import datetime, timedelta, timezone
    from app.models import SwapProposal, TokenInfo, TradePlan

    mint = "So11111111111111111111111111111111111111112"
    token = TokenInfo(mint=mint, symbol="SOL", name="SOL", decimals=9, verified=True)
    now = datetime.now(timezone.utc)
    return TradePlan(
        plan_id="plan_tg", status="pending_confirmation", created_at=now, expires_at=now + timedelta(seconds=120),
        wallet_address=SOL,
        proposal=SwapProposal(input_mint=mint, output_mint=mint, amount_atomic=1, slippage_bps=50, reason="t"),
        quote={}, input_token=token, output_token=token, simulation={}, confirmation_text="CONFIRM plan_tg",
    )


# ----------------------------------------------------------- task delivery

def test_a_task_asked_for_in_telegram_delivers_to_telegram(monkeypatch, sent):
    """An inbox row is invisible to someone who never opens the web app."""
    from app import task_scheduling

    _turns(monkeypatch)
    captured = {}

    async def fake_handle(message, user, tz):
        captured["channel"] = task_scheduling.default_channel()
        return "Alert set."

    monkeypatch.setattr("app.tasks_nl.handle", fake_handle)
    monkeypatch.setattr("app.tasks_nl.is_task_control", lambda _m: True)

    async def run():
        token = task_scheduling.use_channel("telegram")
        try:
            user = (await accounts.get_or_create_user("t@example.com"))[0]
            from app.identity import Identity
            from app.billing_plans import get_plan
            identity = Identity("user", "acct", "ip", user=user, plan=get_plan("free"))
            from app.models import ChatRequest
            await task_scheduling.handle_chat_control(ChatRequest(message="alert me when SOL drops below 180"), identity, None)
        finally:
            task_scheduling.reset_channel(token)

    asyncio.run(run())
    assert captured["channel"] == "telegram"


def test_a_browser_turn_still_defaults_to_the_inbox():
    from app import task_scheduling

    assert task_scheduling.default_channel() == "inapp"
    assert task_scheduling.default_channel("email") == "email"   # what the user asked for wins


def test_a_task_created_in_a_group_does_not_deliver_to_the_group():
    """A task belongs to one account; its thresholds are not the group's business."""
    assert bot._is_private(555) is True
    assert bot._is_private(-1001234567890) is False


def test_the_worker_pushes_a_fired_task_to_the_linked_chat(monkeypatch):
    from app import notifications, tasks

    pushed = []

    async def fake_send(chat_id, text, **kwargs):
        pushed.append((chat_id, text))
        return {"message_id": 1}

    monkeypatch.setattr(tg_client, "send_message", fake_send)
    monkeypatch.setattr(tg_client, "enabled", lambda: True)

    async def run():
        user = (await accounts.get_or_create_user("push@example.com"))[0]
        await accounts.link_identity(user["id"], "telegram", "999")
        return await notifications.send_telegram(user, "SOL alert", "**SOL** crossed $180.")

    assert asyncio.run(run()) is True
    assert pushed and pushed[0][0] == "999"
    assert "<b>SOL alert</b>" in pushed[0][1] and "<b>SOL</b>" in pushed[0][1]


def test_an_account_with_no_linked_chat_is_a_recorded_failure_not_a_crash(monkeypatch):
    from app import notifications

    monkeypatch.setattr(tg_client, "enabled", lambda: True)

    async def run():
        user = (await accounts.get_or_create_user("nolink@example.com"))[0]
        return await notifications.send_telegram(user, "t", "b")

    assert asyncio.run(run()) is False


def test_telegram_is_an_accepted_task_channel():
    from app import tasks

    assert "telegram" in tasks.CHANNELS

    async def run():
        user = (await accounts.get_or_create_user("chan@example.com"))[0]
        task = await tasks.create_task(user, "reminder", {"message": "check SOL"}, {"daily": "09:00"}, channel="telegram")
        return task

    assert asyncio.run(run())["channel"] == "telegram"


# ------------------------------------------------------ isolation between users

def test_two_people_in_one_group_each_get_their_own_conversation(monkeypatch, sent):
    """A conversation is owned by one account. Sharing one across a group
    locked out everyone but the first to ask -- and would have handed them
    that person's bound wallet, focus entity and charter."""
    seen = _turns(monkeypatch)
    asyncio.run(bot.handle_update(_message("@orbit_test_bot what is BONK", chat_id=-100123, user_id=111,
                                           chat_type="supergroup", update_id=1)))
    asyncio.run(bot.handle_update(_message("@orbit_test_bot what is WIF", chat_id=-100123, user_id=222,
                                           chat_type="supergroup", update_id=2)))

    assert len(seen) == 2, "the second person in a group was refused"
    assert seen[0][0].session_id == "tg:-100123:u111"
    assert seen[1][0].session_id == "tg:-100123:u222"
    assert seen[0][1].user["id"] != seen[1][1].user["id"]          # separate accounts
    denied = [p.get("text") for m, p in sent if m == "sendMessage" and "belongs to another" in str(p.get("text"))]
    assert denied == []


def test_two_people_in_private_chats_never_share_anything(monkeypatch, sent):
    seen = _turns(monkeypatch)
    asyncio.run(bot.handle_update(_message("what do I hold", chat_id=111, user_id=111, update_id=1)))
    asyncio.run(bot.handle_update(_message("what do I hold", chat_id=222, user_id=222, update_id=2)))

    first, second = seen[0], seen[1]
    assert first[0].session_id == "tg:111" and second[0].session_id == "tg:222"
    assert first[1].user["id"] != second[1].user["id"]             # own account
    assert first[1].account_id != second[1].account_id             # own credits and rate limit


def test_one_persons_bound_wallet_does_not_reach_another_in_the_same_group(monkeypatch, sent):
    _turns(monkeypatch)
    asyncio.run(bot.handle_update(_message(f"/wallet {SOL}", chat_id=-100999, user_id=111, chat_type="supergroup")))
    mine = asyncio.run(sessions.get_session_context("tg:-100999:u111"))
    theirs = asyncio.run(sessions.get_session_context("tg:-100999:u222"))
    assert mine["mcp_wallet_address"] == SOL
    assert "mcp_wallet_address" not in theirs


# ------------------------------------------------------- keeping one account

def test_a_new_user_is_told_they_may_already_have_an_account(monkeypatch, sent):
    """The fork happens silently otherwise: nothing maps a Telegram id to a
    web account, so the user has to be told before they build a second one."""
    _turns(monkeypatch)
    asyncio.run(bot.handle_update(_message("/start")))
    text = " ".join(str(p.get("text", "")) for m, p in sent if m == "sendMessage")
    assert "/link" in text and "same account" in text


def test_a_user_who_already_linked_is_not_nagged(monkeypatch, sent):
    _turns(monkeypatch)
    tg_user = {"id": 999, "username": "trader"}
    account, _ = asyncio.run(tg_identity.account_for(tg_user))
    asyncio.run(accounts.link_wallet(account["id"], "solana", SOL))    # a second way in

    asyncio.run(bot.handle_update(_message("/start")))
    text = " ".join(str(p.get("text", "")) for m, p in sent if m == "sendMessage")
    assert "Already use Orbit on the web" not in text


def test_link_explains_that_email_is_not_required(monkeypatch, sent):
    """A wallet-only web account links exactly as well as an email one."""
    _turns(monkeypatch)
    asyncio.run(bot.handle_update(_message("/link")))
    message = [p for m, p in sent if m == "sendMessage"][-1]
    assert "Email is not required" in str(message["text"])
    assert "MetaMask" in str(message["text"])
    assert "Do not forward" in str(message["text"])          # it is a bearer credential
    assert "/ui/?tglink=" in message["reply_markup"]["inline_keyboard"][0][0]["url"]


def test_link_says_nothing_to_do_when_already_linked(monkeypatch, sent):
    """`email` is not a mutable column -- an account gets one by BEING the
    email account, so the linked state is reached the way a user reaches it."""
    _turns(monkeypatch)
    tg_user = {"id": 999}
    web = asyncio.run(accounts.get_or_create_user("ravi@example.com"))[0]
    asyncio.run(tg_identity.link_to_account(tg_user, web["id"]))
    asyncio.run(bot.handle_update(_message("/link")))
    assert "already on your Anvaya account" in str([p for m, p in sent if m == "sendMessage"][-1]["text"])


def test_running_out_of_credits_also_mentions_linking(monkeypatch, sent):
    from app.service_errors import ServiceError

    async def refuse(body, identity):
        raise ServiceError(402, "You are out of credits.")

    monkeypatch.setattr(execution_policy, "execute_chat_turn", refuse)
    asyncio.run(bot.handle_update(_message("deep dive on BONK")))
    text = " ".join(str(p.get("text", "")) for m, p in sent if m in ("sendMessage", "editMessageText"))
    assert "out of credits" in text and "/link" in text


# -------------------------------------------- linking started from Telegram

def test_link_hands_back_a_url_that_already_knows_who_you_are(monkeypatch, sent):
    """The common case is someone who met Orbit in Telegram and has no account
    yet. They should never have to find a Connect button on the other side."""
    _turns(monkeypatch)
    asyncio.run(bot.handle_update(_message("/link")))
    button = [p for m, p in sent if m == "sendMessage"][-1]["reply_markup"]["inline_keyboard"][0][0]
    assert "?tglink=" in button["url"]
    token = button["url"].split("tglink=")[1]
    payload = asyncio.run(tg_identity.consume_link_token(token))
    assert payload["kind"] == "telegram" and payload["id"] == "999"


def test_claiming_attaches_telegram_to_whatever_account_you_signed_into(monkeypatch, sent):
    pushed = []

    async def fake_send(chat_id, text, **kwargs):
        pushed.append((chat_id, text))
        return {"message_id": 1}

    monkeypatch.setattr(tg_client, "send_message", fake_send)
    token = asyncio.run(tg_identity.start_link_from_telegram({"id": 999, "username": "trader"}))

    http = TestClient(app)
    # Signed out, the claim is refused outright.
    assert http.post("/auth/telegram/claim", json={"token": token}).status_code in (401, 403)

    me = sign_in(http, email="ravi@example.com")
    # Nothing attaches on its own: the page previews who the link would connect and the person confirms.
    assert http.post("/auth/telegram/claim", json={"token": token}).status_code == 400
    preview = http.get("/auth/telegram/claim/preview", params={"token": token})
    assert preview.status_code == 200 and preview.json()["display"] == "@trader" and preview.json()["confirmation"]
    assert asyncio.run(accounts.find_identity_owner("telegram", "999")) is None            # previewing linked nothing
    body = http.post("/auth/telegram/claim", json={"token": token, "confirmation": preview.json()["confirmation"]})
    assert body.status_code == 200 and body.json()["linked"] is True
    assert asyncio.run(accounts.find_identity_owner("telegram", "999"))["id"] == me["user"]["id"]
    # The loop closes where it started.
    assert pushed and "Connected" in pushed[0][1]


def test_a_claim_token_works_once_and_a_web_token_is_not_a_claim_token(monkeypatch, sent):
    http = TestClient(app)
    sign_in(http, email="ravi@example.com")

    token = asyncio.run(tg_identity.start_link_from_telegram({"id": 999}))
    monkeypatch.setattr(tg_client, "send_message", lambda *a, **k: _noop())

    def confirm(tok):
        pre = http.get("/auth/telegram/claim/preview", params={"token": tok})
        return pre.json().get("confirmation") if pre.status_code == 200 else None
    nonce = confirm(token)
    assert http.post("/auth/telegram/claim", json={"token": token, "confirmation": nonce}).status_code == 200
    assert confirm(token) is None                                                             # single use: no preview either
    assert http.post("/auth/telegram/claim", json={"token": token, "confirmation": nonce}).status_code == 400

    # A web->Telegram token must not be redeemable as a Telegram->web claim.
    web_token = asyncio.run(tg_identity.start_link("some-user-id"))
    assert http.get("/auth/telegram/claim/preview", params={"token": web_token}).status_code == 400
    assert http.post("/auth/telegram/claim", json={"token": web_token, "confirmation": "x"}).status_code == 400


def test_a_telegram_token_is_not_redeemable_as_a_start_payload(monkeypatch, sent):
    """And the reverse: /start must not accept a claim token."""
    _turns(monkeypatch)
    token = asyncio.run(tg_identity.start_link_from_telegram({"id": 999}))
    asyncio.run(bot.handle_update(_message(f"/start {token}")))
    text = " ".join(str(p.get("text", "")) for m, p in sent if m == "sendMessage")
    assert "expired" in text.lower()


async def _noop():
    return {"message_id": 1}


# ------------------------------------------------------------- knowing where you stand

def test_account_command_says_whether_the_chat_is_connected(monkeypatch, sent):
    """"Did it work?" is the first question after tapping a link, and a
    Telegram-only user has no Settings screen to look at."""
    _turns(monkeypatch)
    asyncio.run(bot.handle_update(_message("/account")))
    text = str([p for m, p in sent if m == "sendMessage"][-1]["text"])
    assert "Your Anvaya account" in text and "Credits" in text
    assert "Not connected to a web account" in text and "/link" in text


def test_account_command_confirms_a_connected_chat(monkeypatch, sent):
    _turns(monkeypatch)
    tg_user = {"id": 999, "username": "trader"}
    web = asyncio.run(accounts.get_or_create_user("ravi@example.com"))[0]
    asyncio.run(tg_identity.link_to_account(tg_user, web["id"]))

    asyncio.run(bot.handle_update(_message("/account")))
    text = str([p for m, p in sent if m == "sendMessage"][-1]["text"])
    assert "Connected to your web account" in text
    assert "ravi@example.com" in text


def test_account_shows_a_followed_wallet_as_read_only(monkeypatch, sent):
    _turns(monkeypatch)
    asyncio.run(bot.handle_update(_message(f"/wallet {SOL}", chat_id=777, user_id=999)))
    asyncio.run(bot.handle_update(_message("/account", chat_id=777, user_id=999, update_id=2)))
    text = str([p for m, p in sent if m == "sendMessage"][-1]["text"])
    assert "read-only" in text and SOL[:6] in text


def test_an_ordinary_answer_carries_no_open_orbit_button():
    """It was on every reply. A research answer is finished in the chat, and a
    button that leaves it on each one is noise; /app and /account offer the web."""
    response = _response("BONK top-10 hold 38%.", suggestions=["why is it moving?"])
    keyboard = render.keyboard(response, app_url="https://orbit.example.com/ui/?session=tg:1")
    buttons = [button for row in keyboard["inline_keyboard"] for button in row]
    assert buttons and all(button.get("callback_data") for button in buttons)
    assert not any(button.get("url") or "web_app" in button for button in buttons)


def test_a_slow_turn_is_not_cancelled_and_its_answer_still_arrives(monkeypatch, sent):
    """The turn is admitted and charged before the first provider call, so
    killing it at the timeout throws away paid work and leaves the
    conversation without its answer."""
    from app.models import AgentResponse

    started, finished = asyncio.Event(), []

    async def slow_turn(body, identity):
        started.set()
        await asyncio.sleep(0.2)          # outlives the timeout below
        finished.append(body.message)
        return AgentResponse(answer="the late answer", session_id=body.session_id)

    monkeypatch.setattr(execution_policy, "execute_chat_turn", slow_turn)
    monkeypatch.setattr(settings, "telegram_turn_timeout_seconds", 0.01)

    async def run():
        await bot.handle_update(_message("deep dive on BONK"))
        # Let the orphaned turn finish and deliver on its own.
        await asyncio.sleep(0.4)

    asyncio.run(run())

    assert finished == ["deep dive on BONK"], "the paid turn was cancelled"
    text = " ".join(str(p.get("text", "")) for m, p in sent if m in ("sendMessage", "editMessageText"))
    assert "Still working" in text          # the user is told, not left hanging
    assert "the late answer" in text        # and the answer arrives when it lands



# ---------------------------------------------------------------- review of 330bc651

def test_a_confirmation_is_bound_to_the_session_that_previewed(monkeypatch, sent):
    """An attacker's link opened by a victim previews only; a nonce from one
    session cannot confirm in another, and a confirmation without a preview
    does not exist."""
    monkeypatch.setattr(tg_client, "send_message", lambda *a, **k: _noop())
    token = asyncio.run(tg_identity.start_link_from_telegram({"id": 4242, "username": "attacker"}))
    victim = TestClient(app)
    sign_in(victim, email="victim@example.com")
    nonce = victim.get("/auth/telegram/claim/preview", params={"token": token}).json()["confirmation"]
    other = TestClient(app)
    sign_in(other, email="other@example.com")
    assert other.post("/auth/telegram/claim", json={"token": token, "confirmation": nonce}).status_code == 400
    assert victim.post("/auth/telegram/claim", json={"token": token, "confirmation": "made-up"}).status_code == 400
    assert asyncio.run(accounts.find_identity_owner("telegram", "4242")) is None


def test_a_disabled_deployment_rejects_every_update(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", None)
    monkeypatch.setattr(settings, "telegram_webhook_secret", None)
    assert tg_client.webhook_secret() is None and not tg_client.enabled()
    http = TestClient(app)
    guess = __import__("hashlib").sha256(b"orbit-telegram-webhook:").hexdigest()[:32]
    r = http.post(f"/telegram/webhook/{guess}", json={"update_id": 1, "message": {"text": "hi", "chat": {"id": 1, "type": "private"}, "from": {"id": 1}}},
                  headers={"X-Telegram-Bot-Api-Secret-Token": guess})
    assert r.status_code == 404


def test_after_unlink_the_next_message_gets_its_own_conversation(monkeypatch, sent):
    seen = _turns(monkeypatch)

    def update(text, n):
        u = _message(text); u["update_id"] = n
        return u
    asyncio.run(bot.handle_update(update("price of BONK", 101)))
    first_session = seen[-1][0].session_id
    asyncio.run(bot.handle_update(update("/unlink", 102)))
    asyncio.run(bot.handle_update(update("price of WIF", 103)))
    assert seen[-1][0].session_id != first_session and seen[-1][0].session_id.startswith(first_session)
    assert not any("belongs to another account" in str(p.get("text", "")) for m, p in sent if m == "sendMessage")


def test_a_button_on_an_older_answer_is_refused_not_redirected(monkeypatch, sent):
    old_key, new_key = render.answer_key("first answer"), render.answer_key("second answer")

    async def fake_messages(session_id):
        return [{"role": "assistant", "content": "first answer", "suggestions": ["Explain BONK holders"]},
                {"role": "assistant", "content": "second answer", "suggestions": ["Watch my ANSEM exit"]}]
    monkeypatch.setattr(bot.sessions, "get_messages", fake_messages)
    seen = _turns(monkeypatch)
    asyncio.run(bot.handle_update({"update_id": 7, "callback_query": {"id": "cb7", "data": f"sg:{old_key}:0", "from": {"id": 999},
                                                                       "message": {"message_id": 1, "chat": {"id": 555, "type": "private"}}}}))
    assert seen[-1][0].message == "Explain BONK holders"                                  # the old button still means what it said
    asyncio.run(bot.handle_update({"update_id": 8, "callback_query": {"id": "cb8", "data": "sg:0", "from": {"id": 999},
                                                                       "message": {"message_id": 1, "chat": {"id": 555, "type": "private"}}}}))
    assert seen[-1][0].message == "Explain BONK holders" and any("older answer" in str(p.get("text", "")) for m, p in sent if m == "sendMessage")
