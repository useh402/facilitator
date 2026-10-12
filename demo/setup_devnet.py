"""Solana devnet setup: facilitator fee-payer key, a payer key, a 6-decimal test mint, token accounts.

Run once: `uv run python demo/setup_devnet.py`. Writes demo/.keys.json and prints the env lines.
"""

from __future__ import annotations

import sys
import time
import warnings

warnings.simplefilter("ignore")

from solana.rpc.api import Client  # noqa: E402
from solana.rpc.types import TxOpts  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from spl.token.client import Token  # noqa: E402
from spl.token.constants import TOKEN_PROGRAM_ID  # noqa: E402

from common import load_keys, save_keys  # noqa: E402

RPC = "https://api.devnet.solana.com"
MINT_AMOUNT = 1_000 * 10**6  # 1000 test USD


def airdrop(client: Client, pubkey: Pubkey, lamports: int) -> bool:
    for attempt in range(4):
        try:
            sig = client.request_airdrop(pubkey, lamports).value
            for _ in range(30):
                time.sleep(2)
                st = client.get_signature_statuses([sig]).value[0]
                if st and st.confirmation_status is not None:
                    return True
        except Exception as e:  # rate limits are common on devnet
            print(f"  airdrop attempt {attempt + 1} failed: {str(e)[:120]}")
            time.sleep(5)
    return False


def main() -> int:
    client = Client(RPC)
    keys = load_keys()
    sol = keys.setdefault("solana", {})
    fac = Keypair.from_base58_string(sol["facilitator"]) if "facilitator" in sol else Keypair()
    payer = Keypair.from_base58_string(sol["payer"]) if "payer" in sol else Keypair()
    merchant = Keypair.from_base58_string(sol["merchant"]) if "merchant" in sol else Keypair()
    sol.update({"facilitator": str(fac), "payer": str(payer), "merchant": str(merchant)})
    save_keys(keys)
    print("facilitator (fee payer):", fac.pubkey())
    print("payer:                  ", payer.pubkey())
    print("merchant (payTo):       ", merchant.pubkey())

    for name, kp, want in (("facilitator", fac, 1_000_000_000), ("payer", payer, 1_000_000_000)):
        bal = client.get_balance(kp.pubkey()).value
        if bal < want // 2:
            print(f"requesting airdrop for {name} ...")
            if not airdrop(client, kp.pubkey(), want):
                print(f"airdrop failed for {name}; fund {kp.pubkey()} at https://faucet.solana.com and rerun")
                return 1
        print(f"  {name} balance: {client.get_balance(kp.pubkey()).value / 1e9:.3f} SOL")

    if "mint" in sol:
        token = Token(client, Pubkey.from_string(sol["mint"]), TOKEN_PROGRAM_ID, payer)
        print("mint (existing):        ", sol["mint"])
    else:
        print("creating test mint (6 decimals) ...")
        token = Token.create_mint(client, payer, payer.pubkey(), 6, TOKEN_PROGRAM_ID, skip_confirmation=False)
        sol["mint"] = str(token.pubkey)
        save_keys(keys)
        print("mint:                   ", token.pubkey)

    payer_ata = token.create_associated_token_account(payer.pubkey(), skip_confirmation=False)
    merchant_ata = token.create_associated_token_account(merchant.pubkey(), skip_confirmation=False)
    print("payer ATA:              ", payer_ata)
    print("merchant ATA:           ", merchant_ata)
    bal = token.get_balance(payer_ata).value.ui_amount or 0
    if bal < 100:
        print("minting test tokens to payer ...")
        token.mint_to(payer_ata, payer, MINT_AMOUNT, opts=TxOpts(skip_confirmation=False))
    print("payer token balance:    ", token.get_balance(payer_ata).value.ui_amount_string)

    print("\nEnvironment for the facilitator (.env):")
    print("HF_SOLANA_ENABLED=true")
    print("HF_SOLANA_NETWORK=solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1")
    print(f"HF_SOLANA_PRIVATE_KEY={fac}")
    print(f"HF_SOLANA_ASSETS={sol['mint']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
