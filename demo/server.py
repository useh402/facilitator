"""A paywalled resource that uses the HumanAPI facilitator. One route, one accept per enabled rail.

    uv run python demo/server.py      (listens on :8403; needs the facilitator on :8402 and demo/.keys.json)
"""

from __future__ import annotations

import os
import warnings

warnings.simplefilter("ignore")

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from x402 import x402ResourceServer  # noqa: E402
from x402.http import HTTPFacilitatorClient  # noqa: E402
from x402.http.middleware.fastapi import payment_middleware  # noqa: E402
from x402.mechanisms.evm.exact import register_exact_evm_server  # noqa: E402
from x402.mechanisms.svm.exact import register_exact_svm_server  # noqa: E402

from common import FACILITATOR_URL, SETTLE_TOKEN, load_keys  # noqa: E402

PRICE = "10000"  # 0.01 in 6-decimal units


def build() -> FastAPI:
    keys = load_keys()
    facilitator = HTTPFacilitatorClient(
        {"url": FACILITATOR_URL, "create_headers": lambda: {"settle": {"Authorization": f"Bearer {SETTLE_TOKEN}"}}}
    )
    server = x402ResourceServer(facilitator)
    accepts: list[dict] = []

    rh = keys.get("robinhood", {})
    rh_token = rh.get("token") or os.environ.get("DEMO_ROBINHOOD_ASSET")
    if rh_token and rh.get("merchant") and os.environ.get("DEMO_ROBINHOOD", "1") == "1":
        from eth_account import Account

        register_exact_evm_server(server, "eip155:46630")
        accepts.append(
            {
                "scheme": "exact",
                "network": "eip155:46630",
                "payTo": Account.from_key(rh["merchant"]).address,
                "price": {"amount": PRICE, "asset": rh_token, "extra": {"assetTransferMethod": "permit2"}},
                "maxTimeoutSeconds": 300,
            }
        )

    sol = keys.get("solana", {})
    sol_mint = sol.get("mint") or os.environ.get("DEMO_SOLANA_ASSET")
    if sol_mint and sol.get("merchant") and os.environ.get("DEMO_SOLANA", "1") == "1":
        from solders.keypair import Keypair

        register_exact_svm_server(server, "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1", rpc_url="https://api.devnet.solana.com")
        accepts.append(
            {
                "scheme": "exact",
                "network": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
                "payTo": str(Keypair.from_base58_string(sol["merchant"]).pubkey()),
                "price": {"amount": PRICE, "asset": sol_mint},
                "maxTimeoutSeconds": 120,
            }
        )
    if not accepts:
        raise SystemExit("no rail configured: run demo/setup_devnet.py or demo/setup_robinhood.py first")

    routes = {"GET /ask": {"accepts": accepts, "description": "One human answer", "mime_type": "application/json"}}
    app = FastAPI(title="HumanAPI demo resource")
    middleware = payment_middleware(routes, server)

    @app.middleware("http")
    async def x402_middleware(request, call_next):
        return await middleware(request, call_next)

    @app.get("/ask")
    async def ask():
        return {"answer": "yes, that receipt is legible", "by": "a human", "paid": True}

    @app.get("/")
    async def root():
        return {"demo": "GET /ask costs 0.01 on each enabled rail", "accepts": [a["network"] for a in accepts]}

    return app


if __name__ == "__main__":
    uvicorn.run(build(), host="127.0.0.1", port=8403, log_level="warning")
