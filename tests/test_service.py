import time

from conftest import PAYER, TEST_TOKEN, permit2_body

from humanapi_facilitator.service import FacilitatorService


def test_supported_and_root(client):
    r = client.get("/supported")
    assert r.status_code == 200
    kinds = r.json()["kinds"]
    assert kinds[0]["network"] == "eip155:46630" and kinds[0]["scheme"] == "exact"
    assert client.get("/").json()["service"] == "humanapi-facilitator"


def test_verify_policy_before_chain(client, fake):
    r = client.post("/verify", json=permit2_body(amount="5000000"))
    assert r.json() == {"isValid": False, "invalidReason": "amount_exceeds_facilitator_cap"}
    r = client.post("/verify", json=permit2_body(asset="0x2222222222222222222222222222222222222222"))
    assert r.json()["invalidReason"] == "asset_not_allowed"
    assert fake.verify_calls == 0
    r = client.post("/verify", json=permit2_body())
    assert r.json() == {"isValid": True, "payer": PAYER}
    assert fake.verify_calls == 1


def test_verify_rejects_bad_requests(client):
    assert client.post("/verify", json={"x402Version": 1}).json()["error"] == "unsupported_version"
    body = permit2_body()
    body["paymentRequirements"]["network"] = "eip155:8453"
    body["paymentPayload"]["accepted"]["network"] = "eip155:8453"
    assert client.post("/verify", json=body).json()["error"] == "unsupported_network"
    assert client.post("/verify", content=b"nope", headers={"content-type": "application/json"}).status_code == 400


def test_settle_requires_token(client, service):
    service.rails["robinhood"].chain.receipts["0x" + "cd" * 32] = 1
    assert client.post("/settle", json=permit2_body()).status_code == 401
    r = client.post("/settle", json=permit2_body(), headers={"authorization": "Bearer secret"})
    assert r.status_code == 200 and r.json()["success"] is True


def test_settle_twice_settles_once(client, fake, service):
    h = {"authorization": "Bearer secret"}
    service.rails["robinhood"].chain.receipts["0x" + "cd" * 32] = 1
    first = client.post("/settle", json=permit2_body(nonce="1"), headers=h).json()
    second = client.post("/settle", json=permit2_body(nonce="1"), headers=h).json()
    assert first["success"] and first["transaction"] == "0x" + "cd" * 32
    assert first["explorer"].endswith(first["transaction"])
    assert second == first
    assert fake.settle_calls == 1


def test_settle_failure_is_retryable_and_recorded(client, fake, store, service):
    h = {"authorization": "Bearer secret"}
    service.rails["robinhood"].chain.receipts["0x" + "cd" * 32] = 1
    fake.settle_results = [{"success": False, "error_reason": "insufficient_funds", "transaction": ""}]
    r = client.post("/settle", json=permit2_body(nonce="2"), headers=h).json()
    assert r["success"] is False and r["errorReason"] == "insufficient_funds"
    rows = store.db.query("SELECT status, attempts FROM settlements")
    assert rows[0]["status"] == "failed"
    r = client.post("/settle", json=permit2_body(nonce="2"), headers=h).json()
    assert r["success"] is True
    assert store.db.query("SELECT attempts FROM settlements")[0]["attempts"] == 2
    assert fake.settle_calls == 2


def test_concurrent_settle_second_sees_pending(service, fake):
    import threading

    body = permit2_body(nonce="3")
    gate = threading.Event()
    results = []

    class SlowFake:
        def settle(self, p, r):
            gate.wait(5)
            return fake.settle(p, r)

        verify = fake.verify
        get_supported = fake.get_supported

    service.facilitator = SlowFake()
    service.rails["robinhood"].chain.receipts["0x" + "cd" * 32] = 1

    def go():
        results.append(service.settle(body))

    t1 = threading.Thread(target=go)
    t1.start()
    time.sleep(0.3)
    t2 = threading.Thread(target=go)
    t2.start()
    time.sleep(0.3)
    gate.set()
    t1.join()
    t2.join()
    outcomes = sorted(r.get("errorReason", "ok") for r in results)
    assert outcomes == ["ok", "settlement_pending"]
    assert fake.settle_calls == 1


