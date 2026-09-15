"""Post-answer validation (the pipeline's step-7 validator): before an answer is
surfaced, check its **provenance** (does the reported data trace to a source),
**freshness** (is time-sensitive data timestamped and recent), and **grounding**
(do the answer's quantitative claims appear in the tool evidence, or were they
synthesized without support).

This is advisory, not a gate: it produces a structured `AnswerValidation` the
client can surface and monitoring can alert on. It never rewrites the answer and
never blocks it -- the hard safety gates (simulation, caps, CONFIRM) live on the
trade path and are unaffected. It complements `build_evidence_summary` (which
reports tool success/failure) by validating the answer *content* against that
evidence.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from app.models import AnswerValidation, ValidationCheck
from app.settings import settings

# Intents whose answers carry factual/on-chain data worth validating. General
# chit-chat and pure explanations have nothing to check.
_DATA_INTENTS = {"research", "portfolio", "trade", "cross_chain_swap", "team"}

_TIME_SENSITIVE = re.compile(
    r"\b(price|now|current|currently|latest|today|live|24h|funding|trending|"
    r"gainers?|losers?|volume|market\s*cap|holders?|balance|apy|apr|tvl)\b",
    re.IGNORECASE,
)

# Timestamps the providers stamp into their own output, e.g.
# "**Data freshness**: 2026-09-14 11:40 UTC" / "**Checked**: 2026-09-14 12:04 UTC".
_STAMP = re.compile(
    r"(?:data\s+freshness|checked|as of|as-of)\**\s*[:\-]?\s*"
    r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)",
    re.IGNORECASE,
)

# Salient quantitative claims in an answer: $ amounts (with k/m/b/t or %), bare
# percentages, and large grouped numbers. Small integers ("top 5", "3 pools")
# are deliberately excluded -- they are structure, not data claims.
_MONEY = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?\s?[kKmMbBtT]?\b")
_PERCENT = re.compile(r"\d[\d,]*(?:\.\d+)?\s?%")
_BIG_NUMBER = re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b")


# Canonical headline metrics and the labels providers use for them. Only bold
# key-value headlines ("**Mark Price (USD)**: $77.1K") are read -- table columns
# and list rows are deliberately not, since those are many values, not one claim.
_METRIC_LABELS = {
    "price": ("mark price", "current price", "price"),
    "market_cap": ("market capitalization", "market cap", "marketcap", "mcap"),
    "liquidity": ("liquidity",),
    "volume_24h": ("24h volume", "volume 24h", "24h vol", "volume"),
    "tvl": ("total value locked", "tvl"),
    "fdv": ("fully diluted valuation", "fully diluted", "fdv"),
    "holders": ("holder count", "holders"),
}
_HEADLINE = re.compile(r"\*\*([^*]+?)\*\*\s*[:\-]?\s*([^\n·|]+)")
_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}
_EVM = r"0x[0-9a-fA-F]{40}"
_SOL = r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])"
_ADDRESS = re.compile(rf"{_EVM}|{_SOL}")


def _parse_magnitude(value: str) -> float | None:
    match = re.search(r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([kmbtKMBT])?", value)
    if not match or "%" in value:  # percentages are ratios, not comparable magnitudes
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return number * _SUFFIX.get((match.group(2) or "").lower(), 1.0)


def _canonical_metric(label: str) -> str | None:
    cleaned = re.sub(r"\(.*?\)", "", label).strip().lower()
    for canonical, aliases in _METRIC_LABELS.items():
        if any(cleaned == alias or cleaned.startswith(alias + " ") for alias in aliases):
            return canonical
    return None


def _headline_metrics(text: str) -> dict[str, float]:
    """The first headline value for each canonical metric in one observation."""
    metrics: dict[str, float] = {}
    for label, value in _HEADLINE.findall(text):
        canonical = _canonical_metric(label)
        if canonical and canonical not in metrics:
            magnitude = _parse_magnitude(value)
            if magnitude is not None:
                metrics[canonical] = magnitude
    return metrics


def _tool_names(trajectory: dict) -> list[str]:
    """Descends into nested trajectory dicts, same as _observation_sources."""
    names: list[str] = []
    for key, value in trajectory.items():
        if key.startswith("tool_name_") and isinstance(value, str):
            names.append(value)
        elif isinstance(value, dict):
            names.extend(_tool_names(value))
    return names


def _providers_from_trajectory(trajectory: dict) -> list[str]:
    names: list[str] = []
    for value in _tool_names(trajectory):
        if value == "semantic_cache":
            provider = "cache"
        elif value.startswith("mcp_"):
            provider = value.split("_", 2)[1]
        else:
            provider = value.split("_", 1)[0]
        if provider and provider not in names:
            names.append(provider)
    return names


def _cited_sources(answer: str) -> list[str]:
    cited: list[str] = []
    for match in re.finditer(r"\*\*Provider\*\*\s*[:\-]?\s*([A-Za-z0-9 .()/_-]+?)(?:\s*·|\n|$)", answer):
        name = match.group(1).strip()
        if name and name not in cited:
            cited.append(name)
    for match in re.finditer(r"Source[s]?\s*[:\-]?\s*\[([^\]]+)\]", answer):
        name = match.group(1).strip()
        if name and name not in cited:
            cited.append(name)
    return cited


def _observations_text(trajectory: dict) -> str:
    return "\n".join(text for _, text in _observation_sources(trajectory))


def _digit_signature(token: str) -> str:
    """The significant-digit run of a number, so '$59.48B' in an answer matches
    '59.48' or '59,480' in an observation without exact-format coupling."""
    digits = re.sub(r"[^\d]", "", token)
    return digits.lstrip("0") or ("0" if digits else "")


def _claims(answer: str) -> list[str]:
    seen: list[str] = []
    for pattern in (_MONEY, _PERCENT, _BIG_NUMBER):
        for match in pattern.findall(answer):
            sig = _digit_signature(match)
            if len(sig) >= 2 and sig not in seen:  # ignore 1-digit noise
                seen.append(sig)
    return seen


def _provenance_check(answer: str, sources: list[str], has_claims: bool) -> ValidationCheck:
    if sources:
        return ValidationCheck(name="provenance", status="ok", detail=f"Attributed to: {', '.join(sources[:6])}")
    if has_claims:
        return ValidationCheck(
            name="provenance", status="warn",
            detail="Reports figures with no provider attribution or cited source.",
        )
    return ValidationCheck(name="provenance", status="not_applicable", detail="No data claims to attribute.")


def _freshness_check(request: str, answer: str) -> tuple[ValidationCheck, str | None, float | None]:
    stamps: list[datetime] = []
    for raw in _STAMP.findall(answer):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                stamps.append(datetime.strptime(raw.strip(), fmt).replace(tzinfo=timezone.utc))
                break
            except ValueError:
                continue
    time_sensitive = bool(_TIME_SENSITIVE.search(request))
    if not stamps:
        if time_sensitive:
            return (
                ValidationCheck(
                    name="freshness", status="warn",
                    detail="Time-sensitive request but the answer carries no data timestamp.",
                ),
                None, None,
            )
        return ValidationCheck(name="freshness", status="not_applicable", detail="No time-sensitive data."), None, None
    as_of = max(stamps)
    age_minutes = max(0.0, (datetime.now(timezone.utc) - as_of).total_seconds() / 60.0)
    as_of_text = as_of.strftime("%Y-%m-%d %H:%M UTC")
    if age_minutes > settings.answer_freshness_warn_minutes:
        return (
            ValidationCheck(
                name="freshness", status="warn",
                detail=f"Data is ~{age_minutes:.0f} min old (as of {as_of_text}).",
            ),
            as_of_text, age_minutes,
        )
    return (
        ValidationCheck(name="freshness", status="ok", detail=f"Fresh as of {as_of_text}."),
        as_of_text, age_minutes,
    )


def _grounding_check(answer: str, trajectory: dict, has_trajectory: bool) -> ValidationCheck:
    claims = _claims(answer)
    if not claims:
        return ValidationCheck(name="grounding", status="not_applicable", detail="No quantitative claims to ground.")
    if not has_trajectory:
        return ValidationCheck(
            name="grounding", status="warn",
            detail="Quantitative claims with no tool evidence behind them.",
        )
    evidence = re.sub(r"[^\d]", "", _observations_text(trajectory))
    ungrounded = [sig for sig in claims if sig not in evidence]
    # Conservative: only warn when most claims are unsupported, to avoid false
    # alarms from formatting/rounding differences on a mostly-grounded answer.
    if len(ungrounded) >= max(2, (len(claims) + 1) // 2):
        return ValidationCheck(
            name="grounding", status="warn",
            detail=f"{len(ungrounded)} of {len(claims)} figures don't trace to the retrieved data.",
        )
    return ValidationCheck(name="grounding", status="ok", detail=f"{len(claims) - len(ungrounded)}/{len(claims)} figures trace to tool data.")


def _observation_sources(trajectory: dict) -> list[tuple[str, str]]:
    """(provider, observation_text) per tool call, descending into nested
    trajectory dicts (the team desk nests as {"market_research": {...}}), so a
    multi-source answer is seen wherever its sub-trajectories live."""
    pairs: list[tuple[str, str]] = []
    for key, value in trajectory.items():
        if key.startswith("observation_") and isinstance(value, str):
            index = key.rsplit("_", 1)[-1]
            tool = trajectory.get(f"tool_name_{index}")
            if not isinstance(tool, str):
                provider = "?"
            elif tool.startswith("mcp_"):
                provider = tool.split("_", 2)[1]
            else:
                provider = tool.split("_", 1)[0]
            pairs.append((provider, value))
        elif isinstance(value, dict):
            pairs.extend(_observation_sources(value))
    return pairs


def _consistency_check(trajectory: dict) -> ValidationCheck:
    """Compare the SAME headline metric across DIFFERENT providers, but only when
    they provably concern the same token (a shared contract/mint address), so we
    never compare unrelated tokens' figures. Warns on a material disagreement."""
    per_source: list[tuple[str, dict[str, float]]] = []
    addresses: set[str] = set()
    for provider, text in _observation_sources(trajectory):
        metrics = _headline_metrics(text)
        if metrics:
            per_source.append((provider, metrics))
            addresses |= set(_ADDRESS.findall(text))
    if len(per_source) < 2:
        return ValidationCheck(name="consistency", status="not_applicable", detail="Fewer than two sources with comparable metrics.")
    # Same-subject gate: all comparable observations must point at one token.
    # Missing addresses (can't confirm the subject) is treated as not-comparable
    # rather than risk a cross-token false positive.
    if len(addresses) != 1:
        return ValidationCheck(name="consistency", status="not_applicable", detail="Sources don't share a single confirmed token subject.")
    tolerance = settings.answer_consistency_tolerance_pct / 100.0
    compared = 0
    disagreements: list[str] = []
    for metric in _METRIC_LABELS:
        values = [(p, m[metric]) for p, m in per_source if metric in m]
        if len({p for p, _ in values}) < 2:
            continue
        compared += 1
        magnitudes = [v for _, v in values]
        lo, hi = min(magnitudes), max(magnitudes)
        if hi > 0 and (hi - lo) / hi > tolerance:
            spread = (hi - lo) / hi * 100
            disagreements.append(f"{metric.replace('_', ' ')} differs {spread:.0f}% across sources")
    if disagreements:
        return ValidationCheck(name="consistency", status="warn", detail="; ".join(disagreements))
    if compared:
        return ValidationCheck(name="consistency", status="ok", detail=f"{compared} metric(s) agree across sources.")
    return ValidationCheck(name="consistency", status="not_applicable", detail="No metric reported by two or more sources.")


def validate_answer(
    request: str,
    answer: str,
    trajectory: object,
    intent: str | None,
) -> AnswerValidation | None:
    """Validate a surfaced answer. Returns None when there is nothing to validate
    (a non-data intent with no evidence) so callers can skip attaching it."""
    if not answer:
        return None
    traj = trajectory if isinstance(trajectory, dict) else {}
    has_trajectory = bool(_observation_sources(traj))
    if intent not in _DATA_INTENTS and not has_trajectory:
        return None

    sources = _providers_from_trajectory(traj) + [
        s for s in _cited_sources(answer) if s.lower() not in {p.lower() for p in _providers_from_trajectory(traj)}
    ]
    has_claims = bool(_claims(answer))

    provenance = _provenance_check(answer, sources, has_claims)
    freshness, as_of, age = _freshness_check(request, answer)
    grounding = _grounding_check(answer, traj, has_trajectory)
    consistency = _consistency_check(traj)

    checks = [provenance, freshness, grounding, consistency]
    status = "warn" if any(c.status == "warn" for c in checks) else "ok"
    return AnswerValidation(status=status, checks=checks, sources=sources[:8], as_of=as_of, age_minutes=age)
