"""The browser code, actually executed.

Two defects in a row reached review here while every Python test passed, and
neither was reachable from Python: a quote path that read an undeclared
variable and threw before it ever called Relay, and a dialog that disabled its
own button on a failure path and never re-enabled it. `node --check` proves the
file parses, which is not the same as proving it works.

tests/js/ui_harness.mjs loads the real inline script from index.html into a
stub DOM and calls its functions. These tests read back what the page passed
outward and what it left on screen. Both cases below were confirmed to fail
against commit 6b458a0d before the fix, which is the only reason to trust that
they would fail again.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "js" / "ui_harness.mjs"


def run_case(name: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the browser-code tests cannot run")
    result = subprocess.run(
        [node, str(HARNESS), name], cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.stdout.strip(), f"harness produced no result for {name}:\n{result.stderr[:2000]}"
    payload = json.loads(result.stdout.strip().splitlines()[0])
    assert "error" not in payload, f"{name} failed inside the harness: {payload['error'][:1500]}"
    return payload


def test_the_inline_script_loads_without_error():
    """If page setup throws, every later assertion here is meaningless."""
    assert run_case("quote_reaches_relay")["loadError"] is None


def test_a_standalone_swap_quote_reaches_relay_with_the_requested_slippage():
    """The regression: `slippage` was read here without ever being declared,
    and the dialog had no slippage field at all, so every quote threw
    ReferenceError inside the handler's own try block and surfaced as a vague
    error message instead of a quote."""
    result = run_case("quote_reaches_relay")
    args = result["quoteArgs"]
    assert args is not None, f"the quote never reached Relay; status was {result['status']!r}"
    assert args["chainId"] == 792703809 and args["toChainId"] == 8453
    assert args["currency"] == "SOL" and args["toCurrency"] == "USDC"
    assert args["amount"] == "0.01"
    assert args["slippageBps"] == 50, "the slippage the user typed must reach the quote"
    assert "Nothing has been signed" in result["status"]


def test_blank_slippage_means_relay_decides_rather_than_zero():
    """Blank must not become 0 bps, which would be a quote that cannot fill."""
    args = run_case("quote_allows_blank_slippage")["quoteArgs"]
    assert args is not None
    assert args.get("slippageBps") is None, f"blank slippage became {args.get('slippageBps')!r}"


def test_an_impossible_slippage_is_refused_before_any_network_call():
    result = run_case("quote_rejects_impossible_slippage")
    assert result["quoteArgs"] is None, "an out-of-range slippage still reached Relay"
    assert "between 0 and 10,000" in result["status"]


def test_the_swap_dialog_recovers_once_configuration_loads():
    """The dialog disabled its own controls when configuration could not be
    read and never restored them, so one failed fetch left swapping dead until
    the page was reloaded."""
    result = run_case("dialog_recovers_after_config_failure")
    assert result["afterFailure"]["quoteDisabled"] is True
    assert "Could not confirm" in result["afterFailure"]["status"]

    recovered = result["afterRecovery"]
    assert recovered["quoteDisabled"] is False, "the dialog stayed disabled after configuration loaded"
    assert recovered["quoteLabel"] == "Get quote"
    assert recovered["executionEnabled"] is True
    assert "Could not confirm" not in recovered["status"]


def test_research_mode_and_unreachable_configuration_say_different_things():
    """"We cannot move funds" and "we could not find out" are different facts
    and only one of them is worth retrying."""
    result = run_case("dialog_reports_research_mode_distinctly")
    assert result["configLoaded"] is True
    assert result["quoteDisabled"] is True
    assert "research mode" in result["status"]
    assert "Could not confirm" not in result["status"]


def test_a_quote_still_in_flight_is_discarded_when_the_inputs_change():
    """The reviewed race: ask for 50 bps, change the field to 1 while the
    response is outstanding, then sign. The response used to be assigned to the
    quote state straight after the await, restoring the superseded quote over
    inputs the user had already changed -- so the sign handler received a 50 bps
    quote while the form on screen said 1."""
    result = run_case("quote_in_flight_is_discarded_when_inputs_change")
    assert result["requestedSlippage"] == 50, "fixture must actually request the old slippage"
    assert result["quoteRetained"] is False, "a superseded response restored itself as the live quote"
    assert result["signedQuotes"] == 0, "a quote built from stale inputs reached the execution handler"
    assert "discarded" in result["status"]


def test_a_reviewed_quote_cannot_be_signed_after_the_inputs_change():
    result = run_case("reviewed_quote_cannot_be_signed_after_inputs_change")
    assert result["reviewed"] is True, "fixture must produce a reviewable quote first"
    assert result["signedQuotes"] == 0, "signing went ahead after the inputs changed"


def test_execution_refuses_a_quote_whose_revision_has_been_superseded():
    """The execute-side check on its own. Clearing the quote already stops the
    common path, so this is the guard that catches a future change which bumps
    the revision without clearing -- exercised directly rather than left as
    code nobody has seen run."""
    result = run_case("execution_refuses_a_quote_from_a_superseded_revision")
    assert result["signedQuotes"] == 0
    assert "Inputs changed after this quote" in result["status"]


def test_an_unchanged_reviewed_quote_still_signs():
    """The guard must block stale quotes, not swapping. Without this the two
    tests above would pass on a dialog that simply never signs anything."""
    result = run_case("an_unchanged_reviewed_quote_still_signs")
    assert result["signedQuotes"] == 1, "a valid, unchanged quote was refused"
    assert "completed" in result["status"]


def test_editing_inputs_during_the_execution_claim_stops_wallet_approval():
    """The reviewed gap. The revision check runs before the executor is called,
    and the executor then awaits the server's claim over the network. An edit
    during that wait used to change nothing: the superseded quote went on to
    wallet approval anyway.

    Driven through the REAL compiled app/static/executors.js, not an imitation
    of it, because the guard immediately before wallet approval lives there --
    it had always existed, and the page simply never passed the callback.
    """
    result = run_case("execution_aborts_when_inputs_change_during_the_claim")
    assert result["requestedSlippage"] == 50, "fixture must quote the old slippage first"
    assert result["walletApprovals"] == 0, "a superseded quote reached the wallet handler"
    assert "superseded" in result["status"]


def test_the_execution_claim_still_reaches_the_wallet_when_nothing_changes():
    """The control. Without it the test above passes on a page that can never
    sign anything at all."""
    result = run_case("execution_reaches_the_wallet_when_nothing_changes")
    assert result["walletApprovals"] == 1, "an unchanged quote never reached the wallet"
    assert "completed" in result["status"]


def test_typing_in_the_inline_card_invalidates_its_quote_without_blurring():
    """The reviewed gap. The inline card listened only for `change`, which a
    text or number field withholds until it loses focus, so editing slippage
    while an execution claim was pending left the revision untouched and the
    freshness callback passed. `input` is what a real keystroke fires.
    """
    result = run_case("inline_card_edit_during_claim")
    assert result["askedSlippage"] == 50, "the card never quoted; the case proves nothing"
    assert result["walletApprovals"] == 0, "a stale quote reached the wallet after an unblurred edit"
    assert "superseded" in result["status"]


def test_the_inline_card_still_invalidates_on_change_for_its_selects():
    """`change` is the only event a select emits, so it has to keep working."""
    result = run_case("inline_card_edit_during_claim_on_change")
    assert result["askedSlippage"] == 50
    assert result["walletApprovals"] == 0


def test_the_inline_card_still_signs_when_nothing_is_edited():
    """The control. The two tests above would pass on a card that can never
    sign at all, which is the failure mode this whole file keeps finding."""
    result = run_case("inline_card_signs_when_nothing_changes")
    assert result["askedSlippage"] == 50
    assert result["walletApprovals"] == 1, "an untouched quote never reached the wallet"
    assert "completed" in result["status"]


def test_the_inline_swap_card_enforces_the_risk_charter():
    """The reviewed finding: the inline card's quote path never called the
    charter veto, so a $1,000 trade at 50 bps reached the wallet handler on an
    account whose charter allowed $10 and 10 bps -- limits the dialog and the
    chat card both honoured."""
    result = run_case("charter_blocks_an_over_limit_swap")
    assert result["quotedUsd"] == 1000, "the card never quoted; the case proves nothing"
    assert result["walletApprovals"] == 0, "an over-limit swap reached the wallet handler"
    assert "risk charter" in result["status"]


def test_the_swap_dialog_enforces_the_risk_charter_too():
    """Same limits, same refusal, other surface. One rule or it is not a rule."""
    result = run_case("charter_blocks_an_over_limit_swap_dialog")
    assert result["walletApprovals"] == 0
    assert "risk charter" in result["status"]
