"""Environment-driven settings. Every variable is prefixed HF_ (H402 Facilitator)."""

from __future__ import annotations

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROBINHOOD_MAINNET = "eip155:4663"
ROBINHOOD_TESTNET = "eip155:46630"
SOLANA_MAINNET = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
SOLANA_DEVNET = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"

USDG_ROBINHOOD_MAINNET = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
USDC_SOLANA_MAINNET = "EPjFWdd5AufqSSqeM4qv9bL4dqAfv7PF9Vm7GdWRLd3f"
USDC_SOLANA_DEVNET = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"

DEFAULT_RPC = {
    ROBINHOOD_MAINNET: "https://rpc.mainnet.chain.robinhood.com",
    ROBINHOOD_TESTNET: "https://rpc.testnet.chain.robinhood.com",
    SOLANA_MAINNET: "https://api.mainnet-beta.solana.com",
    SOLANA_DEVNET: "https://api.devnet.solana.com",
}

DEFAULT_ASSETS = {
    ROBINHOOD_MAINNET: [USDG_ROBINHOOD_MAINNET],
    SOLANA_MAINNET: [USDC_SOLANA_MAINNET],
    SOLANA_DEVNET: [USDC_SOLANA_DEVNET],
}

EXPLORER_TX = {
    ROBINHOOD_MAINNET: "https://robin.etherscan.io/tx/{tx}",
    ROBINHOOD_TESTNET: "https://testnet.robin.etherscan.io/tx/{tx}",
    SOLANA_MAINNET: "https://solscan.io/tx/{tx}",
    SOLANA_DEVNET: "https://solscan.io/tx/{tx}?cluster=devnet",
}


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HF_", env_file=".env", extra="ignore")

    env: str = "local"  # local | testnet | mainnet
    host: str = "0.0.0.0"
    port: int = 8402
    log_level: str = "INFO"

    # sqlite:///./facilitator.db or postgresql://user:pass@host/db
    database_url: str = "sqlite:///./facilitator.db"

    # Bearer token required on POST /settle (the only endpoint that spends gas). Empty = open.
    settle_token: str = ""
    rate_limit_per_minute: int = 120
    # Seconds to wait for a receipt or confirmation before answering settlement_pending.
    settle_timeout_seconds: float = 90.0
    # Rows stuck in `sending` longer than this are treated as uncertain and recovered.
    stuck_after_seconds: float = 180.0
    daily_settlement_cap: int = 5000

    # Robinhood Chain rail (USDG through the x402 Permit2 proxy)
    robinhood_enabled: bool = False
    robinhood_network: str = ROBINHOOD_TESTNET
    robinhood_rpc_url: str = ""
    robinhood_private_key: str = ""
    robinhood_assets: str = ""  # comma separated token addresses; empty = defaults for the network
    robinhood_confirmations: int = 3
    robinhood_max_amount: int = 1_000_000_000  # atomic units (6 decimals): 1000 USDG
    robinhood_min_gas_wei: int = 10**16  # health warns below this native balance
    # Gas grants: a tiny amount of the native token sent to a worker's wallet so they can move their rewards.
    gas_grant_wei: int = 20_000_000_000_000  # 0.00002 ETH, enough for a few token transfers on Robinhood Chain
    gas_grants_per_day: int = 300
    gas_grant_hours_between: int = 24  # per wallet

    # Solana rail (USDC through the SVM exact scheme, facilitator is the fee payer)
    solana_enabled: bool = False
    solana_network: str = SOLANA_DEVNET
    solana_rpc_url: str = ""
    solana_private_key: str = ""  # base58 64-byte keypair
    solana_assets: str = ""
    solana_max_amount: int = 1_000_000_000
    solana_min_lamports: int = 50_000_000  # 0.05 SOL

    @field_validator("env")
    @classmethod
    def _env(cls, v: str) -> str:
        if v not in {"local", "testnet", "mainnet"}:
            raise ValueError("HF_ENV must be local, testnet or mainnet")
        return v

    @model_validator(mode="after")
    def _fill_defaults(self) -> Settings:
        if self.robinhood_enabled:
            if not self.robinhood_private_key:
                raise ValueError("HF_ROBINHOOD_PRIVATE_KEY is required when the Robinhood rail is enabled")
            if not self.robinhood_rpc_url:
                self.robinhood_rpc_url = DEFAULT_RPC.get(self.robinhood_network, "")
            if not self.robinhood_rpc_url:
                raise ValueError("HF_ROBINHOOD_RPC_URL is required for this network")
        if self.solana_enabled:
            if not self.solana_private_key:
                raise ValueError("HF_SOLANA_PRIVATE_KEY is required when the Solana rail is enabled")
            if not self.solana_rpc_url:
                self.solana_rpc_url = DEFAULT_RPC.get(self.solana_network, "")
            if not self.solana_rpc_url:
                raise ValueError("HF_SOLANA_RPC_URL is required for this network")
        if self.env == "mainnet":
            if self.robinhood_enabled and self.robinhood_network != ROBINHOOD_MAINNET:
                raise ValueError("HF_ENV=mainnet but the Robinhood network is not eip155:4663")
            if self.solana_enabled and self.solana_network != SOLANA_MAINNET:
                raise ValueError("HF_ENV=mainnet but the Solana network is not mainnet")
            if not self.settle_token:
                raise ValueError("HF_SETTLE_TOKEN is required on mainnet")
        return self

    def allowed_assets(self, network: str) -> list[str] | None:
        """Allowed asset identifiers for a network. None means any asset (never on mainnet)."""
        configured = self.robinhood_assets if network.startswith("eip155:") else self.solana_assets
        if configured:
            return _split(configured)
        if network in DEFAULT_ASSETS:
            return list(DEFAULT_ASSETS[network])
        if self.env == "mainnet":
            raise ValueError(f"no allowed assets configured for {network}")
        return None
