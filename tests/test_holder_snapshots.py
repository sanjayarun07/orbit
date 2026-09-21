"""The holder snapshot ledger (app/holder_snapshots.py): tracking, cadence,
rows from live shapes, point-in-time reads, diffs and the deep-dive history card."""
import asyncio
from datetime import datetime, timedelta, timezone

from app import holder_snapshots as hs, mobula_client, token_deepdive
from app.settings import settings
from app.signals import Subject

_BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
_NEW = "Cm6fNnMk7NfzStP9CZpsQA2v3jjzbcYGAxdJySmHpump"


def _subject(address=_BONK, symbol="BONK"):
    return Subject(kind="token", id=address, chain="solana", symbol=symbol)


def _live_mobula(holders=None, security=None, market=None):
    """A stand-in for the budgeted door returning the live shapes probed on 2026-09-21."""
    def get(version, path, params, timeout=None):
        if path == "/token/holder-positions":
            return holders if holders is not None else [
                {"walletAddress": "A" * 44, "percentageOfTotalSupply": 12.5, "labels": [{"name": "Dev"}]},
                {"walletAddress": "B" * 44, "percentageOfTotalSupply": 6.0, "labels": ["sniper"]},
                {"walletAddress": "C" * 44, "percentageOfTotalSupply": 1.0, "labels": []},
            ]
        if path == "/token/security":
            return security if security is not None else {
                "top10HoldingsPercentage": 31.3, "top50HoldingsPercentage": 45.0, "isMintable": False, "isHoneypot": None, "renounced": True,
                "liquidityAnalysis": [{"poolType": "raydium", "burnedPercentage": 100.0, "lockedPercentage": 0.0, "unlockedPercentage": 0.0}],
            }
        if path == "/market/data":
            return market if market is not None else {"data": {"price": 3.0e-06, "liquidity": 168018.9, "market_cap": 264669441.6}}
        raise AssertionError(path)
    return get


# ---- one row from live shapes ----

def test_take_records_concentration_cohorts_lp_and_market(monkeypatch):
    monkeypatch.setattr(mobula_client, "get", _live_mobula())
    row = hs.take("solana", _BONK)
    assert row["top10_pct"] == 31.3 and row["top50_pct"] == 45.0
    assert row["dev_pct"] == 12.5 and row["sniper_pct"] == 6.0 and row["bundler_pct"] == 0.0
    assert row["lp_burned_pct"] == 100.0 and row["lp_unlocked_pct"] == 0.0
    assert row["price_usd"] == 3.0e-06 and row["market_cap_usd"] == 264669441.6
    assert row["top_holders"][0] == {"address": "A" * 44, "pct": 12.5, "labels": ["dev"]}
    assert row["flags"]["renounced"] is True and row["flags"]["missing"] == []


def test_take_says_what_is_missing_instead_of_inventing_zeros(monkeypatch):
    monkeypatch.setattr(mobula_client, "get", _live_mobula(holders=[], security={"top10HoldingsPercentage": 11.2}, market={}))
    row = hs.take("solana", _BONK)
    assert row["top10_pct"] == 11.2                       # security still answered
    assert row["dev_pct"] is None and row["top_holders"] == []
    assert row["lp_locked_pct"] is None and row["price_usd"] is None
    assert row["flags"]["missing"] == ["holders", "liquidity", "market"]


def test_take_survives_a_dead_endpoint(monkeypatch):
    def broken(version, path, params, timeout=None):
        raise RuntimeError("down")
    monkeypatch.setattr(mobula_client, "get", broken)
    row = hs.take("solana", _BONK)
    assert set(row["flags"]["missing"]) == {"holders", "security", "liquidity", "market"}


# ---- tracking and cadence ----

def test_tracking_extends_never_shortens_and_launches_go_dense(monkeypatch):
    launched = datetime.now(timezone.utc) - timedelta(hours=2)
    s = _subject(_NEW, "BUTTCOIN")
    asyncio.run(hs.track(s, "pulse", days=hs.LAUNCH_DAYS, launched_at=launched))
    asyncio.run(hs.track(s, "deep_dive", days=hs.DEEP_DIVE_DAYS))
    rows = asyncio.run(hs.tracked())
    assert len(rows) == 1 and rows[0]["launched_at"] == launched
    assert rows[0]["until"] > datetime.now(timezone.utc) + timedelta(days=hs.DEEP_DIVE_DAYS - 1)
    assert hs.cadence_minutes(rows[0]) == settings.holder_snapshot_fresh_minutes
    old = dict(rows[0], launched_at=datetime.now(timezone.utc) - timedelta(days=3))
    assert hs.cadence_minutes(old) == settings.holder_snapshot_interval_minutes


