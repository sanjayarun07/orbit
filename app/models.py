from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


from pydantic import ConfigDict, field_validator

# Chains a charter may restrict trading to. Jupiter plans are Solana; Relay
# routes cover the rest.
TRADING_CHAINS = ("solana", "ethereum", "base", "arbitrum", "optimism", "polygon", "bnb", "avalanche", "robinhood")


class RiskCharterFields(BaseModel):
    """A structured risk charter. Every field except `notes` is enforced
    deterministically by charter_risk_node on the real quote; `notes` is the
    only part the Risk agent has to interpret."""

    model_config = ConfigDict(extra="forbid")
    max_trade_usd: float | None = Field(default=None, gt=0, le=1_000_000)
    max_position_pct: float | None = Field(default=None, gt=0, le=100)
    max_slippage_bps: int | None = Field(default=None, ge=1, le=500)
    verified_only: bool = False
    allowed_chains: list[str] = Field(default_factory=list, max_length=len(TRADING_CHAINS))
    notes: str | None = Field(default=None, max_length=300)

    @field_validator("allowed_chains")
    @classmethod
    def _known_chains(cls, chains: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(c.strip().lower() for c in chains if c and c.strip()))
        unknown = [c for c in cleaned if c not in TRADING_CHAINS]
        if unknown:
            raise ValueError(f"unknown chain(s): {', '.join(unknown)}")
        return cleaned

    @field_validator("notes")
    @classmethod
    def _clean_notes(cls, notes: str | None) -> str | None:
        notes = " ".join((notes or "").split())
        return notes or None

    def is_empty(self) -> bool:
        return not any((self.max_trade_usd, self.max_position_pct, self.max_slippage_bps,
                        self.verified_only, self.allowed_chains, self.notes))

    def render(self) -> str:
        parts = []
        if self.max_trade_usd is not None:
            parts.append(f"max ${self.max_trade_usd:,.2f} per trade")
        if self.max_position_pct is not None:
            parts.append(f"max {self.max_position_pct:g}% of portfolio per position")
        if self.max_slippage_bps is not None:
            parts.append(f"max {self.max_slippage_bps} bps slippage")
        if self.verified_only:
            parts.append("only Jupiter-verified tokens")
        if self.allowed_chains:
            parts.append("chains: " + ", ".join(self.allowed_chains))
        if self.notes:
            parts.append(f"also: {self.notes}")
        return "; ".join(parts)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    wallet_address: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    context_revision: int | None = Field(default=None, ge=0)
    quick_action: "QuickAction | None" = None
    # Explicit trading-desk switch from the UI; None leaves the session's
    # current setting (or a chat phrase like "enable team mode") in charge.
    team_mode: bool | None = None
    # Browser timezone (minutes east of UTC) so reminders read local times.
    tz_offset_min: int | None = Field(default=None, ge=-840, le=840)
    # The risk-charter card. When present the message is rewritten to the
    # canonical "set my risk charter: ..." so history shows exactly what was set.
    risk_charter_fields: RiskCharterFields | None = None


