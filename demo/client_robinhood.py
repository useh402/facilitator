"""Pays for GET /ask on Robinhood Chain testnet with a Permit2 signature. Prints the settlement."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import warnings

warnings.simplefilter("ignore")

from eth_account import Account  # noqa: E402
from x402 import x402Client  # noqa: E402
from x402.http.clients import wrapHttpxWithPayment  # noqa: E402
from x402.mechanisms.evm import EthAccountSigner  # noqa: E402
from x402.mechanisms.evm.exact import register_exact_evm_client  # noqa: E402

from common import RESOURCE_URL, load_keys  # noqa: E402


async def main() -> int:
    rh = load_keys().get("robinhood", {})
    if "payer" not in rh:
        print("run demo/setup_robinhood.py first")
        return 1
    payer = Account.from_key(rh["payer"])
    client = x402Client()
    client.set_spend_controls(False)  # the demo pays in a test token, not a default asset
    register_exact_evm_client(client, EthAccountSigner(payer), networks="eip155:46630")
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
