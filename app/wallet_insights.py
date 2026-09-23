"""Deterministic wallet health and portfolio scenario calculations."""

from __future__ import annotations


def wallet_health(snapshot: dict) -> dict:
    findings: list[dict] = []
    sol = snapshot.get("sol") or {}
    if float(sol.get("amount") or 0) < 0.005:
        findings.append({"severity": "warning", "title": "Low Solana gas", "detail": "Keep at least 0.005 SOL available for transaction fees."})
    holdings = snapshot.get("holdings") or []
    unverified = [item for item in holdings if not item.get("verified")]
    if unverified:
        findings.append({"severity": "warning", "title": "Unverified assets", "detail": f"{len(unverified)} holding(s) are not verified by the pricing source."})
    concentrated = [item for item in holdings if float(item.get("allocation_pct") or 0) >= 80]
    if concentrated:
        symbols = ", ".join(item.get("symbol") or "UNKNOWN" for item in concentrated[:3])
        findings.append({"severity": "info", "title": "Concentrated portfolio",
                         "detail": f"At least 80% of priced value is in {symbols}. Concentration is a fact, not a verdict: whether it is a risk depends on the asset "
                                   "and on exit depth, which this check does not measure (say `exit analysis for X` for a live quote)."})
    if snapshot.get("unpriced_holdings"):
        findings.append({"severity": "warning", "title": "Unpriced holdings", "detail": f"{snapshot['unpriced_holdings']} holding(s) could not be valued and are excluded from allocation calculations."})
    return {
        "wallet": snapshot.get("wallet"),
        "status": "attention" if any(item["severity"] == "warning" for item in findings) else "healthy",
        "findings": findings,
        "scope": "Solana balances, pricing, verification, concentration, and gas readiness. Liquidity, exit depth and program approvals are not measured; \"healthy\" means no warning fired, not that holdings are safe or liquid.",
    }


def portfolio_scenario(snapshot: dict, change_pct: float, symbol: str | None = None) -> dict:
    """Apply a simple price shock while keeping quantities constant."""
    if change_pct < -100 or change_pct > 1000:
        raise ValueError("Scenario change must be between -100% and 1000%")
    target = symbol.upper() if symbol else None
    positions = [{"symbol": "SOL", **(snapshot.get("sol") or {})}, *(snapshot.get("holdings") or [])]
    rows = []
    current_total = projected_total = 0.0
    for position in positions:
        current = position.get("usd_value")
        if current is None:
            continue
        current = float(current)
        applies = target is None or str(position.get("symbol") or "").upper() == target
        projected = current * (1 + change_pct / 100) if applies else current
        current_total += current
        projected_total += projected
        rows.append({
            "symbol": position.get("symbol") or "UNKNOWN", "current_usd": round(current, 2),
            "projected_usd": round(projected, 2), "applied_change_pct": change_pct if applies else 0,
        })
    return {
        "wallet": snapshot.get("wallet"), "target": target or "ALL_PRICED_ASSETS",
        "change_pct": change_pct, "current_total_usd": round(current_total, 2),
        "projected_total_usd": round(projected_total, 2),
        "portfolio_change_usd": round(projected_total - current_total, 2), "positions": rows,
        "assumptions": "Token quantities, liquidity, fees, yield, and correlations remain unchanged.",
    }
