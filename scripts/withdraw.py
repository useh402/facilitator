"""Withdraws ETH from the facilitator's signer wallet on Robinhood Chain.

  python scripts/withdraw.py                      shows the balance and what would be sent (nothing moves)
  python scripts/withdraw.py --send               sends everything, less the gas for this one transfer, to the owner wallet
  python scripts/withdraw.py --send --amount 0.01 --to 0x...

The key is read from .env.mainnet (HF_ROBINHOOD_PRIVATE_KEY) and never printed. The default recipient is HF_OWNER_ADDRESS from the same file.
"""

from __future__ import annotations

import argparse
import pathlib
import time

from eth_account import Account
from web3 import Web3

ENV = pathlib.Path(__file__).resolve().parents[1] / ".env.mainnet"


def env_value(name: str) -> str:
    for line in ENV.read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def retry(f, tries=30):
    for i in range(tries):
        try:
            return f()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(5)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--to", default=env_value("HF_OWNER_ADDRESS"), help="recipient; default HF_OWNER_ADDRESS from .env.mainnet")
    ap.add_argument("--amount", type=float, default=None, help="ETH; default: everything")
    args = ap.parse_args()
    if not args.to:
        raise SystemExit("no recipient: set HF_OWNER_ADDRESS in .env.mainnet or pass --to")
    key = env_value("HF_ROBINHOOD_PRIVATE_KEY")
    acct = Account.from_key(key)
    w3 = Web3(Web3.HTTPProvider("https://rpc.mainnet.chain.robinhood.com", request_kwargs={"timeout": 30}))
    to = Web3.to_checksum_address(args.to)
    balance = retry(lambda: w3.eth.get_balance(acct.address))
    price = retry(lambda: w3.eth.gas_price) * 2
    gas = int(retry(lambda: w3.eth.estimate_gas({"from": acct.address, "to": to, "value": 1})) * 1.5)
    fee = gas * price
    value = balance - fee if args.amount is None else w3.to_wei(args.amount, "ether")
    print(f"facilitator {acct.address} holds {w3.from_wei(balance, 'ether')} ETH")
    if value <= 0 or value + fee > balance:
        raise SystemExit("not enough ETH for that withdrawal and its gas")
    print(f"would send {w3.from_wei(value, 'ether')} ETH to {to} (gas set aside: {w3.from_wei(fee, 'ether')} ETH)")
    if not args.send:
        print("nothing sent; add --send to withdraw")
        return
    tx = {"from": acct.address, "to": to, "value": value, "gas": gas, "gasPrice": price, "chainId": 4663, "nonce": retry(lambda: w3.eth.get_transaction_count(acct.address, "pending"))}
    raw = acct.sign_transaction(tx).raw_transaction
    h = Web3.keccak(raw)
    try:
        retry(lambda: w3.eth.send_raw_transaction(raw))
    except Exception as e:
        if "already known" not in str(e).lower():
            raise
    assert retry(lambda: w3.eth.wait_for_transaction_receipt(h, timeout=180))["status"] == 1
    print("sent:", "0x" + h.hex().removeprefix("0x"))


if __name__ == "__main__":
    main()
