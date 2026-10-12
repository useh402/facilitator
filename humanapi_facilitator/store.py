"""Durable replay and settlement store.

One row per payment authorization (keyed by a hash of the authorization, see keys.py).
Statuses:
  sending    we are working on it (verify, broadcast, confirmation)
  settled    confirmed on chain; response_json holds the SettleResponse we returned
  failed     rejected or reverted before value moved; a retry is allowed
  uncertain  a transaction may have been broadcast and we lost track; recovery resolves it

SQLite by default (one process). Postgres via psycopg when the URL starts with postgres.
Also implements the x402 SDK's PendingSettlementStore protocol in `.pending`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS gas_grants (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      at REAL NOT NULL,
      rail TEXT NOT NULL,
      address TEXT NOT NULL,
      amount TEXT NOT NULL,
      tx TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS gas_grants_address ON gas_grants (rail, address, at)",
    """
    CREATE TABLE IF NOT EXISTS settlements (
      key TEXT PRIMARY KEY,
      rail TEXT NOT NULL,
      network TEXT NOT NULL,
      payer TEXT,
      amount TEXT,
      asset TEXT,
      pay_to TEXT,
      nonce TEXT,
      deadline INTEGER,
      status TEXT NOT NULL,
      tx TEXT,
      error TEXT,
      response_json TEXT,
      attempts INTEGER NOT NULL DEFAULT 1,
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS settlements_status ON settlements (status)",
    "CREATE INDEX IF NOT EXISTS settlements_rail_created ON settlements (rail, created_at)",
    """
    CREATE TABLE IF NOT EXISTS pending (
      key TEXT PRIMARY KEY,
      tx TEXT NOT NULL,
      created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      at REAL NOT NULL,
      kind TEXT NOT NULL,
      key TEXT,
      rail TEXT,
      network TEXT,
      payer TEXT,
      amount TEXT,
      outcome TEXT,
      latency_ms REAL
    )
    """,
]


@dataclass
class Row:
    key: str
    rail: str
    network: str
    payer: str | None
    amount: str | None
    asset: str | None
    pay_to: str | None
    nonce: str | None
    deadline: int | None
    status: str
    tx: str | None
    error: str | None
    response_json: str | None
    attempts: int
    created_at: float
    updated_at: float

    @property
    def response(self) -> dict[str, Any] | None:
        return json.loads(self.response_json) if self.response_json else None


class Database:
    """Minimal dialect shim: one connection, one lock, `?` placeholders."""

    def __init__(self, url: str):
        self.url = url
        self.lock = threading.RLock()
        if url.startswith("sqlite"):
            path = url.split("///", 1)[1] if "///" in url else url.split("://", 1)[1]
            self.kind = "sqlite"
            self.conn = sqlite3.connect(path or ":memory:", check_same_thread=False, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            if path and path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
        elif url.startswith("postgres"):
            import psycopg
            from psycopg.rows import dict_row

            self.kind = "postgres"
            self.conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        else:
            raise ValueError("HF_DATABASE_URL must start with sqlite or postgres")
        for stmt in SCHEMA:
            self.execute(self._pg(stmt))

    def _pg(self, sql: str) -> str:
        if self.kind == "postgres":
            sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
            sql = sql.replace("INSERT OR IGNORE", "INSERT")
            if sql.lstrip().startswith("INSERT") and "ON CONFLICT" not in sql and "settlements" in sql:
                sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
            sql = sql.replace("?", "%s")
        return sql

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self.lock:
            cur = self.conn.execute(self._pg(sql), params)
            return cur.rowcount

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.lock:
            cur = self.conn.execute(self._pg(sql), params)
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def close(self) -> None:
        with self.lock:
            self.conn.close()


class PendingStore:
    """x402 SDK PendingSettlementStore backed by the database (5-minute TTL)."""

    TTL = 300.0

    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str) -> str | None:
        self.db.execute("DELETE FROM pending WHERE created_at < ?", (time.time() - self.TTL,))
        rows = self.db.query("SELECT tx FROM pending WHERE key = ?", (key,))
        return rows[0]["tx"] if rows else None

    def set(self, key: str, tx_hash: str) -> None:
        self.db.execute("DELETE FROM pending WHERE key = ?", (key,))
        self.db.execute("INSERT INTO pending (key, tx, created_at) VALUES (?, ?, ?)", (key, tx_hash, time.time()))

    def delete(self, key: str) -> None:
        self.db.execute("DELETE FROM pending WHERE key = ?", (key,))