class EmailSigninStart(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class EmailSigninVerify(BaseModel):
    token: str = Field(min_length=8, max_length=128)


class EmailSigninCode(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class PreferencesUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=60)
    risk_profile: str | None = Field(default=None, max_length=32)
    default_wallet: str | None = Field(default=None, max_length=128)
    theme: str | None = Field(default=None, max_length=16)
    auto_scroll: bool | None = None
    # Notifications
    low_credit_alert: bool | None = None
    low_credit_threshold: int | None = Field(default=None, ge=0, le=100_000)
    receipts: bool | None = None
    product_updates: bool | None = None


class TeamInvite(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: Literal["member", "owner"] = "member"


class TeamAccept(BaseModel):
    owner_id: str = Field(max_length=64)


class DeleteAccountRequest(BaseModel):
    confirm_email: str = Field(min_length=3, max_length=254)


class AdminPlanUpdate(BaseModel):
    plan_id: str = Field(max_length=32)


class AdminCreditGrant(BaseModel):
    amount: int = Field(ge=-1_000_000, le=1_000_000)
    reason: str = Field(min_length=2, max_length=120)
    reference: str | None = Field(default=None, max_length=120)


class FeedbackRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    session_revision: int = Field(ge=0)
    rating: Literal["up", "down", "none"]
    comment: str | None = Field(default=None, max_length=500)


class TaskCreate(BaseModel):
    kind: Literal["reminder", "price_alert", "movers_alert", "brief"]
    spec: dict = Field(default_factory=dict)
    schedule: dict
    channel: Literal["inapp", "email"] = "inapp"
    tz_offset_min: int = Field(default=0, ge=-840, le=840)
    title: str | None = Field(default=None, max_length=140)


class TaskUpdate(BaseModel):
    status: Literal["active", "paused"] | None = None
    channel: Literal["inapp", "email"] | None = None


class InboxRead(BaseModel):
    ids: list[str] | None = None


class CheckoutRequest(BaseModel):
    kind: Literal["subscription", "pack"]
    item_id: str = Field(max_length=32)


class ApiKeyCreate(BaseModel):
    name: str = Field(default="API key", max_length=60)
    scopes: list[str] | None = None


class QuickAction(BaseModel):
    """Typed follow-up command; the label is never used as authoritative state."""

    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_.-]+$")
    prompt: str = Field(min_length=1, max_length=500)
    intent: Literal["general", "research", "portfolio", "trade", "cross_chain_swap"]
    capabilities: list[str] = Field(default_factory=list, max_length=8)
    chain: str | None = Field(default=None, max_length=40)
    entity_kind: Literal["token", "wallet", "transaction", "chain"] | None = None
    entity_label: str | None = Field(default=None, max_length=80)
    entity_address: str | None = Field(default=None, max_length=128)
    wallet_scope: Literal["connected"] | None = None
    context_revision: int = Field(default=0, ge=0)


class SwapProposal(BaseModel):
    input_mint: str
    output_mint: str
    amount_atomic: int = Field(gt=0)
    slippage_bps: int = Field(ge=1, le=500)
    reason: str


class TokenInfo(BaseModel):
    mint: str
    symbol: str
    name: str
    decimals: int
    usd_price: float | None = None
    verified: bool = False
    organic_score: float | None = None


class TradePlan(BaseModel):
    plan_id: str
    status: Literal["pending_confirmation", "submitting", "submitted", "executed", "expired", "rejected", "failed", "superseded", "submission_unknown"]
    submission_signature: str | None = None
    submitted_at: datetime | None = None
    reconciled_at: datetime | None = None
    created_at: datetime
    expires_at: datetime
    wallet_address: str
    proposal: SwapProposal
    quote: dict
    input_token: TokenInfo
    output_token: TokenInfo
    input_value_usd: float | None = None
    warnings: list[str] = Field(default_factory=list)
    simulation: dict
    confirmation_text: str
    # The account this plan was quoted for. confirmation_text is derived from
    # plan_id ("CONFIRM {plan_id}"), so without this the plan id alone is a
    # bearer token for every trade-plan endpoint. Enforced in app/main.py.
    owner_account_id: str | None = None


class ConfirmRequest(BaseModel):
    confirmation_text: str


class SignedTransactionRequest(ConfirmRequest):
    signed_transaction: str = Field(min_length=1, max_length=200_000)


class CrossChainSwapDraft(BaseModel):
    source_chain: str | None = None
    destination_chain: str | None = None
    amount: str | None = None
    input_token: str | None = None
    output_token: str | None = None
    recipient: str | None = None
    slippage_bps: int | None = Field(default=None, ge=0, le=10_000)


class ContextCapsule(BaseModel):
    """A compact, verified-enough entity reference carried across chat turns."""

    kind: Literal["token", "wallet", "transaction", "chain"]
    label: str
    address: str | None = None
    chain: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    source: str = "conversation"


class IntentLock(BaseModel):
    """Canonical action facts that quotes and signed payloads must preserve."""

    action: Literal["swap", "bridge"]
    source_chain: str
    destination_chain: str
    amount: str
    input_token: str
    output_token: str
    recipient: str | None = None
    max_slippage_bps: int | None = None
    fingerprint: str


class EvidenceSummary(BaseModel):
    generated_at: datetime
    providers: list[str] = Field(default_factory=list)
    successful_tools: int = 0
    failed_tools: int = 0
    cached: bool = False
    confidence: Literal["high", "medium", "limited", "none"] = "none"


class TradeReadiness(BaseModel):
    level: Literal["ready", "caution", "high_risk", "unknown"]
    checks: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class GasAdvisory(BaseModel):
    destination_chain: str
    native_token: str
    status: Literal["check_required", "not_required"]
    message: str


class RiskAssessment(BaseModel):
    """Risk-agent verdict on a proposed trade against the user's risk charter.

    A soft, user-configurable layer on top of the deterministic execution caps.
    `charter_applied` is False in advisory mode (no charter set) -- then
    `verdict` is always "ok" and `summary` is informational only.
    """

    # "unresolved": a rule the charter sets could not be evaluated (no portfolio
    # value, unknown notional); treated like blocked -- the card is withheld.
    verdict: Literal["ok", "blocked", "unresolved"]
    summary: str
    charter_applied: bool = False


class ValidationCheck(BaseModel):
    name: Literal["provenance", "freshness", "grounding", "consistency", "coverage"]
    status: Literal["ok", "warn", "not_applicable"]
    detail: str


class AnswerValidation(BaseModel):
    """Advisory step-7 validation of a surfaced answer (see app/answer_validator.py):
    does its data trace to a source (provenance), is it timestamped and recent
    (freshness), and do its figures appear in the tool evidence (grounding). Never
    blocks or rewrites the answer -- it is surfaced for the client and monitoring."""

    status: Literal["ok", "warn"]
    checks: list[ValidationCheck] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    as_of: str | None = None
    age_minutes: float | None = None


class ChartCard(BaseModel):
    """What the browser's TradingView chart widget should draw for this answer."""
    symbol: str                       # exchange-qualified, e.g. BINANCE:SOLUSDT or NASDAQ:AAPL
    label: str
    interval: str = "60"
    kind: Literal["crypto", "equity"] = "crypto"


class AgentResponse(BaseModel):
    answer: str
    prediction_card: dict | None = None
    # What this turn cost the caller's credit balance (absent for service callers).
    credits: dict | None = None
    trade_plan: TradePlan | None = None
    trajectory: dict | None = None
    session_id: str
    suggestions: list[str] = Field(default_factory=list)
    quick_actions: list[QuickAction] = Field(default_factory=list)
    session_revision: int = Field(default=0, ge=0)
    intent: Literal["general", "research", "portfolio", "trade", "cross_chain_swap"] = "general"
    capabilities: list[str] = Field(default_factory=list)
    cross_chain_swap: CrossChainSwapDraft | None = None
    context_capsules: list[ContextCapsule] = Field(default_factory=list)
    intent_lock: IntentLock | None = None
    evidence: EvidenceSummary | None = None
    # The evidence envelopes the turn's tools recorded (app/evidence.py):
    # status, sources, coverage and errors per tool, never the bulk data.
    envelopes: list[dict] = Field(default_factory=list)
    # A durable job this turn started and detached from: the browser polls
    # it and refreshes the conversation when it settles.
    job_id: str | None = None
    trade_readiness: TradeReadiness | None = None
    gas_advisory: GasAdvisory | None = None
    risk_assessment: RiskAssessment | None = None
    team_report: dict | None = None
    validation: AnswerValidation | None = None
    team_mode: bool = False
    risk_charter: str | None = None
    risk_charter_fields: dict | None = None
    chart: ChartCard | None = None
    # The answer gate's verdict when it rewrote or replaced the answer (None otherwise).
    answer_gate: dict | None = None


class IntentPreviewRequest(BaseModel):
    request: str = Field(min_length=1, max_length=2000)
    conversation_history: str = Field(default="", max_length=12_000)
    live: bool = False


class ProviderTestRequest(BaseModel):
    request: str = Field(min_length=1, max_length=2000)
    capability: str = Field(min_length=1, max_length=80)
    chains: list[str] = Field(default_factory=list, max_length=10)
    provider: str | None = Field(default=None, max_length=80)
    live: bool = False


class PortfolioScenarioRequest(BaseModel):
    change_pct: float = Field(ge=-100, le=1000)
    symbol: str | None = Field(default=None, min_length=1, max_length=20)


class WalletAuthChallengeRequest(BaseModel):
    """EVM-only, fixed shape the Coinbase Wallet SDK bundle calls directly."""
    address: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    chain_id: int = Field(default=1, ge=1, le=2_147_483_647)


class WalletAuthVerifyRequest(BaseModel):
    address: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    nonce: str = Field(min_length=16, max_length=128)
    signature: str = Field(min_length=2, max_length=32_770, pattern=r"^(?:0x)?(?:[0-9a-fA-F]{2})+$")


class WalletChallengeRequest(BaseModel):
    # Solana: a base58 Ed25519 public key (32-44 chars). EVM: a 0x + 40-hex
    # address. `chain` is the literal "solana", or an EVM chain_id as a
    # decimal string ("1", "8453", ...) -- app.wallet_auth resolves which.
    address: str = Field(min_length=32, max_length=66)
    chain: str = Field(default="1", min_length=1, max_length=20)


class WalletVerifyRequest(BaseModel):
    address: str = Field(min_length=32, max_length=66)
    chain: str = Field(default="1", min_length=1, max_length=20)
    nonce: str = Field(min_length=16, max_length=128)
    # EOA/Ed25519 signatures are fixed-size; smart/counterfactual EVM wallets
    # can return much larger ERC-1271/ERC-6492 payloads.
    signature: str = Field(
        min_length=2,
        max_length=32_770,
        pattern=r"^(?:0x)?(?:[0-9a-fA-F]{2})+$",
    )


class ProviderPolicyUpdate(BaseModel):
    enabled: bool | None = None
    priority: float | None = Field(default=None, ge=-100, le=100)
    quota_per_minute: int | None = Field(default=None, ge=1, le=100_000)
    cost_usd: float | None = Field(default=None, ge=0, le=1_000)


class RoutePreviewRequest(BaseModel):
    request: str = Field(min_length=1, max_length=2000)
    capability: str = Field(min_length=1, max_length=80)
    chains: list[str] = Field(default_factory=list, max_length=10)


class WashTradingDetectionRequest(BaseModel):
    # Solana-only detector (dex_solana.trades): the mint is a base58 address.
    # The pattern rejects anything with a quote/backslash/whitespace at the
    # API boundary, so it can never break out of the quoted SQL literal it is
    # interpolated into (app/dune_tools.py re-validates as defense in depth).
    token_mint: str = Field(pattern=r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
    # NOTE: pool_filter is spliced into the Dune query as a RAW SQL FRAGMENT by
    # design (an analyst writing e.g. "project = 'raydium'"), so this endpoint's
    # admin key effectively grants arbitrary-SQL power against the Dune account.
    # That is why /admin/wash-trading/runs is admin-only; keep it that way.
    pool_filter: str | None = Field(default=None, max_length=200)
    window_start: datetime
    window_end: datetime
    top_n: int = Field(default=100, ge=1, le=500)
    label: str | None = Field(default=None, max_length=120)


class LifiQuoteRequest(BaseModel):
    from_chain: str = Field(min_length=1, max_length=64)
    to_chain: str = Field(min_length=1, max_length=64)
    from_token: str = Field(min_length=1, max_length=128)
    to_token: str = Field(min_length=1, max_length=128)
    from_amount: str = Field(pattern=r"^[0-9]{1,80}$")
    from_address: str = Field(min_length=20, max_length=128)
    to_address: str | None = Field(default=None, min_length=20, max_length=128)
    slippage: float | None = Field(default=None, ge=0, le=1)
