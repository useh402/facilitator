"""The facilitator service: policy, idempotent settlement, confirmations and recovery on top of
the x402 SDK's verify and settle."""

from __future__ import annotations

import logging
import time
from typing import Any

from x402.schemas import (
    PaymentPayload,
    PaymentRequirements,
    SettleResponse,
    VerifyResponse,
    parse_payment_payload,
    parse_payment_requirements,
)

from .config import EXPLORER_TX, Settings
from .keys import payment_key, rail_of, summarize
from .logging import log
from .rails import Rail
from .store import ReplayStore, Row

logger = logging.getLogger("humanapi.facilitator")

ERR_SETTLEMENT_PENDING = "settlement_pending"


class RequestError(Exception):
    def __init__(self, status: int, reason: str, message: str | None = None):
        super().__init__(message or reason)
        self.status = status
        self.reason = reason
        self.message = message or reason


class FacilitatorService:
    def __init__(self, settings: Settings, store: ReplayStore, facilitator: Any, rails: dict[str, Rail]):
        self.settings = settings
        self.store = store
        self.facilitator = facilitator
        self.rails = rails
        self.started_at = time.time()

    # -- parsing ---------------------------------------------------------------------------------

    def parse(self, body: dict[str, Any]) -> tuple[PaymentPayload, PaymentRequirements]:
        if not isinstance(body, dict):
            raise RequestError(400, "invalid_request", "body must be a JSON object")
        version = body.get("x402Version")
        if version != 2:
            raise RequestError(400, "unsupported_version", "only x402Version 2 is supported")
        try:
            payload = parse_payment_payload(body.get("paymentPayload") or {})
            requirements = parse_payment_requirements(2, body.get("paymentRequirements") or {})
        except Exception as e:  # pydantic validation
            raise RequestError(400, "invalid_request", str(e)[:300]) from e
        if getattr(payload, "x402_version", 2) != 2:
            raise RequestError(400, "unsupported_version", "payload is not version 2")
        return payload, requirements  # type: ignore[return-value]

    def rail_for(self, network: str) -> Rail:
        try:
            rail = self.rails.get(rail_of(network))
        except ValueError:
            rail = None
        if rail is None or rail.network != network:
            raise RequestError(400, "unsupported_network", f"this facilitator does not serve {network}")
        return rail

    # -- policy ----------------------------------------------------------------------------------

    def policy_reason(
        self, rail: Rail, payload: PaymentPayload, requirements: PaymentRequirements, exclude_key: str | None = None
    ) -> str | None:
        if requirements.scheme != "exact" or payload.accepted.scheme != "exact":
            return "unsupported_scheme"
        if str(payload.accepted.network) != str(requirements.network):
            return "network_mismatch"
        try:
            amount = int(str(requirements.amount))
        except ValueError:
            return "invalid_amount"
        if amount <= 0:
            return "invalid_amount"
        if amount > rail.max_amount:
            return "amount_exceeds_facilitator_cap"
        if rail.allowed_assets is not None:
            allowed = {a.lower() for a in rail.allowed_assets}
            if str(requirements.asset).lower() not in allowed:
                return "asset_not_allowed"
        since = time.time() - 86400
        if self.store.count_since(rail.name, since, exclude_key=exclude_key) >= self.settings.daily_settlement_cap:
            return "daily_settlement_cap_reached"
        return None

    # -- endpoints -------------------------------------------------------------------------------

    def supported(self) -> dict[str, Any]:
        return self.facilitator.get_supported().model_dump(by_alias=True, exclude_none=True)

    def verify(self, body: dict[str, Any]) -> dict[str, Any]:
        t0 = time.time()
        payload, requirements = self.parse(body)
        rail = self.rail_for(str(requirements.network))
        reason = self.policy_reason(rail, payload, requirements)
        if reason:
            result = VerifyResponse(is_valid=False, invalid_reason=reason)
        else:
            try:
                result = self.facilitator.verify(payload, requirements)
            except Exception as e:
                log(logger, logging.ERROR, "verify raised", rail=rail.name, error=f"{type(e).__name__}: {str(e)[:300]}")
                result = VerifyResponse(is_valid=False, invalid_reason="facilitator_error", invalid_message=type(e).__name__)
        self._audit("verify", rail, payload, requirements, result.invalid_reason or "ok", t0)
        return result.model_dump(by_alias=True, exclude_none=True)

    def settle(self, body: dict[str, Any]) -> dict[str, Any]:
        t0 = time.time()
        payload, requirements = self.parse(body)
        rail = self.rail_for(str(requirements.network))
        meta = summarize(payload, requirements)
        key = payment_key(meta)

        status, row = self.store.begin(key, meta)
        if status == "settled" and row.response:
            self._audit("settle", rail, payload, requirements, "replay", t0, key)
            return row.response
        if status == "uncertain":
            row = self.resolve(row, rail)
            status = row.status
            if status == "settled" and row.response:
                return row.response
        if status in ("sending", "uncertain"):
            self._audit("settle", rail, payload, requirements, ERR_SETTLEMENT_PENDING, t0, key)
            return self._pending(rail, row.tx, meta.get("payer")).model_dump(by_alias=True, exclude_none=True)
        if status == "failed":
            row = self.store.retry(key)

        reason = self.policy_reason(rail, payload, requirements, exclude_key=key)
        if reason:
            result = SettleResponse(success=False, error_reason=reason, transaction="", network=rail.network, payer=meta.get("payer"))
            self.store.mark_failed(key, reason, result.model_dump(by_alias=True, exclude_none=True))
            self._audit("settle", rail, payload, requirements, reason, t0, key)
            return result.model_dump(by_alias=True, exclude_none=True)

        if rail.name == "solana":
            try:
                expected = rail.svm_expected_signature(
                    payload.payload.get("transaction", ""), str((requirements.extra or {}).get("feePayer", ""))
                )
                if expected:
                    self.store.set_tx(key, expected)
            except Exception as e:
                self.store.mark_failed(key, "invalid_transaction")
                result = SettleResponse(success=False, error_reason="invalid_exact_svm_payload", error_message=str(e)[:200], transaction="", network=rail.network)
                return result.model_dump(by_alias=True, exclude_none=True)

        try:
            result = self.facilitator.settle(payload, requirements)
        except Exception as e:
            # We do not know whether a transaction left the process; recovery decides from the chain.
            self.store.mark_uncertain(key, None, f"exception: {type(e).__name__}: {str(e)[:200]}")
            log(logger, logging.ERROR, "settle raised", rail=rail.name, key=key, error=str(e)[:300])
            self._audit("settle", rail, payload, requirements, "exception", t0, key)
            return self._pending(rail, None, meta.get("payer")).model_dump(by_alias=True, exclude_none=True)

        if result.success:
            if rail.name == "robinhood" and rail.confirmations > 1:
                if not self._wait_confirmations(rail, result.transaction):
                    self.store.mark_uncertain(key, result.transaction, "confirmation timeout")
                    self._audit("settle", rail, payload, requirements, ERR_SETTLEMENT_PENDING, t0, key)
                    return self._pending(rail, result.transaction, result.payer).model_dump(by_alias=True, exclude_none=True)
            out = result.model_dump(by_alias=True, exclude_none=True)
            out["explorer"] = EXPLORER_TX.get(rail.network, "{tx}").format(tx=result.transaction)
            self.store.mark_settled(key, result.transaction, out)
            self._audit("settle", rail, payload, requirements, "settled", t0, key)
            return out

        out = result.model_dump(by_alias=True, exclude_none=True)
        if result.error_reason == ERR_SETTLEMENT_PENDING:
            self.store.mark_uncertain(key, result.transaction or None, ERR_SETTLEMENT_PENDING)
        else:
            self.store.mark_failed(key, result.error_reason or "settle_failed", out)
        self._audit("settle", rail, payload, requirements, result.error_reason or "failed", t0, key)
        return out

    def health(self) -> dict[str, Any]:
        rails: dict[str, Any] = {}
        ok = True
        for rail in self.rails.values():
            entry: dict[str, Any] = {
                "network": rail.network,
                "signer": rail.signer_address,
                "confirmations": rail.confirmations,
                "maxAmount": str(rail.max_amount),
                "allowedAssets": rail.allowed_assets,
            }
            try:
                entry["head"] = rail.head()
                balance = rail.native_balance()
                entry["nativeBalance"] = str(balance)
                entry["lowGas"] = balance < rail.min_native
                entry["reachable"] = True
            except Exception as e:
                entry["reachable"] = False
                entry["error"] = str(e)[:200]
                ok = False
            rails[rail.name] = entry
        try:
            counts = self.store.status_counts()
            db_ok = True
        except Exception:
            counts, db_ok, ok = {}, False, False
        return {
            "ok": ok and db_ok,
            "env": self.settings.env,
            "uptimeSeconds": int(time.time() - self.started_at),
            "rails": rails,
            "settlements": counts,
            "database": "ok" if db_ok else "error",
        }

    # -- recovery --------------------------------------------------------------------------------

    def recover(self) -> int:
        """Resolve rows left in `sending` or `uncertain` by asking the chain. Returns rows resolved."""
        resolved = 0
        for row in self.store.unresolved(self.settings.stuck_after_seconds):
            rail = self.rails.get(row.rail)
            if rail is None or rail.network != row.network:
                continue
            try:
                new = self.resolve(row, rail)
            except Exception as e:
                log(logger, logging.WARNING, "recovery failed", key=row.key, error=str(e)[:200])
                continue
            if new.status in ("settled", "failed"):
                resolved += 1
        return resolved

    def resolve(self, row: Row, rail: Rail) -> Row:
        """Decide one unresolved row from chain state."""
        now = time.time()
        status = "unknown"
        if row.tx:
            status = rail.tx_status(row.tx)
        if status == "unknown" and rail.name == "robinhood" and row.payer and row.nonce:
            try:
                if rail.permit2_nonce_used(row.payer, int(row.nonce)):
                    status = "settled"
            except Exception as e:
                log(logger, logging.WARNING, "nonce bitmap read failed", key=row.key, error=str(e)[:200])

        if status == "settled":
            response = SettleResponse(success=True, transaction=row.tx or "", network=rail.network, payer=row.payer)
            out = response.model_dump(by_alias=True, exclude_none=True)
            out["recovered"] = True
            if row.tx:
                out["explorer"] = EXPLORER_TX.get(rail.network, "{tx}").format(tx=row.tx)
            self.store.mark_settled(row.key, row.tx or "", out)
            log(logger, logging.INFO, "recovered as settled", key=row.key, rail=rail.name)
        elif status == "failed":
            self.store.mark_failed(row.key, "transaction_failed_on_chain")
            log(logger, logging.INFO, "recovered as failed", key=row.key, rail=rail.name)
        else:
            expired = False
            if rail.name == "robinhood" and row.deadline:
                expired = now > row.deadline + 60
            if rail.name == "solana":
                expired = now > row.updated_at + 150  # a blockhash lives about 60 to 90 seconds
            if expired:
                self.store.mark_failed(row.key, "never_landed")
                log(logger, logging.INFO, "recovered as never landed", key=row.key, rail=rail.name)
            elif row.status != "uncertain":
                self.store.mark_uncertain(row.key, row.tx, "stuck in sending")
        new = self.store.get(row.key)
        assert new is not None
        return new

    # -- helpers ---------------------------------------------------------------------------------

    def _wait_confirmations(self, rail: Rail, tx: str) -> bool:
        deadline = time.time() + min(60.0, self.settings.settle_timeout_seconds)
        target: int | None = None
        while time.time() < deadline:
            try:
                if target is None:
                    receipt = rail.chain.eth.get_transaction_receipt(tx)
                    target = int(receipt["blockNumber"]) + rail.confirmations - 1
                if rail.head() >= target:
                    return True
            except Exception:
                pass
            time.sleep(1.0)
        return False

    def _pending(self, rail: Rail, tx: str | None, payer: str | None) -> SettleResponse:
        return SettleResponse(
            success=False,
            error_reason=ERR_SETTLEMENT_PENDING,
            error_message="a settlement for this payment is in progress; retry with the same payload",
            transaction=tx or "",
            network=rail.network,
            payer=payer,
        )

    def _audit(self, kind: str, rail: Rail, payload: PaymentPayload, requirements: PaymentRequirements, outcome: str, t0: float, key: str | None = None) -> None:
        latency = round((time.time() - t0) * 1000, 1)
        payer = None
        try:
            payer = summarize(payload, requirements).get("payer")
        except Exception:
            pass
        try:
            self.store.event(kind, key=key, rail=rail.name, network=rail.network, payer=payer, amount=str(requirements.amount), outcome=outcome, latency_ms=latency)
        except Exception:
            pass
        log(logger, logging.INFO, kind, rail=rail.name, network=rail.network, payer=payer, amount=str(requirements.amount), outcome=outcome, latency_ms=latency, key=key)

    # -- gas grants ---------------------------------------------------------------------------------

    def gas_grant(self, body: dict) -> dict:
        """Sends a little of the native token to a wallet so its owner can move their rewards. Capped per wallet and per day."""
        address = str(body.get("address", ""))
        rail = self.rails.get("robinhood")
        if rail is None:
            raise RequestError(400, "unsupported", "gas grants are only on Robinhood Chain")
        from web3 import Web3

        if not Web3.is_address(address):
            raise RequestError(400, "invalid_request", "address must be an EVM address")
        address = Web3.to_checksum_address(address)
        now = time.time()
        if self.store.gas_grants_since(rail.name, now - 86400) >= self.settings.gas_grants_per_day:
            raise RequestError(429, "daily_cap", "no more gas grants today")
        if self.store.gas_grants_since(rail.name, now - self.settings.gas_grant_hours_between * 3600, address) > 0:
            raise RequestError(429, "already_granted", "this wallet already received gas recently")
        amount = int(self.settings.gas_grant_wei)
        if int(rail.chain.eth.get_balance(address)) >= amount:
            return {"granted": False, "reason": "the wallet already has enough gas", "transaction": None}
        if rail.native_balance() < amount * 20:
            raise RequestError(503, "low_gas", "the facilitator is low on gas")
        tx = self._send_native(rail, address, amount)
        self.store.record_gas_grant(rail.name, address, amount, tx)
        return {"granted": True, "amountWei": str(amount), "transaction": tx}

    def _send_native(self, rail, to: str, amount: int) -> str:
        from eth_account import Account

        w3 = rail.chain
        acct = Account.from_key(self.settings.robinhood_private_key)
        tx = {"from": acct.address, "to": to, "value": amount, "gas": 30_000, "gasPrice": int(w3.eth.gas_price * 2), "chainId": int(w3.eth.chain_id), "nonce": w3.eth.get_transaction_count(acct.address, "pending")}
        raw = acct.sign_transaction(tx).raw_transaction
        h = w3.eth.send_raw_transaction(raw)
        receipt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
        if int(receipt["status"]) != 1:
            raise RequestError(502, "failed", "the gas transfer failed on chain")
        return "0x" + h.hex().removeprefix("0x")
