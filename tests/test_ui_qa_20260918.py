"""UI alignment review of 2026-09-18: three confirmed findings.

1. Mobile settings: the selected tab could sit under the strip's permanent
   right fade (at 320px the Notifications tab ended 12px past the strip).
2. The API-key name field kept the browser's default inset border.
3. Desktop dialog footers painted square corners over the rounded frame.
"""
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_the_selected_settings_tab_is_scrolled_clear_and_fades_follow_the_scroll():
    html = (STATIC / "index.html").read_text()
    assert 'scrollIntoView({block:"nearest",inline:"center"})' in html, "the selected tab is brought clear of the edge fades"
    assert "function syncTabFades" in html and 'classList.toggle("fade-right"' in html and 'classList.toggle("fade-left"' in html
    css = (STATIC / "mobile.css").read_text()
    assert ".tabs.fade-right {" in css and ".tabs.fade-left {" in css
    permanent = [line for line in css.splitlines() if "mask-image" in line and "fade-" not in line]
    assert not permanent, f"a fade must depend on there being more to scroll: {permanent}"


def test_the_api_key_field_shares_the_other_fields_border_and_colour():
    css = (STATIC / "chat.css").read_text()
    rule = next(line for line in css.splitlines() if line.strip().startswith(".field input, .field select") and "border: 1px solid var(--border)" in line)
    assert ".key-create input" in rule, rule
    assert "color: var(--text)" in rule


def test_dialog_head_and_footer_follow_the_frames_corners():
    css = (STATIC / "product.css").read_text()
    footer = next(line for line in css.splitlines() if line.startswith(".dialog-footer {"))
    assert "border-bottom-left-radius: inherit" in footer and "border-bottom-right-radius: inherit" in footer
    head = next(line for line in css.splitlines() if line.startswith(".dialog-head {") and "radius" in line)
    assert "border-top-left-radius: inherit" in head


def test_the_sign_in_sheet_says_when_the_email_was_not_sent():
    """Seen on the phone (2026-09-18): Resend refused the placeholder sender
    and the sheet still said "Check your inbox"."""
    from tests.test_ui_swap_flow import run_case
    r = run_case("signin_sheet_reports_a_send_that_did_not_happen")
    assert "could not be sent" in r["false"] and "Check a@b.co" in r["true"], r
