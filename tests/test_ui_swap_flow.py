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
