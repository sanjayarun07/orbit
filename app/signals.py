"""Typed analyst views and the pure arithmetic that turns them into a book.

Adopted from the ai-hedge-fund v2 design after the 2026-09-20 discussion, asset
class agnostic on purpose: a meme token on Solana, a US equity and a wallet
are all a `Subject`, and every analyst -- a quant model over Mobula data, a
persona reasoning over a fundamentals snapshot, the deep-dive lens -- speaks
the same `Signal`. Everything downstream (blend, clamp, orders, receipts) then
never needs to know which kind of analyst spoke.

Three rules carried over verbatim, because each closes a way to be wrong:

- An ABSTAIN is not a NEUTRAL. "I could not form a view" (no data, model
  failed, answer unparseable) is excluded from the blend entirely; a real
  neutral vote (a bundle check that found nothing) dilutes. No opinion must
  never masquerade as opinion-neutral.
- The model's influence ends at the Signal. Sizing and limits are deterministic
  arithmetic here; no clamp is negotiable and freed exposure stays in cash.
- Pure functions. Given the same signals the same book comes out, so a replay
  is exact and a receipt can be checked by recomputation.

Nothing in this module does I/O.
"""
from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

SubjectKind = Literal["token", "equity", "wallet"]

# Confidence words the analysts use, folded into a magnitude. A persona's
# 0-100 confidence divides by 100 instead; both land in the same [-1, +1].
CONFIDENCE_MAGNITUDE = {"high": 0.9, "medium": 0.6, "low": 0.3}
STANCE_SIGN = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}


