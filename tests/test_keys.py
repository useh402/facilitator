from conftest import permit2_body
from x402.schemas import parse_payment_payload, parse_payment_requirements

from humanapi_facilitator.keys import payment_key, summarize


def _parse(body):
    return parse_payment_payload(body["paymentPayload"]), parse_payment_requirements(2, body["paymentRequirements"])


def test_key_depends_on_payer_and_nonce_only():
    a = summarize(*_parse(permit2_body(nonce="7")))
    b = summarize(*_parse(permit2_body(nonce="0x7", amount="99")))
    c = summarize(*_parse(permit2_body(nonce="8")))
    assert a["payer"] == b["payer"] and a["nonce"] == "7" == b["nonce"]
    assert payment_key(a) == payment_key(b)
    assert payment_key(a) != payment_key(c)
    assert a["kind"] == "permit2" and a["deadline"] == 4102444800