class ReplayStore:
    def __init__(self, db: Database):
        self.db = db
        self.pending = PendingStore(db)

    # -- lifecycle -----------------------------------------------------------------------------

    def begin(self, key: str, meta: dict[str, Any]) -> tuple[str, Row]:
        """Claim a key. Returns ("new", row) when this call owns the settlement, else the existing status."""
        now = time.time()
        with self.db.lock:
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO settlements (key, rail, network, payer, amount, asset, pay_to, nonce, deadline,"
                " status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'sending', ?, ?)",
                (
                    key,
                    meta.get("rail", ""),
                    meta.get("network", ""),
                    meta.get("payer"),
                    meta.get("amount"),
                    meta.get("asset"),
                    meta.get("pay_to"),
                    meta.get("nonce"),
                    meta.get("deadline"),
                    now,
                    now,
                ),
            )
            row = self.get(key)
            assert row is not None
            if inserted:
                return "new", row
            return row.status, row

    def retry(self, key: str) -> Row:
        """Move a failed row back to sending for another attempt."""
        self.db.execute(
            "UPDATE settlements SET status = 'sending', error = NULL, attempts = attempts + 1, updated_at = ?"
            " WHERE key = ? AND status = 'failed'",
            (time.time(), key),
        )
        row = self.get(key)
        assert row is not None
        return row

    def set_tx(self, key: str, tx: str) -> None:
        self.db.execute("UPDATE settlements SET tx = ?, updated_at = ? WHERE key = ?", (tx, time.time(), key))

    def mark_settled(self, key: str, tx: str, response: dict[str, Any]) -> None:
        self.db.execute(
            "UPDATE settlements SET status = 'settled', tx = ?, error = NULL, response_json = ?, updated_at = ?"
            " WHERE key = ?",
            (tx, json.dumps(response), time.time(), key),
        )

    def mark_failed(self, key: str, error: str, response: dict[str, Any] | None = None) -> None:
        self.db.execute(
            "UPDATE settlements SET status = 'failed', error = ?, response_json = ?, updated_at = ? WHERE key = ?",
            (error, json.dumps(response) if response else None, time.time(), key),
        )

    def mark_uncertain(self, key: str, tx: str | None, error: str) -> None:
        self.db.execute(
            "UPDATE settlements SET status = 'uncertain', tx = COALESCE(?, tx), error = ?, updated_at = ?"
            " WHERE key = ?",
            (tx or None, error, time.time(), key),
        )

    # -- reads ---------------------------------------------------------------------------------

    def get(self, key: str) -> Row | None:
        rows = self.db.query("SELECT * FROM settlements WHERE key = ?", (key,))
        return Row(**rows[0]) if rows else None

    def unresolved(self, stuck_after: float) -> list[Row]:
        cutoff = time.time() - stuck_after
        rows = self.db.query(
            "SELECT * FROM settlements WHERE status = 'uncertain' OR (status = 'sending' AND updated_at < ?)"
            " ORDER BY created_at",
            (cutoff,),
        )
        return [Row(**r) for r in rows]

    def count_since(
        self,
        rail: str,
        since: float,
        statuses: tuple[str, ...] = ("sending", "settled", "uncertain"),
        exclude_key: str | None = None,
    ) -> int:
        marks = ",".join("?" for _ in statuses)
        rows = self.db.query(
            f"SELECT COUNT(*) AS n FROM settlements WHERE rail = ? AND created_at >= ? AND status IN ({marks})"
            " AND key <> ?",
            (rail, since, *statuses, exclude_key or ""),
        )
        return int(rows[0]["n"])

    def status_counts(self) -> dict[str, int]:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM settlements GROUP BY status")
        return {r["status"]: int(r["n"]) for r in rows}

    # -- audit ---------------------------------------------------------------------------------

    def event(self, kind: str, **fields: Any) -> None:
        self.db.execute(
            "INSERT INTO events (at, kind, key, rail, network, payer, amount, outcome, latency_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                kind,
                fields.get("key"),
                fields.get("rail"),
                fields.get("network"),
                fields.get("payer"),
                fields.get("amount"),
                fields.get("outcome"),
                fields.get("latency_ms"),
            ),
        )

    # -- gas grants -------------------------------------------------------------------------------

    def gas_grants_since(self, rail: str, since: float, address: str | None = None) -> int:
        if address is None:
            rows = self.db.query("SELECT COUNT(*) AS n FROM gas_grants WHERE rail = ? AND at >= ?", (rail, since))
        else:
            rows = self.db.query("SELECT COUNT(*) AS n FROM gas_grants WHERE rail = ? AND at >= ? AND address = ?", (rail, since, address.lower()))
        return int(rows[0]["n"])

    def record_gas_grant(self, rail: str, address: str, amount: int, tx: str) -> None:
        self.db.execute("INSERT INTO gas_grants (at, rail, address, amount, tx) VALUES (?, ?, ?, ?, ?)", (time.time(), rail, address.lower(), str(amount), tx))
