"""Read-only, restartable reconciliation. Never signs or rebroadcasts a transaction."""
import asyncio
import logging

from app.plans import unsettled_plans, update_submission
from app.solana_rpc import rpc

logger = logging.getLogger(__name__)


async def reconcile_once() -> int:
    plans = await unsettled_plans()
    if not plans:
        return 0
    result = await rpc("getSignatureStatuses", [
        [p.submission_signature for p in plans], {"searchTransactionHistory": True},
    ])
    values = result.get("value")
    if not isinstance(values, list) or len(values) != len(plans):
        raise ValueError("Invalid signature status response")
    for plan, value in zip(plans, values):
        # Absence is not proof of rejection or expiration. Keep the plan locked.
        status = "submission_unknown"
        if isinstance(value, dict):
            if value.get("confirmationStatus") == "finalized":
                status = "failed" if value.get("err") is not None else "executed"
            else:
                status = "submitted"
        await update_submission(plan, status)
    return len(plans)


async def reconciliation_worker() -> None:
    while True:
        try:
            await reconcile_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Execution reconciliation unavailable; retrying without rebroadcast")
        await asyncio.sleep(15)
