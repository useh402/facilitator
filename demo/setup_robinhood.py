"""Robinhood Chain testnet (46630) setup: keys, balance check, a 6-decimal test token, Permit2 approval.

Run once: `uv run python demo/setup_robinhood.py`. The facilitator and payer keys need test ETH first:
claim it at https://faucet.testnet.chain.robinhood.com for both addresses this script prints.
"""

from __future__ import annotations

import sys

from eth_account import Account
from web3 import Web3

from common import load_keys, save_keys

RPC = "https://rpc.testnet.chain.robinhood.com"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
MAX_UINT = 2**256 - 1

# A minimal 6-decimal ERC-20 (name HumanAPI Test USD, symbol tUSD) that mints 1,000,000 to the deployer.
# Compiled from the Solidity in demo/TestUSD.sol with solc 0.8.26 (see that file for the source).
TEST_USD_ABI = [
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "transfer", "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "mint", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
]


def send(w3: Web3, account, tx: dict) -> str:
    tx.setdefault("nonce", w3.eth.get_transaction_count(account.address))
    tx.setdefault("chainId", w3.eth.chain_id)
    if "gas" not in tx:
        tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.3)
    tx.setdefault("gasPrice", w3.eth.gas_price)
    signed = account.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    if receipt["status"] != 1:
        raise RuntimeError(f"transaction {h.hex()} reverted")
    return h.hex()


def main() -> int:
    w3 = Web3(Web3.HTTPProvider(RPC))
    keys = load_keys()
    rh = keys.setdefault("robinhood", {})
    fac = Account.from_key(rh["facilitator"]) if "facilitator" in rh else Account.create()
    payer = Account.from_key(rh["payer"]) if "payer" in rh else Account.create()
    merchant = Account.from_key(rh["merchant"]) if "merchant" in rh else Account.create()
    rh.update({"facilitator": fac.key.hex(), "payer": payer.key.hex(), "merchant": merchant.key.hex()})
    save_keys(keys)
    print("chain id:              ", w3.eth.chain_id)
    print("facilitator (gas):     ", fac.address)
    print("payer:                 ", payer.address)
    print("merchant (payTo):      ", merchant.address)
    need = False
    for name, acct in (("facilitator", fac), ("payer", payer)):
        bal = w3.eth.get_balance(acct.address)
        print(f"  {name} balance: {w3.from_wei(bal, 'ether')} ETH")
        if bal < w3.to_wei(0.01, "ether"):
            need = True
    if need:
        print("\nBoth addresses need test ETH. Claim at https://faucet.testnet.chain.robinhood.com then rerun.")
        return 1

    if "token" not in rh:
        from pathlib import Path

        bytecode = (Path(__file__).parent / "TestUSD.bin").read_text().strip()
        print("deploying test token ...")
        contract = w3.eth.contract(abi=TEST_USD_ABI, bytecode=bytecode)
        tx = contract.constructor().build_transaction({"from": payer.address})
        h = send(w3, payer, tx)
        receipt = w3.eth.get_transaction_receipt(h)
        rh["token"] = receipt["contractAddress"]
        save_keys(keys)
    token = w3.eth.contract(address=rh["token"], abi=TEST_USD_ABI)
    print("test token (tUSD):     ", rh["token"])
    print("payer tUSD balance:    ", token.functions.balanceOf(payer.address).call() / 1e6)

    allowance = token.functions.allowance(payer.address, PERMIT2).call()
    if allowance < 10**12:
        print("approving Permit2 once (the payer pays this gas) ...")
        tx = token.functions.approve(PERMIT2, MAX_UINT).build_transaction({"from": payer.address})
        print("  approve tx:", send(w3, payer, tx))
    print("Permit2 allowance OK")

    print("\nEnvironment for the facilitator (.env):")
    print("HF_ROBINHOOD_ENABLED=true")
    print("HF_ROBINHOOD_NETWORK=eip155:46630")
    print(f"HF_ROBINHOOD_PRIVATE_KEY={fac.key.hex()}")
    print(f"HF_ROBINHOOD_ASSETS={rh['token']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
