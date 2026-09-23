"""The structure benchmark (scripts/structure_benchmark.py, app/structure_rules.py):
labels from the ledger use only what was knowable at decision time; the
rules arm names its reasons and its unknowns; the scorer counts recall,
false alerts and misses."""
import sys
from pathlib import Path

from app.structure_rules import assess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from structure_benchmark import build_cases, score  # noqa: E402


def _row(t, **f):
    day, hour = divmod(t, 24)
    return {"subject_key": "token:solana:X", "taken_at": f"2026-09-{21 + day:02d}T{hour:02d}:00:00+00:00", "top10_pct": None, "liquidity_usd": None, "price_usd": None, **f}


def test_rules_name_reasons_and_report_missing_fields_as_unknown():
    out = assess({"lp_unlocked_pct": 80, "top10_pct": 65, "bundler_pct": 2, "sniper_pct": 3, "insider_pct": 1, "dev_pct": 0, "liquidity_usd": 50_000})
    assert out["flag"] and out["score"] == 4 and "more than half the liquidity can be pulled" in out["reasons"] and out["unknown"] == []
    quiet = assess({"lp_unlocked_pct": 0, "top10_pct": 20, "bundler_pct": 2, "sniper_pct": 3, "insider_pct": 1, "dev_pct": 0, "liquidity_usd": 50_000})
    assert not quiet["flag"] and quiet["reasons"] == []
    blind = assess({"top10_pct": 65})
    assert not blind["flag"] and set(blind["unknown"]) >= {"lp_unlocked_pct", "bundler_pct", "liquidity_usd"}     # unknown is never clean


def test_labels_come_from_the_window_after_the_decision_snapshot_only():
    rows = {"token:solana:X": [_row(10, top10_pct=30, liquidity_usd=100_000, price_usd=1.0), _row(12, top10_pct=35, liquidity_usd=90_000, price_usd=0.9),
                               _row(14, top10_pct=45, liquidity_usd=20_000, price_usd=0.2), _row(16, top10_pct=50, liquidity_usd=10_000, price_usd=0.1),
                               _row(40, top10_pct=90, liquidity_usd=0, price_usd=0.0)],          # outside a 24h horizon
            "token:solana:Y": [_row(10, top10_pct=20, liquidity_usd=50_000, price_usd=2.0), _row(12, top10_pct=21, liquidity_usd=52_000, price_usd=2.1),
                               _row(14, top10_pct=22, liquidity_usd=51_000, price_usd=2.0), _row(16, top10_pct=22, liquidity_usd=55_000, price_usd=2.2), _row(18, top10_pct=23, liquidity_usd=55_000, price_usd=2.3)],
            "token:solana:Z": [_row(10, top10_pct=20), _row(12, top10_pct=21)]}
    cases = {c["id"]: c for c in build_cases(rows, min_snapshots=5, horizon_hours=24)}
    assert set(cases) == {"token:solana:X", "token:solana:Y"}                           # Z has too few snapshots
    x = cases["token:solana:X"]
    assert x["outcome"] == {"concentration_up": True, "liquidity_collapse": True, "price_collapse": True} and x["bad"] and not x["clean"]
    assert x["snapshots_in_window"] == 4 and x["evidence"]["top10_pct"] == 30              # the decision-time snapshot, nothing later
    y = cases["token:solana:Y"]
    assert y["outcome"] == {"concentration_up": False, "liquidity_collapse": False, "price_collapse": False} and y["clean"] and not y["bad"]


def test_the_scorer_counts_recall_false_alerts_and_names_the_misses():
    results = [{"id": "a", "bad": True, "clean": False, "flag": True, "latency_ms": 1, "model_calls": 0},
               {"id": "b", "bad": True, "clean": False, "flag": False, "latency_ms": 1, "model_calls": 0},
               {"id": "c", "bad": False, "clean": True, "flag": True, "latency_ms": 1, "model_calls": 0},
               {"id": "d", "bad": False, "clean": True, "flag": False, "latency_ms": 1, "model_calls": 0}]
    text = score(results, "rules")
    assert "recall on bad outcomes   50%" in text and "false alerts on clean    50%" in text and "missed: b" in text
