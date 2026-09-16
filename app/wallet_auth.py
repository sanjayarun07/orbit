"""One-time signature challenges proving control of a wallet -- EVM (EIP-191/
1271/6492) and Solana (Ed25519). Used by /auth/wallet/* (app/main.py) both to
sign a caller in with no email at all and to link a wallet to an already
signed-in account; the caller-facing session this produces is the ordinary
USER_COOKIE session (app/accounts.py's create_user_session), not anything in
this module -- create_challenge/verify_challenge and their Solana siblings
only answer one question: does this signature prove control of this address."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import secrets
import time

import base58
import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from eth_abi import encode
from eth_account import Account
from eth_account.messages import defunct_hash_message, encode_defunct
from eth_utils import keccak

from app.db import get_redis
from app.settings import settings


CHALLENGE_TTL = 5 * 60
SESSION_TTL = 7 * 24 * 60 * 60
COOKIE_NAME = "orbit_auth_session"
# Audited universal signature validator deployed deterministically with the
# EIP-2470 singleton factory. It validates EOAs, ERC-1271 contract wallets and
# ERC-6492 counterfactual wallets within eth_call (no state is persisted).
UNIVERSAL_SIGNATURE_VALIDATOR = "0x7dd271fa79df3a5feb99f73bebfa4395b2e4f4be"
_challenges: dict[str, tuple[float, dict]] = {}
_sessions: dict[str, tuple[float, dict]] = {}


def _prune() -> None:
    now = time.time()
    for store in (_challenges, _sessions):
        for key in [key for key, (expires, _) in store.items() if expires <= now]:
            store.pop(key, None)


async def create_challenge(address: str, domain: str, uri: str, chain_id: int = 1) -> dict:
    if chain_id not in settings.evm_auth_rpc_urls:
        raise ValueError(f"Wallet login is not configured for chain {chain_id}")
    nonce = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=CHALLENGE_TTL)
    normalized = address.lower()
    message = (
        f"{domain} wants you to sign in with your Ethereum account:\n"
        f"{address}\n\nSign in to Orbit. This request does not initiate a transaction or cost gas.\n\n"
        f"URI: {uri}\nVersion: 1\nChain ID: {chain_id}\nNonce: {nonce}\n"
        f"Issued At: {now.isoformat()}\nExpiration Time: {expires.isoformat()}"
    )
    value = {
        "address": normalized,
        "chain_id": chain_id,
        "message": message,
        "expires_at": expires.isoformat(),
    }
    redis = await get_redis()
    if redis is not None:
        await redis.setex(f"wallet_auth_challenge:{nonce}", CHALLENGE_TTL, json.dumps(value))
    else:
        _prune()
        _challenges[nonce] = (time.time() + CHALLENGE_TTL, value)
    return {"nonce": nonce, "message": message, "expires_at": expires.isoformat()}


async def _verify_universal_signature(
    address: str, message: str, signature: str, chain_id: int
) -> bool:
    """Verify smart-wallet signatures through the audited ERC-6492 validator."""
    rpc_url = settings.evm_auth_rpc_urls.get(chain_id)
    if not rpc_url:
        raise ValueError(f"Wallet login is not configured for chain {chain_id}")
    signature_bytes = bytes.fromhex(signature.removeprefix("0x"))
    digest = bytes(defunct_hash_message(text=message))
    selector = keccak(text="isValidSig(address,bytes32,bytes)")[:4]
    args = encode(["address", "bytes32", "bytes"], [address, digest, signature_bytes])
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": UNIVERSAL_SIGNATURE_VALIDATOR, "data": "0x" + (selector + args).hex()},
            "latest",
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(rpc_url, json=payload)
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError("The wallet signature verification network is temporarily unavailable") from exc
    if result.get("error"):
        return False
    value = result.get("result")
    return isinstance(value, str) and value not in {"", "0x"} and int(value, 16) == 1


async def verify_challenge(address: str, nonce: str, signature: str) -> tuple[str, dict]:
    redis = await get_redis()
    if redis is not None:
        raw = await redis.getdel(f"wallet_auth_challenge:{nonce}")
        value = json.loads(raw) if raw else None
    else:
        _prune()
        stored = _challenges.pop(nonce, None)
        value = stored[1] if stored and stored[0] > time.time() else None
    if not value or value.get("address") != address.lower():
        raise ValueError("Login challenge is invalid, expired, or already used")
    normalized_signature = signature if signature.startswith("0x") else f"0x{signature}"
    signature_size = (len(normalized_signature) - 2) // 2
    valid = False
    if signature_size == 65:
        try:
            recovered = Account.recover_message(
                encode_defunct(text=value["message"]),
                signature=normalized_signature,
            )
            valid = recovered.lower() == address.lower()
        except Exception:
            pass
    if not valid:
        valid = await _verify_universal_signature(
            address,
            value["message"],
            normalized_signature,
            int(value.get("chain_id", 1)),
        )
    if not valid:
        raise ValueError("Wallet signature does not match the requested address")
    token = secrets.token_urlsafe(32)
    session = {"address": address, "provider": "coinbase", "authenticated_at": datetime.now(timezone.utc).isoformat()}
    if redis is not None:
        await redis.setex(f"wallet_auth_session:{token}", SESSION_TTL, json.dumps(session))
    else:
        _sessions[token] = (time.time() + SESSION_TTL, session)
    return token, session


_SOLANA_CHALLENGE_TTL = CHALLENGE_TTL
_solana_challenges: dict[str, tuple[float, dict]] = {}


def _prune_solana() -> None:
    now = time.time()
    for key in [key for key, (expires, _) in _solana_challenges.items() if expires <= now]:
        _solana_challenges.pop(key, None)


async def create_solana_challenge(address: str, domain: str, uri: str) -> dict:
    """Sign-In-With-Solana-style message; the address itself is the base58
    Ed25519 public key, so there is no separate chain_id/RPC to configure."""
    try:
        if len(base58.b58decode(address)) != 32:
            raise ValueError
    except ValueError as exc:
        raise ValueError("Not a Solana address") from exc
    nonce = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=_SOLANA_CHALLENGE_TTL)
    message = (
        f"{domain} wants you to sign in with your Solana account:\n"
        f"{address}\n\nSign in to Orbit. This request does not initiate a transaction or cost gas.\n\n"
        f"URI: {uri}\nVersion: 1\nNonce: {nonce}\n"
        f"Issued At: {now.isoformat()}\nExpiration Time: {expires.isoformat()}"
    )
    value = {"address": address, "message": message, "expires_at": expires.isoformat()}
    redis = await get_redis()
    if redis is not None:
        await redis.setex(f"wallet_auth_challenge_sol:{nonce}", _SOLANA_CHALLENGE_TTL, json.dumps(value))
    else:
        _prune_solana()
        _solana_challenges[nonce] = (time.time() + _SOLANA_CHALLENGE_TTL, value)
    return {"nonce": nonce, "message": message, "expires_at": expires.isoformat()}


def _verify_ed25519(address: str, message: str, signature_hex: str) -> bool:
    try:
        public_key = base58.b58decode(address)
        if len(public_key) != 32:
            return False
        signature = bytes.fromhex(signature_hex.removeprefix("0x"))
        if len(signature) != 64:
            return False
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message.encode("utf-8"))
        return True
    except (InvalidSignature, ValueError):
        return False


async def verify_solana_challenge(address: str, nonce: str, signature: str) -> None:
    """Raises ValueError on any failure (expired/used/unknown nonce, wrong
    address, bad signature) -- mirrors verify_challenge's EVM contract so the
    route layer handles both the same way. Returns None on success."""
    redis = await get_redis()
    if redis is not None:
        raw = await redis.getdel(f"wallet_auth_challenge_sol:{nonce}")
        value = json.loads(raw) if raw else None
    else:
        _prune_solana()
        stored = _solana_challenges.pop(nonce, None)
        value = stored[1] if stored and stored[0] > time.time() else None
    if not value or value.get("address") != address:
        raise ValueError("Login challenge is invalid, expired, or already used")
    if not _verify_ed25519(address, value["message"], signature):
        raise ValueError("Wallet signature does not match the requested address")


async def get_auth_session(token: str | None) -> dict | None:
    if not token:
        return None
    redis = await get_redis()
    if redis is not None:
        raw = await redis.get(f"wallet_auth_session:{token}")
        return json.loads(raw) if raw else None
    _prune()
    stored = _sessions.get(token)
    return stored[1] if stored else None


async def delete_auth_session(token: str | None) -> None:
    if not token:
        return
    redis = await get_redis()
    if redis is not None:
        await redis.delete(f"wallet_auth_session:{token}")
    else:
        _sessions.pop(token, None)
