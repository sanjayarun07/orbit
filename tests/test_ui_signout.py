"""Signing out clears the conversation on screen (live, 2026-09-22): the
account chip went to Trial and the sidebar emptied, but the transcript stayed
until a reload -- a privacy exposure on a shared screen. The real inline
script runs in tests/js/ui_harness.mjs."""
from tests.test_ui_swap_flow import run_case


def test_sign_out_clears_the_transcript_on_screen():
    out = run_case("signout_clears_the_transcript")
    assert out["loadError"] is None and out["signedOut"]
    assert "what does my wallet hold" not in out["transcript"]
