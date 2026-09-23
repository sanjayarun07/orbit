"""Deterministic risk rules over a token's recorded structure.

The first arm of the structure benchmark (scripts/structure_benchmark.py):
no model, no network, one function from a holder-ledger snapshot to a risk
verdict with the reasons that produced it. It exists to be measured
against a grounded analyst on the same saved evidence, not to be trusted
on its own. Every rule names the field it read; a field the snapshot did
not carry is reported as unknown, never as clean.
"""
from __future__ import annotations

# (field, threshold, points, reason) -- a rule fires when the field is at
# or past the threshold. Shares are percent of supply.
RULES: tuple[tuple[str, float, int, str], ...] = (
    ("lp_unlocked_pct", 50.0, 2, "more than half the liquidity can be pulled"),
    ("top10_pct", 40.0, 1, "top ten wallets hold over 40% of supply"),
    ("top10_pct", 60.0, 1, "top ten wallets hold over 60% of supply"),
    ("bundler_pct", 15.0, 1, "bundled wallets hold over 15%"),
    ("sniper_pct", 15.0, 1, "snipers hold over 15%"),
    ("insider_pct", 10.0, 1, "insiders hold over 10%"),
    ("dev_pct", 10.0, 1, "the deployer holds over 10%"),
)
FLAG_AT = 3
FIELDS = tuple(dict.fromkeys(field for field, *_ in RULES))


def assess(snapshot: dict) -> dict:
    """{"score", "flag", "reasons", "unknown"} from one ledger snapshot."""
    score, reasons, unknown = 0, [], []
    for field, threshold, points, reason in RULES:
        value = snapshot.get(field)
        if value is None:
            if field not in unknown:
                unknown.append(field)
            continue
        if float(value) >= threshold:
            score += points
            reasons.append(reason)
    liquidity = snapshot.get("liquidity_usd")
    if liquidity is not None and float(liquidity) < 10_000:
        score += 1
        reasons.append("liquidity under $10k")
    elif liquidity is None:
        unknown.append("liquidity_usd")
    return {"score": score, "flag": score >= FLAG_AT, "reasons": reasons, "unknown": unknown}