def test_due_picks_never_snapshotted_and_stale_rows_fresh_first():
    now = datetime.now(timezone.utc)
    fresh_stale = {"subject_key": "a", "launched_at": now - timedelta(hours=1), "last_snapshot": now - timedelta(minutes=15)}
    fresh_recent = {"subject_key": "b", "launched_at": now - timedelta(hours=1), "last_snapshot": now - timedelta(minutes=2)}
    old_never = {"subject_key": "c", "launched_at": now - timedelta(days=5), "last_snapshot": None}
    old_recent = {"subject_key": "d", "launched_at": now - timedelta(days=5), "last_snapshot": now - timedelta(minutes=30)}
    assert [r["subject_key"] for r in hs.due([old_never, fresh_stale, fresh_recent, old_recent], now)] == ["a", "c"]


def test_tick_snapshots_due_tokens_and_marks_them(monkeypatch):
    monkeypatch.setattr(mobula_client, "get", _live_mobula())
    s = _subject()
    asyncio.run(hs.track(s, "deep_dive", days=14))
    assert asyncio.run(hs.tick()) == 1
    assert asyncio.run(hs.tick()) == 0                    # within the interval: nothing due
    rows = asyncio.run(hs.history(s.key))
    assert len(rows) == 1 and rows[0]["top10_pct"] == 31.3


def test_a_disabled_ledger_takes_nothing(monkeypatch):
    monkeypatch.setattr(settings, "holder_snapshots_enabled", False)
    assert asyncio.run(hs.snapshot(_subject())) is None


# ---- launches from Pulse ----

def _pulse_item(address, symbol, **fields):
    return {"tokenSymbol": symbol, "created_at": "2026-09-20T19:40:25.000Z", "holders_count": 23, "top10HoldingsPercentage": 44.0,
            "devHoldingsPercentage": 5.0, "snipersHoldingsPercentage": 12.0, "bundlersHoldingsPercentage": 3.0, "insidersHoldingsPercentage": 0.0,
            "price": 1.2e-05, "market_cap": 12000.0, "deployer": "D" * 44, "source": "pump.fun",
            "pair": {"baseToken": {"address": address, "symbol": symbol}, "quoteToken": {"address": "So1" + "1" * 40, "symbol": "SOL"},
                     "token0": {"address": "So1" + "1" * 40, "symbol": "SOL"}, "token1": {"address": address, "symbol": symbol}, "liquidity": 3000.0},
            **fields}


def test_launch_address_follows_the_base_pointer_then_the_symbol_then_the_non_quote_side():
    assert hs.launch_address(_pulse_item(_NEW, "BUTTCOIN")) == _NEW
    # Live BNB shape: baseToken is the POINTER "token0"; token1 is Binance-peg DOGE, not the launch.
    doge = "0xbA2aE424d960c26247Dd6c32edC70B295c744C43"
    bnb = {"tokenSymbol": "BEEGE", "pair": {"baseToken": "token0", "quoteToken": "token1",
                                             "token0": {"address": "0x" + "b" * 40, "symbol": "BEEGE"}, "token1": {"address": doge, "symbol": "DOGE"}}}
    assert hs.launch_address(bnb) == "0x" + "b" * 40
    by_symbol = {"tokenSymbol": "GRND", "pair": {"token0": {"address": "G" * 44, "symbol": "GRND"}, "token1": {"address": "H" * 44, "symbol": "HOMO"}}}
    assert hs.launch_address(by_symbol) == "G" * 44
    two_memes = {"pair": {"token0": {"address": "X" * 44, "symbol": "SHITCOIN"}, "token1": {"address": _NEW, "symbol": "BUTTCOIN"}}}
    assert hs.launch_address(two_memes) == _NEW           # no pointer, no symbol: token1 first, as the launch card reads it
    assert hs.launch_address({"pair": {"token0": {"address": "So1", "symbol": "SOL"}}}) is None


def test_pulse_rows_drop_implausible_holder_counts_and_unsettled_market_caps():
    assert hs.row_from_pulse(_pulse_item(_NEW, "X", holders_count=252503))["holders_count"] is None
    thin = hs.row_from_pulse(_pulse_item(_NEW, "X", holders_count=2))
    assert thin["holders_count"] == 2 and thin["market_cap_usd"] is None


def test_discovery_tracks_launches_and_records_the_pulse_row_for_free(monkeypatch):
    monkeypatch.setattr(settings, "holder_snapshot_pulse_chains", "solana")
    calls = []

    def get(version, path, params, timeout=None):
        calls.append(path)
        return {"new": {"data": [_pulse_item(_NEW, "BUTTCOIN")]}, "bonding": [], "bonded": []}
    monkeypatch.setattr(mobula_client, "get", get)
    assert asyncio.run(hs.discover_launches()) == 1
    assert calls == ["/pulse"]                            # no per-token calls at discovery
    rows = asyncio.run(hs.tracked())
    assert rows[0]["source"] == "pulse" and rows[0]["symbol"] == "BUTTCOIN" and rows[0]["launched_at"].year == 2026
    first = asyncio.run(hs.history(f"solana:{_NEW.lower()}"))[0]
    assert first["holders_count"] == 23 and first["sniper_pct"] == 12.0 and first["liquidity_usd"] is None   # pair liquidity is one-sided
    assert first["flags"]["deployer"] == "D" * 44 and "liquidity" in first["flags"]["missing"]


