"""Pays for GET /ask on Solana devnet with a partially signed SPL transfer. Prints the settlement."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import warnings

warnings.simplefilter("ignore")

from solders.keypair import Keypair  # noqa: E402
from x402 import x402Client  # noqa: E402
from x402.http.clients import wrapHttpxWithPayment  # noqa: E402
from x402.mechanisms.svm import KeypairSigner  # noqa: E402
from x402.mechanisms.svm.exact import register_exact_svm_client  # noqa: E402

from common import RESOURCE_URL, load_keys  # noqa: E402


async def main() -> int:
    sol = load_keys().get("solana", {})
    if "payer" not in sol:
        print("run demo/setup_devnet.py first")
        return 1
    payer = Keypair.from_base58_string(sol["payer"])
    client = x402Client()
    client.set_spend_controls(False)  # the demo pays in a test token, not a default asset
    register_exact_svm_client(
        client, KeypairSigner(payer), networks="solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1", rpc_url="https://api.devnet.solana.com"
    )
    async with wrapHttpxWithPayment(client, timeout=180) as http:
        r = await http.get(f"{RESOURCE_URL}/ask")
        print("status:", r.status_code)
        print("body:  ", r.text)
        settle = r.headers.get("payment-response")
        if settle:
            print("settlement:", json.dumps(json.loads(base64.b64decode(settle)), indent=2))
        return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
