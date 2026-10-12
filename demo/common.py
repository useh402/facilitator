"""Shared bits for the demo scripts: where the throwaway keys live and how to load them."""

from __future__ import annotations

import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEYS_FILE = HERE / ".keys.json"  # gitignored; throwaway test keys only


def load_keys() -> dict:
    if not KEYS_FILE.exists():
        return {}
    return json.loads(KEYS_FILE.read_text())


def save_keys(data: dict) -> None:
    KEYS_FILE.write_text(json.dumps(data, indent=2))
    os.chmod(KEYS_FILE, 0o600)


FACILITATOR_URL = os.environ.get("DEMO_FACILITATOR_URL", "http://127.0.0.1:8402")
RESOURCE_URL = os.environ.get("DEMO_RESOURCE_URL", "http://127.0.0.1:8403")
SETTLE_TOKEN = os.environ.get("HF_SETTLE_TOKEN", "demo-token")