# ---- reading: point in time, diffs, the history card ----

def _row(taken_at, **over):
    base = {"id": "x", "taken_at": taken_at, "top10_pct": 41.0, "top50_pct": 60.0, "holders_count": 100, "dev_pct": 8.0, "sniper_pct": 10.0,
            "bundler_pct": 0.0, "insider_pct": 0.0, "lp_burned_pct": 0.0, "lp_locked_pct": 0.0, "lp_unlocked_pct": 100.0,
            "price_usd": 1.0e-05, "liquidity_usd": 20000.0, "market_cap_usd": 100000.0,
            "top_holders": [{"address": "A" * 44, "pct": 20.0, "labels": ["dev"]}, {"address": "B" * 44, "pct": 5.0, "labels": []}],
            "flags": {"mintable": False, "honeypot": None, "renounced": False, "missing": []}}
    base.update(over)
    return base


def test_as_of_returns_only_rows_taken_before_the_moment():
    s = _subject()
    t0 = datetime.now(timezone.utc) - timedelta(hours=3)
    for i in range(3):
        asyncio.run(hs.store(s.key, _row(t0 + timedelta(hours=i), id=f"r{i}", top10_pct=41.0 - 5 * i)))
    assert asyncio.run(hs.as_of(s.key, t0 - timedelta(minutes=1))) is None
    assert asyncio.run(hs.as_of(s.key, t0 + timedelta(hours=1, minutes=30)))["id"] == "r1"
    assert asyncio.run(hs.as_of(s.key, datetime.now(timezone.utc)))["id"] == "r2"


def test_diff_names_what_moved_and_stays_quiet_below_thresholds():
    t = datetime.now(timezone.utc)
    a = _row(t)
    b = _row(t, top10_pct=23.0, lp_locked_pct=95.0, lp_unlocked_pct=5.0, price_usd=2.0e-05,
             top_holders=[{"address": "B" * 44, "pct": 5.0, "labels": []}, {"address": "C" * 44, "pct": 4.0, "labels": ["sniper"]}],
             flags={"mintable": False, "honeypot": None, "renounced": True, "missing": []})
    lines = hs.diff(a, b)
    assert "Top-10 share 41.00% → 23.00%" in lines
    assert "LP locked 0.00% → 95.00%" in lines and "LP unlocked 100.00% → 5.00%" in lines
    assert any(line.startswith("Price $1e-05 → $2e-05 (+100.0%)") for line in lines)
    assert any("1 wallet(s) left the top 20" in line and "AAAA…AAAA" in line for line in lines)
    assert any("1 wallet(s) entered the top 20" in line for line in lines)
    assert "Ownership renounced: False → True" in lines
    assert hs.diff(a, _row(t, top10_pct=41.3, price_usd=1.02e-05)) == []


def test_history_card_needs_two_rows_and_then_tells_the_move():
    s = _subject()
    assert asyncio.run(hs.history_card(s)) is None
    t0 = datetime.now(timezone.utc) - timedelta(hours=12)
    asyncio.run(hs.store(s.key, _row(t0, id="a")))
    assert asyncio.run(hs.history_card(s)) is None          # one row is the present, not history
    asyncio.run(hs.store(s.key, _row(t0 + timedelta(hours=6), id="b", top10_pct=30.0)))
    asyncio.run(hs.store(s.key, _row(t0 + timedelta(hours=12), id="c", top10_pct=23.0, lp_locked_pct=95.0, lp_unlocked_pct=5.0)))
    card = asyncio.run(hs.history_card(s))
    assert "Holder history — BONK" in card and "**Rows**: 3" in card and "**Span**: 12 h" in card
    assert "Top-10 share 41.00% → 23.00%" in card and "LP locked 0.00% → 95.00%" in card
    assert "Latest step (6 h)" in card and "Top-10 share 30.00% → 23.00%" in card
    assert "nothing before the first row is known" in card


