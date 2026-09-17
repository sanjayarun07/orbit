"""The six findings of the 2026-09-17 UI QA, as browser-code regressions.

Each is the QA's reproduction inverted -- the real inline script from
index.html run in tests/js/ui_harness.mjs with the server answering 503 --
plus the control that shows the confirmed path still completes. UI-06 (the
admin layout at 320 px) is CSS and cannot be proved here; it was measured in
Chrome (see docs/production-operations.md).
"""
from tests.test_ui_swap_flow import run_case


# --- UI-01 · delete all conversations -----------------------------------------

def test_a_failed_delete_all_keeps_the_conversations_and_says_so():
    result = run_case("delete_all_keeps_what_the_server_did_not_delete")
    assert result["deletes"] == 3, "both per-conversation deletes and the account delete must be attempted"
    assert result["outcome"]["ok"] is False
    assert sorted(result["remaining"]) == ["a", "b"], "conversations the server kept were dropped from view"
    assert "could not be deleted" in result["outcome"]["message"] and "Try again" in result["outcome"]["message"]


def test_a_confirmed_delete_all_clears_the_list():
    result = run_case("delete_all_clears_the_list_when_the_server_confirms")
    assert result["outcome"] == {"ok": True} and result["remaining"] == []


# --- UI-02 · preference saves ---------------------------------------------------

def test_a_failed_preference_save_keeps_the_dialog_open_with_the_typed_value():
    result = run_case("preference_save_reports_a_failed_server_write")
    assert result["sent"] == "New name", "the save was never attempted"
    assert result["dialogOpen"] and result["typedValueKept"] == "New name"
    assert result["storedName"] is None, "an unconfirmed account value was written to browser storage"
    assert result["status"].startswith("Not saved") and result["saveEnabled"]


def test_a_confirmed_preference_save_closes_and_applies():
    result = run_case("preference_save_applies_once_the_server_confirms")
    assert result["sent"] == "New name" and not result["dialogOpen"]
    assert result["storedName"] == "New name" and result["status"] == ""


# --- UI-03 · leaving a team -----------------------------------------------------

def test_a_failed_team_departure_stays_on_members_with_an_error():
    result = run_case("leaving_a_team_reports_a_failed_departure")
    assert result["tabsSwitchedTo"] == [], "the panel changed before the server answered"
    assert result["status"].startswith("Could not leave the team")
    assert result["buttonEnabled"] and result["buttonText"] == "Leave team"


def test_a_confirmed_team_departure_returns_to_account():
    result = run_case("leaving_a_team_returns_to_account_when_confirmed")
    assert result["tabsSwitchedTo"] == ["account"] and result["status"] == ""


# --- UI-04 · token suggestion placement ---------------------------------------

def test_the_suggestion_list_never_opens_behind_the_header():
    """The QA geometry: 1440x1000, composer top at 356, a 64 px header and a
    320 px list. Measured against the viewport edge there was 'room' above,
    so the list opened over the header and its first row was unclickable."""
    result = run_case("mention_menu_never_opens_behind_the_header")
    assert result["below"] is True, f"the list opened above, with its top at y={result['topIfAbove']} inside the {result['headerBottom']}px header"


def test_the_suggestion_list_still_opens_above_a_composer_at_the_bottom():
    result = run_case("mention_menu_opens_above_a_composer_at_the_bottom")
    assert result["below"] is False and result["maxHeight"] == 320
    assert result["topIfAbove"] >= result["headerBottom"]


# --- UI-05 · public wallet address --------------------------------------------

def test_text_that_is_not_an_address_is_refused_and_the_dialog_stays_open():
    result = run_case("a_public_address_that_is_not_one_is_refused")
    assert result["errorShown"] and "not a valid wallet address" in result["errorText"]
    # The typed text stays in the field for correction; nothing adopted it.
    assert result["dialogOpen"] and "connected" not in result["classes"] and result["label"] == ""


def test_a_real_address_is_adopted_and_marked_view_only():
    result = run_case("a_real_public_address_is_adopted_as_view_only")
    assert not result["errorShown"] and not result["dialogOpen"]
    assert result["adopted"] == "So11111111111111111111111111111111111111112"
    assert "view only" in result["label"] and "readonly" in result["classes"]


def test_well_formed_base58_that_is_not_a_32_byte_key_is_refused():
    """Follow-up to UI-05: the alphabet-and-length regex accepted strings that
    cannot be a Solana public key. The string is now decoded and must be
    exactly 32 bytes."""
    result = run_case("a_base58_string_that_is_not_32_bytes_is_refused")
    for name in ("fortyThreeOnes", "fortyFourZs", "thirtyTwoChars"):
        assert result[name]["regex"] is True, f"{name} must be a regex-passing string, or this proves nothing"
        assert result[name]["bytes"] != 32 and result[name]["accepted"] is False, (name, result[name])
    assert result["systemProgram"] == {"regex": True, "bytes": 32, "accepted": True}
    assert result["wrappedSol"] == {"regex": True, "bytes": 32, "accepted": True}
    assert result["evm"]["accepted"] is True
