from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from humanapi_facilitator.config import ROBINHOOD_TESTNET, Settings
from humanapi_facilitator.rails import Rail
from humanapi_facilitator.service import FacilitatorService
from humanapi_facilitator.store import Database, ReplayStore

TEST_TOKEN = "0x1111111111111111111111111111111111111111"
PAY_TO = "0x209693Bc6afc0C5328bA36FaF03C514EF312287C"
PAYER = "0x857b06519E91e3A54538791bDbb0E22373e36b66"
PROXY = "0x402085c248EeA27D92E8b30b2C58ed07f9E20001"


def permit2_body(amount: str = "10000", nonce: str = "12345", deadline: int = 4102444800, asset: str = TEST_TOKEN) -> dict:
    requirements = {
        "scheme": "exact",
        "network": ROBINHOOD_TESTNET,
        "asset": asset,
        "amount": amount,
        "payTo": PAY_TO,
        "maxTimeoutSeconds": 300,
        "extra": {"assetTransferMethod": "permit2"},
    }
    payload = {
        "x402Version": 2,
        "accepted": requirements,
        "payload": {
            "signature": "0x" + "ab" * 65,
            "permit2Authorization": {
                "from": PAYER,
                "permitted": {"token": asset, "amount": amount},
                "spender": PROXY,
                "nonce": nonce,
                "deadline": str(deadline),
                "witness": {"to": PAY_TO, "validAfter": "0"},
            },
        },
    }
    return {"x402Version": 2, "paymentPayload": payload, "paymentRequirements": requirements}


class FakeChain:
    """Stands in for web3 / solana clients in Rail."""

    def __init__(self):
        self.receipts: dict[str, int] = {}
        self.block = 100
        self.nonces_used: set[tuple[str, int]] = set()

    class _Eth:
        def __init__(self, outer):
            self.outer = outer

        @property
        def block_number(self):
            return self.outer.block

        def get_balance(self, _addr):
            return 10**18

        def get_transaction_receipt(self, tx):
            if tx not in self.outer.receipts:
                raise Exception("not found")
            return {"status": self.outer.receipts[tx], "blockNumber": self.outer.block - 5}

    @property
    def eth(self):
        return FakeChain._Eth(self)


class FakeRail(Rail):
    def permit2_nonce_used(self, owner: str, nonce: int) -> bool:  # type: ignore[override]
        return (owner.lower(), nonce) in self.chain.nonces_used


class FakeFacilitator:
    def __init__(self):
        self.verify_result = {"is_valid": True, "payer": PAYER}
        self.settle_results: list = []
        self.settle_calls = 0
        self.verify_calls = 0
        self.raise_on_settle: Exception | None = None

    def get_supported(self):
        from x402.schemas import SupportedKind, SupportedResponse

        return SupportedResponse(
            kinds=[SupportedKind(x402_version=2, scheme="exact", network=ROBINHOOD_TESTNET)],
            signers={"eip155:*": [PAY_TO]},
        )

    def verify(self, payload, requirements):
        from x402.schemas import VerifyResponse

        self.verify_calls += 1
        return VerifyResponse(**self.verify_result)

    def settle(self, payload, requirements):
        from x402.schemas import SettleResponse

        self.settle_calls += 1
        if self.raise_on_settle:
            raise self.raise_on_settle
        if self.settle_results:
            r = self.settle_results.pop(0)
        else:
            r = {"success": True, "transaction": "0x" + "cd" * 32, "payer": PAYER}
        return SettleResponse(network=ROBINHOOD_TESTNET, **r)


@pytest.fixture
def settings(tmp_path):
    return Settings(env="local", database_url=f"sqlite:///{tmp_path}/t.db", robinhood_enabled=False, solana_enabled=False, settle_token="secret", rate_limit_per_minute=0, settle_timeout_seconds=2, _env_file=None)


@pytest.fixture
def store(settings):
    return ReplayStore(Database(settings.database_url))


@pytest.fixture
def fake():
    return FakeFacilitator()


@pytest.fixture
def service(settings, store, fake):
    chain = FakeChain()
    rail = FakeRail(
        name="robinhood",
        network=ROBINHOOD_TESTNET,
        rpc_url="http://fake",
        signer_address=PAY_TO,
        allowed_assets=[TEST_TOKEN],
        max_amount=1_000_000,
        chain=chain,
        signer=None,
        confirmations=3,
        min_native=1,
    )
    return FacilitatorService(settings, store, fake, {"robinhood": rail})


@pytest.fixture
def client(settings, service):
    from fastapi.testclient import TestClient

    from humanapi_facilitator.app import create_app

    app = create_app(settings, service)
    with TestClient(app) as c:
        yield c

