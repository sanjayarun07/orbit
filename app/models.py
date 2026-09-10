from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    wallet_address: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    context_revision: int | None = Field(default=None, ge=0)
    quick_action: "QuickAction | None" = None


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


class AgentResponse(BaseModel):
    answer: str
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
    trade_readiness: TradeReadiness | None = None
    gas_advisory: GasAdvisory | None = None


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
    address: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    chain_id: int = Field(default=1, ge=1, le=2_147_483_647)


class WalletAuthVerifyRequest(BaseModel):
    address: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    nonce: str = Field(min_length=16, max_length=128)
    # EOAs use 65-byte signatures, while smart/counterfactual wallets can
    # return much larger ERC-1271/ERC-6492 payloads.
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


class LifiQuoteRequest(BaseModel):
    from_chain: str = Field(min_length=1, max_length=64)
    to_chain: str = Field(min_length=1, max_length=64)
    from_token: str = Field(min_length=1, max_length=128)
    to_token: str = Field(min_length=1, max_length=128)
    from_amount: str = Field(pattern=r"^[0-9]{1,80}$")
    from_address: str = Field(min_length=20, max_length=128)
    to_address: str | None = Field(default=None, min_length=20, max_length=128)
    slippage: float | None = Field(default=None, ge=0, le=1)
