# H402 facilitator

One Python service that is an x402 facilitator for two rails:

| Rail | Network (CAIP-2) | Asset | Route |
| --- | --- | --- | --- |
| Robinhood Chain | `eip155:4663` mainnet, `eip155:46630` testnet | USDG `0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168` (6 decimals) | `exact`, Permit2 through the canonical x402ExactPermit2Proxy `0x402085c248EeA27D92E8b30b2C58ed07f9E20001` (live on both chains) |
| Solana | `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp` mainnet, `solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1` devnet | USDC `EPjFWdd5AufqSSqeM4qv9bL4dqAfv7PF9Vm7GdWRLd3f` | `exact`, SVM; this service is the fee payer |

Verification and settlement follow the x402 v2 specification through the official `x402` Python SDK (2.25.0).
This service adds what a facilitator in production needs around it:

- `POST /settle` is idempotent: one settlement per authorization, ever. A second call with the same payload returns the first result; a concurrent call gets `settlement_pending`.
- Durable replay store (SQLite or Postgres), never memory only.
- Crash recovery: rows left in flight are resolved from the chain on startup and every minute (receipt lookup, the Permit2 nonce bitmap on Robinhood Chain, the precomputed transaction signature on Solana).
- Policy before the chain is touched: allowed assets per rail, a per-settlement amount cap, a daily settlement cap, bearer auth on `/settle`, a per-IP rate limit.
- Confirmation depth on Robinhood Chain (3 blocks by default) before a settlement is reported as final.
- `GET /health` shows chain reachability, signer addresses and native balances with a low-gas flag, and the store's status counts.
- Structured JSON logs with payer, amount, outcome and latency; never a signature or key.

## Endpoints

| Method and path | Purpose |
| --- | --- |
| `GET /supported` | Kinds this facilitator handles, with the Solana fee payer in `extra` |
| `POST /verify` | `{ x402Version: 2, paymentPayload, paymentRequirements }` → `{ isValid, invalidReason?, payer }` |
| `POST /settle` | Same body, `Authorization: Bearer <HF_SETTLE_TOKEN>` → `{ success, transaction, network, payer, explorer }` or `{ success: false, errorReason }` |
| `GET /health` | 200 when every rail and the database answer, else 503 |

## Run

```bash
git clone https://github.com/useh402/facilitator && cd facilitator
uv sync --extra dev          # Python 3.12 is pinned; uv installs it
cp .env.example .env         # fill in keys and networks
uv run humanapi-facilitator  # http://127.0.0.1:8402
uv run pytest                # unit tests, no network
```

Docker: `docker build -t humanapi-facilitator . && docker run --env-file .env -p 8402:8402 humanapi-facilitator`.

Postgres: `uv sync --extra postgres` and set `HF_DATABASE_URL=postgresql://...`.

## Demo: the full 402 round trip

`demo/server.py` is a paywalled resource that asks this facilitator to verify and settle; `demo/client_*.py` pay for it.

1. Solana devnet: `uv run python demo/setup_devnet.py` creates throwaway keys, requests SOL airdrops, creates a 6-decimal test mint and token accounts, and prints the `HF_SOLANA_*` lines for `.env`. If the public airdrop is rate limited, fund the two printed addresses at https://faucet.solana.com and rerun.
2. Robinhood Chain testnet: `uv run python demo/setup_robinhood.py` creates keys and prints two addresses; claim test ETH for both at https://faucet.testnet.chain.robinhood.com, rerun, and it deploys `demo/TestUSD.sol` (a 6-decimal ERC-20, bytecode in `demo/TestUSD.bin`), mints to the payer and approves Permit2 once.
3. Start the facilitator with the printed env, then `uv run python demo/server.py` in a second terminal.
4. `uv run python demo/client_solana.py` and `uv run python demo/client_robinhood.py`: each prints the 200 body and the decoded `PAYMENT-RESPONSE` header with the transaction, visible on Solscan (devnet) and the Robinhood testnet explorer.

Keys written by the setup scripts live in `demo/.keys.json` (gitignored). They are test keys; never reuse them.

## Configuration

See `.env.example`. Every variable is prefixed `HF_`. `HF_ENV=mainnet` refuses to start unless the networks are the mainnets, a settle token is set, and allowed assets resolve (USDG and USDC by default).

## Layout

```text
humanapi_facilitator/
  config.py     settings and network constants
  rails.py      builds the SDK facilitator per rail; read-only chain helpers (receipts, nonce bitmap, expected signature)
  service.py    policy, idempotent settle, confirmations, recovery, health
  store.py      replay store (SQLite/Postgres) and the SDK pending-settlement store
  keys.py       deterministic key per authorization
  app.py        FastAPI endpoints, auth, rate limit, recovery loop
tests/          unit tests with a fake facilitator and fake chain
demo/           paywalled server, clients, testnet setup scripts
```
