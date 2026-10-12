import threading

from humanapi_facilitator.store import Database, ReplayStore


def test_begin_is_atomic_and_idempotent(tmp_path):
    store = ReplayStore(Database(f"sqlite:///{tmp_path}/a.db"))
    meta = {"rail": "robinhood", "network": "eip155:46630", "payer": "0xabc", "amount": "1"}
    status, row = store.begin("k1", meta)
    assert status == "new" and row.status == "sending"
    status, row = store.begin("k1", meta)
    assert status == "sending"
    store.mark_settled("k1", "0xtx", {"success": True, "transaction": "0xtx"})
    status, row = store.begin("k1", meta)
    assert status == "settled" and row.response["transaction"] == "0xtx"


def test_concurrent_begin_claims_once(tmp_path):
    store = ReplayStore(Database(f"sqlite:///{tmp_path}/b.db"))
    meta = {"rail": "solana", "network": "solana:x"}
    results = []

    def go():
        results.append(store.begin("same", meta)[0])

    threads = [threading.Thread(target=go) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("new") == 1
    assert results.count("sending") == 19


def test_failed_rows_can_retry_and_counts(tmp_path):
    store = ReplayStore(Database(f"sqlite:///{tmp_path}/c.db"))
    meta = {"rail": "robinhood", "network": "eip155:46630"}
    store.begin("k", meta)
    store.mark_failed("k", "insufficient_funds", {"success": False})
    assert store.get("k").status == "failed"
    row = store.retry("k")
    assert row.status == "sending" and row.attempts == 2
    assert store.count_since("robinhood", 0) == 1
    store.mark_uncertain("k", "0xdead", "timeout")
    assert store.unresolved(10**9)[0].tx == "0xdead"


def test_pending_store_protocol(tmp_path):
    from x402.pending_settlement_store import PendingSettlementStore

    store = ReplayStore(Database(f"sqlite:///{tmp_path}/d.db"))
    assert isinstance(store.pending, PendingSettlementStore)
    assert store.pending.get("x") is None
    store.pending.set("x", "0x1")
    assert store.pending.get("x") == "0x1"
    store.pending.set("x", "0x2")
    assert store.pending.get("x") == "0x2"
    store.pending.delete("x")
    assert store.pending.get("x") is None
