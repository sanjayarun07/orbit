from datetime import datetime, timezone

import pytest

from app import dune_tools
from app.settings import settings


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_execute_sql_posts_to_sql_execute_with_api_key_header(monkeypatch):
    monkeypatch.setattr(settings, "dune_api_key", "dune-key")
    captured = {}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, **kwargs):
            captured.update({"url": url, **kwargs})
            return _Response({"execution_id": "exec_123", "state": "QUERY_STATE_PENDING"})

    monkeypatch.setattr(dune_tools.httpx, "Client", Client)
    execution_id = dune_tools.execute_sql("SELECT 1", performance="small")
    assert execution_id == "exec_123"
    assert captured["url"].endswith("/sql/execute")
    assert captured["headers"]["X-Dune-Api-Key"] == "dune-key"
    assert captured["json"] == {"sql": "SELECT 1", "performance": "small"}


def test_execute_sql_requires_api_key(monkeypatch):
    monkeypatch.setattr(settings, "dune_api_key", None)
    with pytest.raises(RuntimeError, match="DUNE_API_KEY"):
        dune_tools.execute_sql("SELECT 1")


def test_poll_execution_loops_until_completed(monkeypatch):
    monkeypatch.setattr(settings, "dune_api_key", "dune-key")
    monkeypatch.setattr(settings, "dune_poll_interval_seconds", 0.0)
    responses = iter([
        {"state": "QUERY_STATE_PENDING"},
        {"state": "QUERY_STATE_PENDING"},
        {"state": "QUERY_STATE_COMPLETED"},
    ])

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url, **_kwargs):
            return _Response(next(responses))

    monkeypatch.setattr(dune_tools.httpx, "Client", Client)
    result = dune_tools.poll_execution("exec_123")
    assert result["state"] == "QUERY_STATE_COMPLETED"


def test_poll_execution_raises_on_failed_state(monkeypatch):
    monkeypatch.setattr(settings, "dune_api_key", "dune-key")

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url, **_kwargs):
            return _Response({"state": "QUERY_STATE_FAILED", "error": "bad sql"})

    monkeypatch.setattr(dune_tools.httpx, "Client", Client)
    with pytest.raises(dune_tools.DuneQueryError, match="QUERY_STATE_FAILED"):
        dune_tools.poll_execution("exec_123")


def test_poll_execution_times_out(monkeypatch):
    monkeypatch.setattr(settings, "dune_api_key", "dune-key")
    monkeypatch.setattr(settings, "dune_poll_interval_seconds", 0.0)
    monkeypatch.setattr(settings, "dune_poll_timeout_seconds", 0.0)

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url, **_kwargs):
            return _Response({"state": "QUERY_STATE_PENDING"})

    monkeypatch.setattr(dune_tools.httpx, "Client", Client)
    with pytest.raises(dune_tools.DuneQueryError, match="did not complete"):
        dune_tools.poll_execution("exec_123")


def test_fetch_results_parses_rows(monkeypatch):
    monkeypatch.setattr(settings, "dune_api_key", "dune-key")

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url, **_kwargs):
            return _Response({"result": {"rows": [{"trader_id": "abc", "volume_usd": 100.0}]}})

    monkeypatch.setattr(dune_tools.httpx, "Client", Client)
    rows = dune_tools.fetch_results("exec_123")
    assert rows == [{"trader_id": "abc", "volume_usd": 100.0}]


_MINT = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
_WALLET_A = "3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS"
_WALLET_B = "So11111111111111111111111111111111111111112"


def test_build_top_traders_sql_includes_window_and_token():
    sql = dune_tools.build_top_traders_sql(
        _MINT, None,
        datetime(2026, 9, 12, tzinfo=timezone.utc), datetime(2026, 9, 13, tzinfo=timezone.utc), 100,
    )
    assert _MINT in sql
    assert "2026-09-12 00:00:00" in sql
    assert "2026-09-13 00:00:00" in sql
    assert "LIMIT 100" in sql


def test_build_wallet_trades_sql_includes_every_wallet():
    sql = dune_tools.build_wallet_trades_sql(
        [_WALLET_A, _WALLET_B], _MINT,
        datetime(2026, 9, 12, tzinfo=timezone.utc), datetime(2026, 9, 13, tzinfo=timezone.utc),
    )
    assert f"'{_WALLET_A}'" in sql
    assert f"'{_WALLET_B}'" in sql


def test_sql_builders_reject_non_base58_values_that_could_inject():
    """Dune's API takes SQL as text, so an interpolated mint/wallet that isn't
    a base58 address must be rejected before it can break out of its quoted
    literal -- a quote-carrying value never reaches the query string.
    """
    injection = "x' OR '1'='1"
    window = (datetime(2026, 9, 12, tzinfo=timezone.utc), datetime(2026, 9, 13, tzinfo=timezone.utc))
    for bad in (injection, "MintAddress111", ""):
        try:
            dune_tools.build_top_traders_sql(bad, None, *window, 100)
            assert False, f"expected ValueError for token_mint={bad!r}"
        except ValueError:
            pass
    try:
        dune_tools.build_wallet_trades_sql([injection], _MINT, *window)
        assert False, "expected ValueError for an injecting wallet id"
    except ValueError:
        pass
