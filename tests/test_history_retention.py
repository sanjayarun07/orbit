"""STAB-05: conversation retention, and who a conversation belongs to.

The promise being tested, stated once so the tests can be read against it:

* **Retention is measured from the last write.** Every turn appended to a
  conversation pushes its expiry out again; reading it does not. So "30 days"
  means thirty days after the last message, not after the conversation started.
* **Signed out, a conversation is scratch space.** Two hours, and it is never
  mapped to an account, so it cannot be listed or recovered later.
* **Signed in, it is history.** Thirty days, listed by account, and it survives
  a restart or a different worker handling the next turn.
* **The 200-message cap is a display cap.** A conversation keeps its most
  recent 200 messages; older ones are trimmed as new ones arrive.

The regression this file exists for: the retention a conversation had been
granted lived only in a per-process dict, so any process that had not itself
granted it -- a restarted API, or simply a second worker -- wrote the short
scratch expiry back over a signed-in account's history.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import accounts, sessions
from app.db import get_redis
from app.main import app
from app.settings import settings
from tests.conftest import sign_in


HISTORY_KEY = "chat_history:{}"


def _ttl(session_id: str) -> int | None:
    """Seconds left on the stored transcript, or None without Redis."""
    async def _read():
        redis = await get_redis()
        if redis is None:
            return None
        return await redis.ttl(HISTORY_KEY.format(session_id))
    return asyncio.run(_read())


def _append(session_id: str, text: str) -> None:
    asyncio.run(sessions.append_turn(session_id, "user", text))


def _extend_signed_in(session_id: str) -> None:
    asyncio.run(sessions.extend_retention(session_id, settings.chat_history_signed_in_ttl_seconds))


def _forget_process_state(session_id: str) -> None:
    """Everything this process happens to remember about the session.

    Stands in for the two cases that matter operationally and are otherwise
    hard to reach in a test: the API restarted, or the next turn is handled by
    a worker that never saw the turn that granted the long retention.
    """
    sessions._retention.pop(session_id, None)


@pytest.fixture
def redis_backed():
    if _ttl("probe-for-redis") is None:
        pytest.skip("retention is expressed as Redis key TTLs; no Redis configured")


# --- retention survives a restart or a second worker --------------------------

def test_a_signed_in_conversation_keeps_its_long_retention_on_a_worker_that_never_granted_it(redis_backed):
    """The regression. Worker A grants thirty days; worker B appends the next
    turn knowing nothing about it, and must not shorten the expiry."""
    session_id = "retention-across-workers"
    _append(session_id, "first turn")
    _extend_signed_in(session_id)
    granted = _ttl(session_id)
    assert granted > settings.chat_history_ttl_seconds

    _forget_process_state(session_id)
    _append(session_id, "next turn, handled elsewhere")

    kept = _ttl(session_id)
    assert kept > settings.chat_history_ttl_seconds, (
        f"retention collapsed from {granted}s to {kept}s when a process that had not "
        "granted it appended a turn"
    )


def test_an_anonymous_conversation_stays_short_lived(redis_backed):
    session_id = "retention-anonymous"
    _append(session_id, "just looking")
    ttl = _ttl(session_id)
    # Redis reports -1 for a key with no expiry at all. Assert the lower bound
    # too: "never shortens" must not become "never expires". Redis treats a key
    # with no TTL as having an infinite one, so an extend-only write that used
    # GT alone would refuse to set the very first expiry and leak this
    # conversation forever.
    assert 0 < ttl <= settings.chat_history_ttl_seconds, f"first message left TTL {ttl}"


def test_the_very_first_message_of_a_conversation_gets_a_real_expiry(redis_backed):
    """The specific failure the NX half of the extend-only write prevents."""
    session_id = "retention-first-message-expiry"
    asyncio.run(sessions.clear_history(session_id))
    _append(session_id, "the only message so far")
    assert _ttl(session_id) > 0, "a brand-new conversation was stored with no expiry"


def test_committing_a_turn_also_leaves_both_keys_expiring(redis_backed):
    """commit_turn writes the transcript and the routing context together; the
    context key is overwritten by value on every turn, so it is the one that
    could quietly lose its expiry or have it reset."""
    session_id = "retention-commit-turn"
    asyncio.run(sessions.clear_history(session_id))
    asyncio.run(sessions.commit_turn(session_id, "hello", "hi there", {}, {"revision": 1}))
    asyncio.run(sessions.extend_retention(session_id, settings.chat_history_signed_in_ttl_seconds))

    async def _context_ttl():
        redis = await get_redis()
        return await redis.ttl(f"chat_context:{session_id}")

    assert _ttl(session_id) > settings.chat_history_ttl_seconds
    assert asyncio.run(_context_ttl()) > settings.chat_history_ttl_seconds

    _forget_process_state(session_id)
    asyncio.run(sessions.commit_turn(session_id, "again", "sure", {}, {"revision": 2}))
    assert _ttl(session_id) > settings.chat_history_ttl_seconds
    assert asyncio.run(_context_ttl()) > settings.chat_history_ttl_seconds


def test_writing_to_an_anonymous_conversation_slides_its_window_forward(redis_backed):
    """Short retention still refreshes on every write; it is a sliding two
    hours, not two hours from the first message."""
    session_id = "retention-sliding"
    _append(session_id, "one")

    async def _age_it():
        redis = await get_redis()
        await redis.expire(HISTORY_KEY.format(session_id), 60)
    asyncio.run(_age_it())
    assert _ttl(session_id) <= 60

    _append(session_id, "two")
    assert _ttl(session_id) > 60


def test_retention_is_measured_from_the_last_write_not_from_reading(redis_backed):
    session_id = "retention-read-does-not-extend"
    _append(session_id, "one")
    _extend_signed_in(session_id)

    async def _shorten():
        redis = await get_redis()
        await redis.expire(HISTORY_KEY.format(session_id), 300)
    asyncio.run(_shorten())

    asyncio.run(sessions.get_messages(session_id))
    assert _ttl(session_id) <= 300, "reading a conversation extended its retention"


def test_extending_retention_is_not_reversible_by_a_shorter_request(redis_backed):
    """extend_retention is for lengthening only; a short request must not be
    able to cut an account's history down."""
    session_id = "retention-no-downgrade"
    _append(session_id, "one")
    _extend_signed_in(session_id)
    long_ttl = _ttl(session_id)
    asyncio.run(sessions.extend_retention(session_id, 60))
    assert _ttl(session_id) > settings.chat_history_ttl_seconds
    assert _ttl(session_id) >= long_ttl - 5