def test_deep_dive_gains_a_history_dimension_only_when_the_ledger_has_history(monkeypatch):
    from types import SimpleNamespace

    class _Router:
        def try_route(self, *a, **k):
            return None
        def try_route_across(self, *a, **k):
            return None
    monkeypatch.setattr(token_deepdive, "get_provider_router", lambda: _Router())
    monkeypatch.setattr(settings, "holder_snapshots_enabled", True)
    monkeypatch.setattr(settings, "mobula_api_key", "test")
    bundle = asyncio.run(token_deepdive.build_token_evidence(_BONK, "solana", "BONK"))
    assert "history" not in {d.name for d in bundle.dimensions}
    s = _subject()
    t0 = datetime.now(timezone.utc) - timedelta(hours=2)
    asyncio.run(hs.store(s.key, _row(t0, id="a")))
    asyncio.run(hs.store(s.key, _row(t0 + timedelta(hours=2), id="b", top10_pct=20.0)))
    bundle = asyncio.run(token_deepdive.build_token_evidence(_BONK, "solana", "BONK"))
    history = next(d for d in bundle.dimensions if d.name == "history")
    assert history.status == "available" and history.source == "holder_snapshots" and "41.00% → 20.00%" in history.detail


def test_discovery_skips_launches_nobody_holds_yet(monkeypatch):
    monkeypatch.setattr(settings, "holder_snapshot_pulse_chains", "solana")
    monkeypatch.setattr(mobula_client, "get", lambda v, p, params, timeout=None: {"new": {"data": [_pulse_item(_NEW, "BUTTCOIN", holders_count=3)]}})
    assert asyncio.run(hs.discover_launches()) == 0 and asyncio.run(hs.tracked()) == []


def test_two_dark_rows_stop_tracking(monkeypatch):
    def nothing(version, path, params, timeout=None):
        return [] if path == "/token/holder-positions" else {}
    monkeypatch.setattr(mobula_client, "get", nothing)
    monkeypatch.setattr(settings, "holder_snapshot_interval_minutes", 0)
    monkeypatch.setattr(settings, "holder_snapshot_fresh_minutes", 0)
    s = _subject(_NEW, "DEAD")
    asyncio.run(hs.track(s, "deep_dive", days=14))
    assert asyncio.run(hs.tick()) == 1 and len(asyncio.run(hs.tracked())) == 1   # one dark row: still tracked
    assert asyncio.run(hs.tick()) == 1 and asyncio.run(hs.tracked()) == []       # second dark row: stopped
    pulse_row = hs.row_from_pulse(_pulse_item(_NEW, "X"))
    assert not hs.dark(pulse_row)


def test_fresh_token_concentration_comes_from_positions_without_the_pool(monkeypatch):
    holders = [{"walletAddress": "P" * 44, "percentageOfTotalSupply": 52.4, "labels": ["liquidityPool"]},
               {"walletAddress": "A" * 44, "percentageOfTotalSupply": 9.0, "labels": []},
               {"walletAddress": "B" * 44, "percentageOfTotalSupply": 4.0, "labels": ["sniper"]}]
    monkeypatch.setattr(mobula_client, "get", _live_mobula(holders=holders, security={"top10HoldingsPercentage": 0.0}))
    row = hs.take("solana", _NEW)
    assert row["top10_pct"] == 13.0                       # security said 0 (not computed); the pool wallet is not a holder
    assert row["top_holders"][0]["address"] == "P" * 44   # but the list still shows it, labelled


def test_pulse_rows_never_claim_zero_concentration_or_a_one_sided_liquidity():
    row = hs.row_from_pulse(_pulse_item(_NEW, "X", top10HoldingsPercentage=0.0))
    assert row["top10_pct"] is None and row["liquidity_usd"] is None and row["market_cap_usd"] == 12000.0


def test_discovery_records_the_pulse_row_once_per_token(monkeypatch):
    monkeypatch.setattr(settings, "holder_snapshot_pulse_chains", "solana")
    monkeypatch.setattr(mobula_client, "get", lambda v, p, params, timeout=None: {"new": {"data": [_pulse_item(_NEW, "BUTTCOIN")]}})
    asyncio.run(hs.discover_launches())
    asyncio.run(hs.discover_launches())
    assert len(asyncio.run(hs.history(f"solana:{_NEW.lower()}"))) == 1


def test_diff_does_not_read_a_missing_holder_list_as_wallets_leaving():
    t = datetime.now(timezone.utc)
    pulse = _row(t, top_holders=[], flags={"source": "pulse", "missing": ["top_holders"]})
    later = _row(t, top10_pct=41.0)
    assert not any("left the top" in line or "entered the top" in line for line in hs.diff(pulse, later))


def test_launch_symbol_belongs_to_the_chosen_side():
    item = {"tokenSymbol": "HOMO", "pair": {"baseToken": "token0", "token0": {"address": "G" * 44, "symbol": "GRND"}, "token1": {"address": "H" * 44, "symbol": "HOMO"}}}
    address = hs.launch_address(item)
    assert address == "G" * 44 and hs.launch_symbol(item, address) == "GRND"
    assert hs.launch_symbol({"tokenSymbol": "X", "pair": {}}, "Q" * 44) == "X"
