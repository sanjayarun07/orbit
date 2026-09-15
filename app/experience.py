"""User-facing trust metadata derived without extra model or provider calls."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import re

from app.context_entities import WalletReference, extract_token_reference, extract_wallet_reference
from app.models import (
    ContextCapsule,
    CrossChainSwapDraft,
    EvidenceSummary,
    GasAdvisory,
    IntentLock,
    TradePlan,
    TradeReadiness,
)
from app.capability_router import (
    extract_chains,
    extract_charter,
    extract_cross_chain_draft,
    is_charter_clear,
    is_charter_set,
    is_team_disable,
    is_team_enable,
    is_trade_cancellation,
    is_trade_confirmation,
    is_trade_modifier,
    route_capabilities,
)


_NATIVE_GAS = {
    "base": "ETH", "ethereum": "ETH", "arbitrum": "ETH", "optimism": "ETH",
    "robinhood": "ETH", "polygon": "POL", "bnb": "BNB", "avalanche": "AVAX",
    "solana": "SOL", "sui": "SUI", "tron": "TRX",
}
_NAMED_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])\$?([A-Za-z][A-Za-z0-9._-]{1,15})\s+"
    r"(?:token|coin|memecoin|meme\s+coin)\b",
    re.IGNORECASE,
)
_CONTEXTUAL_TOKEN = re.compile(r"\b(?:this|that|the)\s+(?:token|coin)\b", re.IGNORECASE)
_OWN_WALLET = re.compile(r"\b(?:my|connected)\b.{0,40}\b(?:wallet|portfolio|balances?|transactions?|activity|holdings?)\b", re.IGNORECASE)


def _fingerprint(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:20]


def build_intent_lock(
    plan: TradePlan | None,
    draft: CrossChainSwapDraft | None,
    wallet_address: str | None,
) -> IntentLock | None:
    if plan is not None:
        amount = format(
            Decimal(plan.proposal.amount_atomic) / (Decimal(10) ** plan.input_token.decimals),
            "f",
        )
        if "." in amount:
            amount = amount.rstrip("0").rstrip(".")
        payload = {
            "action": "swap", "source_chain": "solana", "destination_chain": "solana",
            "amount": amount, "input_token": plan.proposal.input_mint,
            "output_token": plan.proposal.output_mint, "recipient": plan.wallet_address,
            "max_slippage_bps": plan.proposal.slippage_bps,
        }
        return IntentLock(**payload, fingerprint=_fingerprint(payload))
    if draft and all((draft.source_chain, draft.destination_chain, draft.amount, draft.input_token, draft.output_token)):
        payload = {
            "action": "bridge" if draft.source_chain != draft.destination_chain else "swap",
            "source_chain": draft.source_chain, "destination_chain": draft.destination_chain,
            "amount": draft.amount, "input_token": draft.input_token,
            "output_token": draft.output_token, "recipient": draft.recipient or wallet_address,
            "max_slippage_bps": draft.slippage_bps,
        }
        return IntentLock(**payload, fingerprint=_fingerprint(payload))
    return None


def build_context_capsules(
    request: str,
    history: str,
    answer: str,
    plan: TradePlan | None,
    draft: CrossChainSwapDraft | None,
    connected_wallet: str | None = None,
) -> list[ContextCapsule]:
    capsules: list[ContextCapsule] = []
    if plan:
        capsules.extend([
            ContextCapsule(kind="token", label=plan.input_token.symbol, address=plan.input_token.mint, chain="solana", source="jupiter"),
            ContextCapsule(kind="token", label=plan.output_token.symbol, address=plan.output_token.mint, chain="solana", source="jupiter"),
        ])
    elif draft:
        if draft.input_token:
            capsules.append(ContextCapsule(kind="token", label=draft.input_token, chain=draft.source_chain, confidence=.8, source="relay_draft"))
        if draft.output_token:
            capsules.append(ContextCapsule(kind="token", label=draft.output_token, chain=draft.destination_chain, confidence=.8, source="relay_draft"))
    else:
        token = extract_token_reference(request)
        named = _NAMED_TOKEN.search(request)
        # Only explicitly labelled identities can bind an answer to a token.
        # DEX URLs generally identify pools; discovery lists contain unrelated assets.
        answer_token = extract_token_reference(answer) if token is None and re.search(
            r"\b(?:mint|contract)(?:\s+address)?\s*[:`*]*\s*(?:0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})", answer
        ) else None
        if token is None and not named:
            token = answer_token
        if token is None and _CONTEXTUAL_TOKEN.search(request):
            token = extract_token_reference(history)
        # Historical addresses are considered only for explicit wallet
        # follow-ups. Otherwise an old wallet—or a Solana token mint—can leak
        # into a completely new topic.
        wallet = extract_wallet_reference(request, answer)
        if wallet is None and re.search(
            r"\b(?:this|that|the)\s+(?:wallet|address|portfolio)\b"
            r"|\b(?:its|their)\s+(?:transactions?|balances?|holdings?|counterparties|pnl)\b",
            request,
            re.IGNORECASE,
        ):
            wallet = extract_wallet_reference(history)
        if connected_wallet and _OWN_WALLET.search(request):
            wallet = WalletReference(
                connected_wallet,
                "solana" if not connected_wallet.startswith("0x") else None,
            )
        if token:
            capsules.append(ContextCapsule(kind="token", label="Token", address=token.address, chain=token.chain))
        elif named and named.group(1).lower() not in {"this", "that", "the", "a", "an"}:
            chain_match = re.search(
                r"\b(solana|base|ethereum|arbitrum|optimism|polygon|bnb|avalanche|robinhood(?:\s+chain)?)\b",
                request,
                re.IGNORECASE,
            )
            chain = chain_match.group(1).lower().replace(" chain", "") if chain_match else None
            capsules.append(ContextCapsule(
                kind="token",
                label=named.group(1).upper(),
                address=answer_token.address if answer_token else None,
                chain=chain or (answer_token.chain if answer_token else None),
                confidence=.9 if answer_token else .8,
            ))
        if wallet and (not token or wallet.address.lower() != token.address.lower()):
            capsules.append(ContextCapsule(kind="wallet", label="Wallet", address=wallet.address, chain=wallet.chain))
    unique: dict[tuple, ContextCapsule] = {}
    for capsule in capsules:
        unique[(capsule.kind, (capsule.address or capsule.label).lower(), capsule.chain)] = capsule
    return list(unique.values())[:4]


def _tool_calls(trajectory: dict) -> list[tuple[str, object]]:
    """(tool_name, observation) per call, descending into nested trajectory
    dicts (the team desk nests as {"market_research": {...}})."""
    calls: list[tuple[str, object]] = []
    for key, value in trajectory.items():
        if key.startswith("tool_name_") and isinstance(value, str):
            index = key.rsplit("_", 1)[-1]
            calls.append((value, trajectory.get(f"observation_{index}")))
        elif isinstance(value, dict):
            calls.extend(_tool_calls(value))
    return calls


def build_evidence_summary(trajectory: object) -> EvidenceSummary | None:
    if not isinstance(trajectory, dict):
        return None
    names: list[str] = []
    successes = failures = 0
    cached = False
    for value, result in _tool_calls(trajectory):
        failed = isinstance(result, str) and ("failed:" in result.lower() or "error" in result[:120].lower())
        failures += int(failed)
        successes += int(not failed)
        if value == "semantic_cache":
            cached = True
        if value == "semantic_cache":
            provider = "cache"
        elif value.startswith("mcp_"):
            provider = value.split("_", 2)[1]
        else:
            provider = value.split("_", 1)[0]
        if provider not in names:
            names.append(provider)
    if not names:
        return None
    confidence = "high" if successes >= 2 and failures == 0 else "medium" if successes else "limited"
    return EvidenceSummary(
        generated_at=datetime.now(timezone.utc), providers=names,
        successful_tools=successes, failed_tools=failures, cached=cached, confidence=confidence,
    )


def build_trade_readiness(plan: TradePlan | None) -> TradeReadiness | None:
    if plan is None:
        return None
    checks = ["Jupiter token identity resolved", "Price-impact policy passed", "Transaction simulation passed"]
    if plan.output_token.verified:
        checks.append("Output token is verified by Jupiter")
    warnings = [re.sub(r"^[^:]{20,50}:\s*", "", item) for item in plan.warnings]
    level = "caution" if warnings or not plan.output_token.verified else "ready"
    return TradeReadiness(level=level, checks=checks, warnings=warnings[:5])


def build_gas_advisory(draft: CrossChainSwapDraft | None) -> GasAdvisory | None:
    if not draft or not draft.destination_chain or draft.source_chain == draft.destination_chain:
        return None
    native = _NATIVE_GAS.get(draft.destination_chain, "native gas token")
    output = (draft.output_token or "").upper()
    status = "not_required" if output == native else "check_required"
    message = (
        f"You are receiving {output or 'a token'} on {draft.destination_chain.title()}. "
        f"Keep some {native} there for later transactions. Orbit will verify the destination gas balance before submission."
        if status == "check_required" else
        f"The received {native} can pay destination-chain gas."
    )
    return GasAdvisory(destination_chain=draft.destination_chain, native_token=native, status=status, message=message)


def advance_session_context(
    previous: dict,
    request: str,
    connected_wallet: str | None,
    intent: str,
    capabilities: list[str],
    capsules: list[ContextCapsule],
    intent_lock: IntentLock | None,
    workflow_draft: CrossChainSwapDraft | None = None,
) -> dict:
    """Create the next canonical context snapshot after one completed turn."""
    context = dict(previous or {})
    context["revision"] = int(context.get("revision", 0)) + 1
    context["last_intent"] = intent
    # Risk charter persists across turns in session context (Minara-style
    # "Custom Prompt"). Set/clear it here so the next trade turn's Risk agent
    # sees it; a plain message leaves whatever is already stored untouched.
    if is_charter_set(request):
        rules = extract_charter(request)
        if rules:
            context["risk_charter"] = rules
            # A charter typed as free text has no structured fields; the card
            # path re-attaches them after this (app/main.py).
            context["risk_charter_fields"] = None
    elif is_charter_clear(request):
        context["risk_charter"] = None
        context["risk_charter_fields"] = None
    # Team-mode ("trading desk") toggle persists across turns in session context.
    if is_team_enable(request):
        context["team_mode"] = True
    elif is_team_disable(request):
        context["team_mode"] = False
    context["last_capabilities"] = list(capabilities)
    if connected_wallet:
        context["connected_wallet"] = {
            "address": connected_wallet,
            "chain": "solana" if not connected_wallet.startswith("0x") else context.get("connected_wallet", {}).get("chain"),
        }
    if capsules:
        # Prefer the latest token subject; otherwise retain the explicit wallet.
        focus = next((item for item in reversed(capsules) if item.kind == "token"), capsules[-1])
        context["focus"] = focus.model_dump()
    elif intent in {"research", "portfolio"}:
        # A new explicit topic with no resolved entity must not inherit the
        # prior token/wallet merely because it shares the same chat.
        context["focus"] = None
    if is_trade_cancellation(request):
        context["active_workflow"] = None
    elif intent_lock:
        context["active_workflow"] = {
            "intent": intent,
            "status": "pending_approval",
            **intent_lock.model_dump(),
        }
    elif intent == "cross_chain_swap" and workflow_draft is not None:
        context["active_workflow"] = {
            "intent": intent,
            "status": "collecting_details",
            **workflow_draft.model_dump(),
        }
    elif intent in {"trade", "cross_chain_swap"} and not is_trade_modifier(request):
        chains = extract_chains(request)
        partial = extract_cross_chain_draft(request, chains)
        active = context.get("active_workflow") or {}
        starts_new = bool(re.search(
            r"\b(?:swap|buy|sell|exchange|bridge|trade|convert|move)\b",
            request,
            re.IGNORECASE,
        ))
        if (
            active.get("status") == "collecting_details"
            and active.get("intent") in {"trade", "cross_chain_swap"}
            and not starts_new
        ):
            prior = {
                "source_chain": active.get("source_chain"),
                "destination_chain": active.get("destination_chain"),
                "amount": active.get("amount"),
                "input_token": active.get("input_token"),
                "output_token": active.get("output_token"),
                "recipient": active.get("recipient"),
                "slippage_bps": active.get("max_slippage_bps", active.get("slippage_bps")),
            }
            partial = {key: value if value is not None else prior.get(key) for key, value in partial.items()}
        if partial.get("source_chain") and not partial.get("destination_chain"):
            partial["destination_chain"] = partial["source_chain"]
        context["active_workflow"] = {
            "intent": intent,
            "status": "collecting_details",
            **partial,
        }
    elif intent in {"research", "portfolio"}:
        # Execution context is fail-closed across an explicit topic switch.
        # Existing quote cards remain independently reviewable by plan ID, but
        # later natural-language fragments cannot mutate an unrelated route.
        context["active_workflow"] = None
    elif intent == "general" and not is_trade_confirmation(request) and not is_trade_modifier(request):
        # Explanations and clarification turns cannot keep an unrelated draft
        # available for a later fragment to mutate.
        context["active_workflow"] = None
    return context