def test_exception_marks_uncertain_and_recovery_uses_nonce_bitmap(service, fake, store):
    fake.raise_on_settle = RuntimeError("rpc exploded")
    r = service.settle(permit2_body(nonce="4"))
    assert r["errorReason"] == "settlement_pending"
    row = store.db.query("SELECT * FROM settlements")[0]
    assert row["status"] == "uncertain" and row["nonce"] == "4"
    # The chain says the Permit2 nonce was consumed: the payment did land.
    service.rails["robinhood"].chain.nonces_used.add((PAYER.lower(), 4))
    assert service.recover() == 1
    fake.raise_on_settle = None
    again = service.settle(permit2_body(nonce="4"))
    assert again["success"] is True and again.get("recovered") is True
    assert fake.settle_calls == 1


def test_recovery_marks_never_landed_after_deadline(service, fake, store):
    fake.raise_on_settle = RuntimeError("boom")
    service.settle(permit2_body(nonce="5", deadline=int(time.time()) - 3600))
    assert service.recover() == 1
    assert store.db.query("SELECT status, error FROM settlements")[0]["error"] == "never_landed"


def test_confirmation_timeout_returns_pending_then_recovers(service, fake, store, settings):
    settings.settle_timeout_seconds = 1
    r = service.settle(permit2_body(nonce="6"))
    assert r["errorReason"] == "settlement_pending" and r["transaction"] == "0x" + "cd" * 32
    service.rails["robinhood"].chain.receipts["0x" + "cd" * 32] = 1
    assert service.recover() == 1
    assert service.settle(permit2_body(nonce="6"))["success"] is True


def test_daily_cap(service, fake, settings):
    settings.daily_settlement_cap = 1
    service.rails["robinhood"].chain.receipts["0x" + "cd" * 32] = 1
    assert service.settle(permit2_body(nonce="7"))["success"] is True
    assert service.settle(permit2_body(nonce="8"))["errorReason"] == "daily_settlement_cap_reached"


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] and data["rails"]["robinhood"]["reachable"] and data["rails"]["robinhood"]["lowGas"] is False
    assert data["rails"]["robinhood"]["allowedAssets"] == [TEST_TOKEN]


def test_settings_validation():
    import pytest

    from humanapi_facilitator.config import Settings

    with pytest.raises(ValueError):
        Settings(env="mainnet", robinhood_enabled=True, robinhood_private_key="0x" + "11" * 32, robinhood_network="eip155:46630", settle_token="x", _env_file=None)
    s = Settings(env="testnet", robinhood_enabled=True, robinhood_private_key="0x" + "11" * 32, _env_file=None)
    assert s.robinhood_rpc_url.startswith("https://rpc.testnet")
    assert s.allowed_assets("eip155:46630") is None
    assert s.allowed_assets("eip155:4663") == ["0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"]


def test_service_type():
    assert FacilitatorService


def test_gas_grant_is_capped_per_wallet_and_per_day(service, settings):
    from humanapi_facilitator.service import RequestError

    settings.robinhood_private_key = "0x" + "11" * 32
    sent = []
    service._send_native = lambda rail, to, amount: (sent.append((to, amount)), "0xgas")[1]
    from types import SimpleNamespace

    rail = service.rails["robinhood"]
    balances: dict[str, int] = {}
    rail.chain = SimpleNamespace(eth=SimpleNamespace(get_balance=lambda a: balances.get(a, 0)))
    rail.native_balance = lambda: 10**18
    w = "0x" + "ab" * 20
    r = service.gas_grant({"address": w})
    assert r["granted"] and r["transaction"] == "0xgas" and len(sent) == 1 and sent[0][1] == settings.gas_grant_wei
    try:
        service.gas_grant({"address": w})
        raise AssertionError("second grant in a day must be refused")
    except RequestError as e:
        assert e.reason == "already_granted"
    try:
        service.gas_grant({"address": "nope"})
        raise AssertionError("bad address must be refused")
    except RequestError as e:
        assert e.status == 400
    balances[sent[0][0]] = settings.gas_grant_wei
    assert service.gas_grant({"address": "0x" + "cd" * 20})["granted"]  # another wallet is fine
    settings.gas_grants_per_day = 2
    try:
        service.gas_grant({"address": "0x" + "ef" * 20})
        raise AssertionError("daily cap must hold")
    except RequestError as e:
        assert e.reason == "daily_cap"
