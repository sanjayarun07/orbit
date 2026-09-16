import base64

import base58
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from app.deployment import require_custodial_signing, require_execution_enabled
from app.plans import (
    claim_plan_submission,
    get_plan,
    get_prepared_transaction,
    update_submission,
)
from app.settings import settings
from app.solana_rpc import rpc, simulate_transaction


def _same_reviewed_message(expected, signed) -> bool:
    """Compare transaction semantics while allowing a wallet-refreshed blockhash.

    Wallet-standard implementations may replace a stale recent blockhash during
    approval. The blockhash changes freshness, not what the transaction does.
    All signer/account metadata, lookup tables, and compiled instructions remain
    strictly bound to the reviewed Jupiter plan.
    """
    attributes = ("header", "account_keys", "address_table_lookups", "instructions")
    return type(expected) is type(signed) and all(
        getattr(expected, attribute, None) == getattr(signed, attribute, None)
        for attribute in attributes
    )


def _reviewed_message_diff(expected, signed) -> list[str]:
    """Name the semantic fields changed by a wallet without exposing calldata."""
    if type(expected) is not type(signed):
        return ["transaction format"]
    labels = {
        "header": "required signers",
        "account_keys": "accounts or recipient",
        "address_table_lookups": "address lookup tables",
        "instructions": "program instructions, tokens, or amount",
    }
    return [
        label for attribute, label in labels.items()
        if getattr(expected, attribute, None) != getattr(signed, attribute, None)
    ]


async def execute_confirmed_plan(plan_id: str, confirmation_text: str) -> dict:
    # Checked before the plan is even loaded: a research deployment, or one
    # that does not hold keys for its users, refuses without touching state.
    require_custodial_signing()
    plan = await get_plan(plan_id)
    if plan.status != "pending_confirmation":
        raise ValueError(f"Plan cannot execute in status {plan.status}")
    if confirmation_text != plan.confirmation_text:
        raise ValueError("Confirmation text does not match")
    if not settings.live_trading:
        raise ValueError("LIVE_TRADING is disabled")
    if not settings.solana_private_key:
        raise ValueError("SOLANA_PRIVATE_KEY is not configured")

    keypair = Keypair.from_bytes(base58.b58decode(settings.solana_private_key))
    if str(keypair.pubkey()) != plan.wallet_address:
        raise ValueError("Configured signer does not match the quoted wallet")

    transaction = await get_prepared_transaction(plan)
    simulation = await simulate_transaction(transaction)
    if not simulation["ok"]:
        raise ValueError(f"Final transaction simulation failed: {simulation['error']}")
    unsigned = VersionedTransaction.from_bytes(base64.b64decode(transaction))
    signed = VersionedTransaction(unsigned.message, [keypair])
    encoded = base64.b64encode(bytes(signed)).decode()
    return await _broadcast_reviewed(plan, signed, encoded)



async def prepare_wallet_transaction(plan_id: str, confirmation_text: str) -> dict:
    """Return the exact simulated transaction for explicit browser-wallet approval."""
    require_execution_enabled("Preparing a transaction for wallet approval")
    plan = await get_plan(plan_id)
    if plan.status != "pending_confirmation":
        raise ValueError(f"Plan cannot execute in status {plan.status}")
    if confirmation_text != plan.confirmation_text:
        raise ValueError("Confirmation text does not match")
    if not settings.live_trading:
        raise ValueError("LIVE_TRADING is disabled")
    transaction = await get_prepared_transaction(plan)
    return {
        "plan_id": plan.plan_id,
        "wallet_address": plan.wallet_address,
        "transaction": transaction,
        "encoding": "base64",
    }


async def submit_wallet_transaction(
    plan_id: str, confirmation_text: str, signed_transaction: str
) -> dict:
    """Broadcast only a valid wallet signature over the plan-bound message."""
    require_execution_enabled("Submitting a signed transaction")
    plan = await get_plan(plan_id)
    if plan.status != "pending_confirmation":
        raise ValueError(f"Plan cannot execute in status {plan.status}")
    if confirmation_text != plan.confirmation_text:
        raise ValueError("Confirmation text does not match")
    if not settings.live_trading:
        raise ValueError("LIVE_TRADING is disabled")

    try:
        expected = VersionedTransaction.from_bytes(
            base64.b64decode(await get_prepared_transaction(plan), validate=True)
        )
        signed = VersionedTransaction.from_bytes(base64.b64decode(signed_transaction, validate=True))
    except Exception as exc:
        raise ValueError("Signed transaction is not valid base64 Solana transaction data") from exc

    if not _same_reviewed_message(expected.message, signed.message):
        changed = ", ".join(_reviewed_message_diff(expected.message, signed.message))
        raise ValueError(
            "Signed transaction does not match the reviewed trade plan. "
            f"Changed: {changed or 'unknown semantic fields'}. Request a fresh quote; nothing was submitted."
        )
    if str(signed.message.account_keys[0]) != plan.wallet_address:
        raise ValueError("Transaction signer does not match the wallet used for the quote")
    try:
        signed.verify_and_hash_message()
    except Exception as exc:
        raise ValueError("Wallet signature verification failed") from exc

    simulation = await simulate_transaction(signed_transaction)
    if not simulation["ok"]:
        raise ValueError(f"Signed transaction simulation failed: {simulation['error']}")
    return await _broadcast_reviewed(plan, signed, signed_transaction)


async def _broadcast_reviewed(plan, signed: VersionedTransaction, encoded: str) -> dict:
    """Persist the deterministic signature atomically BEFORE the network call."""
    signature = str(signed.signatures[0])
    await claim_plan_submission(plan, signature)
    status = "submitted"
    try:
        returned = await rpc("sendTransaction", [
            encoded, {"encoding": "base64", "skipPreflight": False, "maxRetries": 2},
        ])
        if returned != signature:
            status = "submission_unknown"
    except Exception:
        # Timeout can mean accepted. Reconciliation, never retry, resolves it.
        status = "submission_unknown"
    try:
        await update_submission(plan, status)
    except Exception:
        # The pre-broadcast record remains durable as submitting, with signature.
        status = "submission_unknown"
    return {"signature": signature, "plan_id": plan.plan_id, "status": status}
