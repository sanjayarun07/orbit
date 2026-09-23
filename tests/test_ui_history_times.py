"""Reloaded conversations retain message times instead of showing reload time."""
from tests.test_ui_swap_flow import run_case


def test_history_passes_persisted_times_and_keeps_unknown_legacy_times_unknown():
    result = run_case("history_preserves_message_times")
    assert result["loadError"] is None
    assert result["seen"] == [
        {"role": "user", "text": "old question", "ts": 1700000000},
        {"role": "assistant", "ts": 1700000060},
        {"role": "user", "text": "legacy question", "ts": None},
        {"role": "assistant", "ts": None},
    ]