# --- the display cap ----------------------------------------------------------

def test_a_conversation_keeps_its_most_recent_messages_up_to_the_display_cap():
    session_id = "retention-display-cap"
    cap = sessions._MAX_DISPLAY_MESSAGES
    for index in range(cap + 25):
        _append(session_id, f"message {index}")
    messages = asyncio.run(sessions.get_messages(session_id))
    assert len(messages) == cap
    assert messages[-1]["content"] == f"message {cap + 24}"
    assert messages[0]["content"] == "message 25", "the cap must trim the oldest, not the newest"


# --- ownership and listing ----------------------------------------------------

def test_history_belongs_to_the_account_not_the_browser():
    """Two accounts in one browser: signing in as the second must not show the
    first one's conversations."""
    client = TestClient(app)
    first = sign_in(client, email="first-account@example.com")
    client.post("/chat/history/session-of-first/claim") if False else None
    asyncio.run(accounts.touch_chat_session(first["user"]["id"], "conversation-of-first"))
    _append("conversation-of-first", "something private")
    assert any(c["session_id"] == "conversation-of-first"
               for c in client.get("/me/conversations").json()["conversations"])

    client.post("/auth/logout")
    sign_in(client, email="second-account@example.com")
    listed = client.get("/me/conversations").json()["conversations"]
    assert all(c["session_id"] != "conversation-of-first" for c in listed)


def test_one_account_sees_its_conversations_from_any_browser():
    first_browser = TestClient(app)
    me = sign_in(first_browser, email="two-browsers@example.com")
    asyncio.run(accounts.touch_chat_session(me["user"]["id"], "conversation-from-laptop"))
    _append("conversation-from-laptop", "started on the laptop")

    second_browser = TestClient(app)
    sign_in(second_browser, email="two-browsers@example.com")
    listed = second_browser.get("/me/conversations").json()["conversations"]
    assert any(c["session_id"] == "conversation-from-laptop" for c in listed)


def test_a_signed_out_visitor_has_no_listable_history():
    client = TestClient(app)
    assert client.get("/me/conversations").status_code in (401, 403)


def test_an_anonymous_conversation_becomes_the_account_that_signs_in_on_it():
    """The anonymous-to-signed-in transition: a conversation started signed out
    is claimable by the first account to use it, and then retained as theirs."""
    from app.session_access import require_session_access

    client = TestClient(app)
    session_id = "conversation-started-anonymously"
    _append(session_id, "asked before signing in")
    assert asyncio.run(accounts.chat_session_owner(session_id)) is None

    me = sign_in(client, email="claims-anon@example.com")
    asyncio.run(require_session_access(session_id, _identity_for(me["user"]["id"]), claim=True))
    assert asyncio.run(accounts.chat_session_owner(session_id)) == me["user"]["id"]

    _extend_signed_in(session_id)
    listed = client.get("/me/conversations").json()["conversations"]
    assert any(c["session_id"] == session_id for c in listed)


def test_a_claimed_conversation_is_not_claimable_by_anyone_else():
    from app.session_access import SessionAccessDenied, require_session_access

    session_id = "conversation-already-claimed"
    first, second = "user-claimant-one", "user-claimant-two"
    assert asyncio.run(accounts.touch_chat_session(first, session_id)) is True
    with pytest.raises(SessionAccessDenied):
        asyncio.run(require_session_access(session_id, _identity_for(second), claim=True))


def test_deleting_conversations_removes_the_transcript_not_just_the_listing():
    """"Delete my history" has to delete the messages. Dropping only the
    account mapping would leave the transcript readable by session id until it
    expired on its own."""
    client = TestClient(app)
    me = sign_in(client, email="deletes-history@example.com")
    session_id = "conversation-to-delete"
    asyncio.run(accounts.touch_chat_session(me["user"]["id"], session_id))
    _append(session_id, "please forget this")
    assert asyncio.run(sessions.get_messages(session_id))

    deleted = client.delete("/me/conversations")
    assert deleted.status_code == 200 and deleted.json()["deleted"] >= 1
    assert asyncio.run(sessions.get_messages(session_id)) == []
    assert client.get("/me/conversations").json()["conversations"] == []


def test_a_conversation_whose_transcript_has_expired_is_not_listed_as_a_phantom():
    """The listing and the transcript live in different stores. If the
    transcript is gone, the sidebar must not offer an empty conversation."""
    client = TestClient(app)
    me = sign_in(client, email="phantom-row@example.com")
    session_id = "conversation-with-expired-transcript"
    asyncio.run(accounts.touch_chat_session(me["user"]["id"], session_id))
    # Mapping exists, transcript never written or already expired.
    assert client.get("/me/conversations").json()["conversations"] == []


def _identity_for(user_id: str):
    class _Identity:
        signed_in = True
        user = {"id": user_id}
    return _Identity()
