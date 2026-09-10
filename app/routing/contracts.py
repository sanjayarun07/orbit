"""Stable, provider-neutral contracts for understanding one chat turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


WorkflowIntent = Literal["general", "research", "portfolio", "trade", "cross_chain_swap"]
ExecutionProvider = Literal["jupiter", "relay"]
RouteMode = Literal["read", "collect", "quote", "control"]


@dataclass(frozen=True)
class CapabilityRoute:
    """A semantic route, independent of a concrete provider implementation.

    ``intent`` remains compatible with the existing LangGraph branches.  The
    extra fields make provider selection and incomplete execution requests
    explicit instead of encoding them indirectly in the intent name.
    """

    intent: WorkflowIntent
    capabilities: tuple[str, ...]
    chains: tuple[str, ...] = ()
    confidence: float = 1.0
    source: str = "rules"
    execution_provider: ExecutionProvider | None = None
    mode: RouteMode = "read"
    missing_fields: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class ExecutionDraft:
    source_chain: str | None = None
    destination_chain: str | None = None
    amount: str | None = None
    input_token: str | None = None
    output_token: str | None = None
    recipient: str | None = None
    slippage_bps: int | None = None

    def as_dict(self) -> dict:
        return {
            "source_chain": self.source_chain,
            "destination_chain": self.destination_chain,
            "amount": self.amount,
            "input_token": self.input_token,
            "output_token": self.output_token,
            "recipient": self.recipient,
            "slippage_bps": self.slippage_bps,
        }

    def missing(self) -> tuple[str, ...]:
        required = ("source_chain", "destination_chain", "amount", "input_token", "output_token")
        return tuple(name for name in required if getattr(self, name) is None)
