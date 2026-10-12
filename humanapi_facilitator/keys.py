"""Deterministic keys and summaries for payment payloads.

The key identifies the authorization itself, not the HTTP request, so two settle calls for
the same signed payment map to the same row whatever else differs.
"""

from __future__ import annotations

import hashlib
from typing import Any

from x402.schemas import PaymentPayload, PaymentRequirements


def rail_of(network: str) -> str:
    if network.startswith("eip155:"):
        return "robinhood"
    if network.startswith("solana:"):
        return "solana"
    raise ValueError(f"unsupported network {network}")


def _norm_nonce(nonce: Any) -> str:
    s = str(nonce).strip().lower()
    try:
        return str(int(s, 0))
    except ValueError:
        return s


def summarize(payload: PaymentPayload, requirements: PaymentRequirements) -> dict[str, Any]:
    """Payer, amount, asset, nonce and deadline as stored in the replay table."""
    network = str(payload.accepted.network)
    inner = payload.payload or {}
    meta: dict[str, Any] = {
        "rail": rail_of(network),
        "network": network,
        "amount": str(requirements.amount),
        "asset": str(requirements.asset),
        "pay_to": str(requirements.pay_to),
        "payer": None,
        "nonce": None,
        "deadline": None,
    }
    if network.startswith("eip155:"):
        if "permit2Authorization" in inner:
            auth = inner["permit2Authorization"]
            meta["payer"] = str(auth.get("from", "")).lower()
            meta["nonce"] = _norm_nonce(auth.get("nonce", ""))
            meta["deadline"] = int(str(auth.get("deadline", 0)), 0)
            meta["kind"] = "permit2"
        elif "authorization" in inner:
            auth = inner["authorization"]
            meta["payer"] = str(auth.get("from", "")).lower()
            meta["nonce"] = _norm_nonce(auth.get("nonce", ""))
            meta["deadline"] = int(str(auth.get("validBefore", 0)), 0)
            meta["kind"] = "eip3009"
        else:
            raise ValueError("unrecognised EVM payload")
    else:
        from x402.mechanisms.svm.types import ExactSvmPayload
        from x402.mechanisms.svm.utils import (
            decode_transaction_from_payload,
            get_token_payer_from_transaction,
            transaction_message_hash,
        )

        tx = decode_transaction_from_payload(ExactSvmPayload.from_dict(inner))
        meta["nonce"] = transaction_message_hash(tx)
        try:
            meta["payer"] = get_token_payer_from_transaction(tx) or None
        except Exception:
            meta["payer"] = None
        meta["kind"] = "svm"
    return meta


def payment_key(meta: dict[str, Any]) -> str:
    raw = f"{meta['network']}|{meta.get('kind')}|{meta.get('payer') or ''}|{meta.get('nonce') or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()
