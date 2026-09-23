"""The subscription catalog and what each plan entitles a user to.

Prices and credit amounts are product decisions (2026-09-15): Free 100/mo,
Pro $19 -> 2,000/mo, Max $49 -> 7,500/mo; anonymous visitors get a one-time
10-credit trial keyed on IP/device. Stripe price ids come from settings so the
catalog is the same in test and live mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.settings import settings


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    price_usd_month: float
    monthly_credits: int
    # One-time grant for accounts that never sign in (anonymous trial).
    trial_credits: int = 0
    api_keys: bool = False
    mcp: bool = False
    team_mode: bool = True
    chat_requests_per_minute: int = 10
    history: bool = True
    wallets: bool = True
    seats: int = 1
    features: tuple[str, ...] = field(default_factory=tuple)

    def stripe_price_id(self) -> str | None:
        return getattr(settings, f"stripe_price_{self.id}", None) or None

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "price_usd_month": self.price_usd_month,
            "monthly_credits": self.monthly_credits,
            "trial_credits": self.trial_credits,
            "entitlements": {
                "api_keys": self.api_keys,
                "mcp": self.mcp,
                "team_mode": self.team_mode,
                "history": self.history,
                "wallets": self.wallets,
                "chat_requests_per_minute": self.chat_requests_per_minute,
                "seats": self.seats,
            },
            "features": list(self.features),
            "purchasable": bool(self.stripe_price_id()),
        }


ANONYMOUS = Plan(
    "anonymous", "Trial", 0.0, 0, trial_credits=10, api_keys=False, mcp=False, team_mode=False,
    chat_requests_per_minute=5, history=False, wallets=False,
    features=("10 trial credits", "Research and market data", "No history, wallets or trades"),
)
FREE = Plan(
    "free", "Free", 0.0, 300, api_keys=False, mcp=False, chat_requests_per_minute=10,
    features=("300 credits / month", "Conversation history", "Wallet connection and trade review", "Risk charter"),
)
PRO = Plan(
    "pro", "Pro", 19.0, 2000, api_keys=True, mcp=True, chat_requests_per_minute=30,
    features=("2,000 credits / month", "Everything in Free", "API keys and MCP access for Claude / ChatGPT", "Trading desk (team mode)"),
)
MAX = Plan(
    "max", "Max", 49.0, 7500, api_keys=True, mcp=True, chat_requests_per_minute=60, seats=5,
    features=("7,500 credits / month", "Everything in Pro", "Higher rate limits", "Team: up to 5 members share the credit pool", "Priority support"),
)

# The closed-beta plan: free, generous, every entitlement a tester needs to
# exercise the product (API keys, MCP, the trading desk). Only reachable when
# settings.closed_beta is on; never purchasable.
BETA = Plan(
    "beta", "Beta", 0.0, settings.closed_beta_monthly_credits, api_keys=True, mcp=True, team_mode=True,
    chat_requests_per_minute=30,
    features=(f"{settings.closed_beta_monthly_credits:,} credits / month, free during the beta", "Conversation history",
              "Wallet connection and trade review", "Risk charter", "API keys and MCP access", "Trading desk (team mode)"),
)
PLANS: dict[str, Plan] = {plan.id: plan for plan in (ANONYMOUS, FREE, PRO, MAX, BETA)}
PAID_PLAN_IDS = ("pro", "max")


@dataclass(frozen=True)
class Pack:
    """A one-time credit top-up (Stripe Checkout in payment mode)."""
    id: str
    name: str
    credits: int
    price_usd: float

    def stripe_price_id(self) -> str | None:
        return getattr(settings, f"stripe_price_{self.id}", None) or None

    def public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "credits": self.credits, "price_usd": self.price_usd,
            "purchasable": bool(self.stripe_price_id()),
        }


# Placeholder prices (roughly the Pro rate without the subscription discount);
# set the real amounts on the Stripe Prices -- Checkout charges what Stripe says.
PACKS: dict[str, Pack] = {
    pack.id: pack for pack in (
        Pack("pack_500", "Starter pack", 500, 6.0),
        Pack("pack_2000", "Builder pack", 2000, 20.0),
        Pack("pack_10000", "Desk pack", 10000, 85.0),
    )
}


def get_pack(pack_id: str | None) -> Pack | None:
    return PACKS.get(pack_id or "")


def get_plan(plan_id: str | None) -> Plan:
    plan = PLANS.get(plan_id or "", FREE)
    # In the closed beta every signed-in account is on the Beta plan, however
    # its row is labelled: a Free row from before, a Pro/Max row an admin set,
    # a stale subscription. The anonymous trial stays the trial. The monthly
    # grant is keyed by plan id, so a tester who already drew Free's allowance
    # this month gets the beta one at once.
    if settings.closed_beta and plan.id != ANONYMOUS.id:
        return BETA
    return plan


def catalog() -> list[dict]:
    """The plans a signed-in user can be on (the trial is not a choice)."""
    if settings.closed_beta:
        return [BETA.public()]
    return [plan.public() for plan in (FREE, PRO, MAX)]
