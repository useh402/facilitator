"""Builds the x402 SDK facilitator for the enabled rails, plus read-only chain clients for
health checks, confirmations and crash recovery."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from x402 import x402FacilitatorSync

from .config import Settings
from .store import ReplayStore

PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
PERMIT2_NONCE_BITMAP_ABI = [
    {
        "type": "function",
        "name": "nonceBitmap",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "address"}, {"name": "", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]


@dataclass
class Rail:
    name: str  # robinhood | solana
    network: str
    rpc_url: str
    signer_address: str
    allowed_assets: list[str] | None
    max_amount: int
    chain: Any  # Web3 or solana Client, read-only use
    signer: Any  # SDK facilitator signer
    confirmations: int = 1
    min_native: int = 0

    # -- chain reads used by the service -------------------------------------------------------

    def native_balance(self) -> int:
        if self.name == "robinhood":
            return int(self.chain.eth.get_balance(self.signer_address))
        from solders.pubkey import Pubkey

        return int(self.chain.get_balance(Pubkey.from_string(self.signer_address)).value)

    def head(self) -> int:
        if self.name == "robinhood":
            return int(self.chain.eth.block_number)
        return int(self.chain.get_slot().value)

    def tx_status(self, tx: str) -> str:
        """'settled', 'failed' or 'unknown' for a transaction hash or signature."""
        if self.name == "robinhood":
            try:
                receipt = self.chain.eth.get_transaction_receipt(tx)
            except Exception:
                return "unknown"
            if receipt is None:
                return "unknown"
            return "settled" if int(receipt["status"]) == 1 else "failed"
        from solders.signature import Signature

        result = self.chain.get_signature_statuses([Signature.from_string(tx)], search_transaction_history=True)
        status = result.value[0] if result.value else None
        if status is None:
            return "unknown"
        if status.err is not None:
            return "failed"
        return "settled" if status.confirmation_status is not None else "unknown"

    def permit2_nonce_used(self, owner: str, nonce: int) -> bool:
        """Read Permit2's unordered nonce bitmap: tells whether an authorization was consumed."""
        assert self.name == "robinhood"
        from web3 import Web3

        contract = self.chain.eth.contract(address=Web3.to_checksum_address(PERMIT2_ADDRESS), abi=PERMIT2_NONCE_BITMAP_ABI)
        bitmap = contract.functions.nonceBitmap(Web3.to_checksum_address(owner), nonce >> 8).call()
        return bool((int(bitmap) >> (nonce & 0xFF)) & 1)

    def svm_expected_signature(self, tx_base64: str, fee_payer: str) -> str | None:
        """The transaction id Solana will assign once we co-sign: our ed25519 signature is deterministic."""
        assert self.name == "solana"
        from solders.transaction import VersionedTransaction

        if fee_payer != self.signer_address:
            return None
        signed = self.signer.sign_transaction(tx_base64, fee_payer, self.network)
        tx = VersionedTransaction.from_bytes(base64.b64decode(signed))
        return str(tx.signatures[0])


def build(settings: Settings, store: ReplayStore) -> tuple[x402FacilitatorSync, dict[str, Rail]]:
    facilitator = x402FacilitatorSync()
    rails: dict[str, Rail] = {}

    if settings.robinhood_enabled:
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware
        from x402.mechanisms.evm import FacilitatorWeb3Signer
        from x402.mechanisms.evm.exact.facilitator import ExactEvmScheme, ExactEvmSchemeConfig

        signer = FacilitatorWeb3Signer(
            private_key=settings.robinhood_private_key,
            rpc_url=settings.robinhood_rpc_url,
            confirmation_timeout_seconds=settings.settle_timeout_seconds,
        )
        scheme = ExactEvmScheme(signer, ExactEvmSchemeConfig(simulate_in_settle=True), pending_store=store.pending)
        facilitator.register([settings.robinhood_network], scheme)
        w3 = Web3(Web3.HTTPProvider(settings.robinhood_rpc_url, request_kwargs={"timeout": 20}))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        rails["robinhood"] = Rail(
            name="robinhood",
            network=settings.robinhood_network,
            rpc_url=settings.robinhood_rpc_url,
            signer_address=signer.address,
            allowed_assets=settings.allowed_assets(settings.robinhood_network),
            max_amount=settings.robinhood_max_amount,
            chain=w3,
            signer=signer,
            confirmations=max(1, settings.robinhood_confirmations),
            min_native=settings.robinhood_min_gas_wei,
        )

    if settings.solana_enabled:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            from solana.rpc.api import Client as SolanaClient
        from solders.keypair import Keypair
        from x402.mechanisms.svm.exact.facilitator import ExactSvmScheme
        from x402.mechanisms.svm.settlement_cache import SettlementCache
        from x402.mechanisms.svm.signers import FacilitatorKeypairSigner

        keypair = Keypair.from_base58_string(settings.solana_private_key)
        signer = FacilitatorKeypairSigner(keypair, rpc_url=settings.solana_rpc_url)
        scheme = ExactSvmScheme(signer, SettlementCache(), pending_store=store.pending)
        facilitator.register([settings.solana_network], scheme)
        rails["solana"] = Rail(
            name="solana",
            network=settings.solana_network,
            rpc_url=settings.solana_rpc_url,
            signer_address=str(keypair.pubkey()),
            allowed_assets=settings.allowed_assets(settings.solana_network),
            max_amount=settings.solana_max_amount,
            chain=SolanaClient(settings.solana_rpc_url, timeout=20),
            signer=signer,
            confirmations=1,
            min_native=settings.solana_min_lamports,
        )

    return facilitator, rails