class Subject(BaseModel):
    """What a view is about. A token is (chain, address); an equity is a
    ticker; a wallet is (chain, address). `symbol` is display only."""

    model_config = ConfigDict(frozen=True)

    kind: SubjectKind
    id: str
    chain: str | None = None
    symbol: str | None = None

    @field_validator("id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("a subject needs an id")
        return value.strip()

    @property
    def key(self) -> str:
        """Stable identity across records: chain-scoped for on-chain subjects,
        upper-case ticker for equities."""
        if self.kind == "equity":
            return f"equity:{self.id.upper()}"
        return f"{(self.chain or 'unknown').lower()}:{self.id.lower()}"

    def label(self) -> str:
        if self.symbol:
            return self.symbol.upper()
        return self.id if self.kind == "equity" else f"{self.id[:4]}…{self.id[-4:]}"


class Signal(BaseModel):
    """One analyst's view of one subject at one moment.

    `value` is conviction from -1 (bearish) to +1 (bullish). `components` is a
    quant model's decomposition (what fed the number); `metadata` carries the
    provenance -- and `abstained`, which is the flag that keeps a non-view out
    of every blend."""

    model_name: str
    subject: Subject
    as_of: str = Field(description="ISO-8601 moment the view was formed")
    value: float
    reasoning: str | None = None
    components: dict[str, float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("value")
    @classmethod
    def _bounded(cls, value: float) -> float:
        if value != value or not -1.0 <= value <= 1.0:  # NaN fails the first test
            raise ValueError("conviction must be a number in [-1, 1]")
        return float(value)

    @property
    def abstained(self) -> bool:
        return self.metadata.get("abstained") is True

    @classmethod
    def abstain(cls, model_name: str, subject: Subject, as_of: str, reason: str) -> "Signal":
        """No view. Excluded from blends; the reason is kept for the receipt."""
        return cls(model_name=model_name, subject=subject, as_of=as_of, value=0.0,
                   reasoning=f"abstained: {reason}",
                   metadata={"abstained": True, "abstain_reason": reason})

    @classmethod
    def from_stance(cls, model_name: str, subject: Subject, as_of: str, stance: str, confidence: str | float,
                    reasoning: str | None = None, **metadata: Any) -> "Signal":
        """Fold a (bullish|bearish|neutral, high|medium|low or 0-100) pair into a
        conviction. An unrecognised stance or confidence abstains rather than
        guessing a direction."""
        sign = STANCE_SIGN.get(str(stance).strip().lower())
        if sign is None:
            return cls.abstain(model_name, subject, as_of, f"stance not parsed: {stance!r}")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            if not 0 <= float(confidence) <= 100:
                return cls.abstain(model_name, subject, as_of, f"confidence out of range: {confidence!r}")
            magnitude = float(confidence) / 100.0
        else:
            magnitude = CONFIDENCE_MAGNITUDE.get(str(confidence).strip().lower())
            if magnitude is None:
                return cls.abstain(model_name, subject, as_of, f"confidence not parsed: {confidence!r}")
        return cls(model_name=model_name, subject=subject, as_of=as_of, value=sign * magnitude, reasoning=reasoning,
                   metadata={"abstained": False, "stance": str(stance).lower(), "confidence": confidence, **metadata})


class SubjectSkip(BaseModel):
    """Something a decision was asked to consider and could not, and why. A
    skip is shown, never silently dropped: a missing dimension changes how much
    a verdict can be trusted."""

    subject: str
    reason: str


@runtime_checkable
class AlphaModel(Protocol):
    """Every analyst implements this and plugs into the same blend.

    `data` is whatever the model reads through: the live Mobula door today, a
    point-in-time reader in a backtest. The model never reaches past it, so
    the same model is honest in both modes."""

    @property
    def name(self) -> str: ...

    def predict(self, subject: Subject, as_of: str, data: Any) -> Signal: ...


# ---------------------------------------------------------------------------
# Blend: views -> target weights
# ---------------------------------------------------------------------------

class BlendResult(BaseModel):
    convictions: dict[str, float]   # per subject key, blended view, pre-scaling
    weights: dict[str, float]       # per subject key; sum(|w|) <= gross_target


def blend_signals(signals: list[Signal], model_weights: dict[str, float] | None = None,
                  gross_target: float = 1.0, market_neutral: bool = False) -> BlendResult:
    """Per subject, the weighted mean over VOTING models; abstains are out of
    numerator and denominator. Cross-sectionally, weights are the (optionally
    demeaned) convictions normalised so the book deploys `gross_target`.

    Market-neutral demeans before scaling: long what the desk likes most
    relative to the rest, short the least liked, sleeve sums to zero. Uniform
    convictions demean to a flat book. A model missing from `model_weights`
    votes with weight 1."""
    model_weights = model_weights or {}
    weighted_sum: dict[str, float] = {}
    weight_total: dict[str, float] = {}
    for signal in signals:
        if signal.abstained:
            continue
        w = float(model_weights.get(signal.model_name, 1.0))
        if w <= 0:
            continue
        key = signal.subject.key
        weighted_sum[key] = weighted_sum.get(key, 0.0) + w * signal.value
        weight_total[key] = weight_total.get(key, 0.0) + w

    keys = sorted({s.subject.key for s in signals})
    convictions = {k: (weighted_sum[k] / weight_total[k]) if weight_total.get(k) else 0.0 for k in keys}

    scaled = convictions
    if market_neutral and keys:
        mean = sum(convictions.values()) / len(convictions)
        scaled = {k: c - mean for k, c in convictions.items()}

    # Threshold, not == 0: demeaning identical convictions leaves ~1e-16 of
    # residue, and dividing by it would normalise noise into a full book.
    gross = sum(abs(c) for c in scaled.values())
    if gross < 1e-9:
        weights = {k: 0.0 for k in keys}
    else:
        weights = {k: c / gross * gross_target for k, c in scaled.items()}
    return BlendResult(convictions=convictions, weights=weights)


# ---------------------------------------------------------------------------
# Risk: hard limits the analysts cannot override
# ---------------------------------------------------------------------------

class RiskLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_position_pct: float = Field(gt=0, le=1.0, description="max |weight| per subject, fraction of equity")
    max_gross_exposure: float = Field(gt=0, description="max sum of |weights|; 1.0 = unlevered")


class ClampEvent(BaseModel):
    """One limit firing, kept so every clamp is explainable on the receipt."""

    limit: Literal["max_position_pct", "max_gross_exposure"]
    subject: str | None = None      # None for the book-level gross clamp
    before: float
    after: float


class RiskResult(BaseModel):
    weights: dict[str, float]
    clamps: list[ClampEvent]


def apply_limits(weights: dict[str, float], limits: RiskLimits) -> RiskResult:
    """Per-subject cap first (sign preserved), then a proportional gross
    scale-down. Scaling only shrinks, so it can never re-violate the cap, and
    the pair is idempotent. Exposure a clamp removes stays in cash."""
    clamped: dict[str, float] = {}
    clamps: list[ClampEvent] = []
    cap = limits.max_position_pct
    for key in sorted(weights):
        w = weights[key]
        if abs(w) > cap:
            new_w = cap if w > 0 else -cap
            clamps.append(ClampEvent(limit="max_position_pct", subject=key, before=w, after=new_w))
            clamped[key] = new_w
        else:
            clamped[key] = w
    gross = sum(abs(w) for w in clamped.values())
    if gross > limits.max_gross_exposure:
        scale = limits.max_gross_exposure / gross
        clamped = {k: w * scale for k, w in clamped.items()}
        clamps.append(ClampEvent(limit="max_gross_exposure", before=gross, after=limits.max_gross_exposure))
    return RiskResult(weights=clamped, clamps=clamps)


def limits_from_charter(fields: dict | None, *, default_gross: float = 1.0) -> RiskLimits | None:
    """The user's risk charter as hard limits. The charter states
    `max_position_pct` in PERCENT (0-100, app/models.py); limits are a
    fraction. None when the charter sets no position cap -- the desk is then
    advisory, and nothing here should pretend to a limit the user never set."""
    if not fields:
        return None
    pct = fields.get("max_position_pct")
    try:
        pct = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        return None
    if pct is None or not 0 < pct <= 100:
        return None
    return RiskLimits(max_position_pct=pct / 100.0, max_gross_exposure=default_gross)
