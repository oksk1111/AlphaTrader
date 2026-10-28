import json

import pytest

from modules.brokers.models import Market
from modules.brokers.toss_stream import (
    TossStreamProtocolError, parse_frame, subscription_declaration,
)


def test_subscription_is_full_replace_and_includes_personal_orders():
    declaration = subscription_declaration(
        [(Market.US, "XLK"), (Market.US, "IWM"), (Market.KR, "069500")],
        account_seq="3", request_id="req-1",
    )
    assert declaration[0] == {"id": "req-1"}
    assert {item["type"] for item in declaration[1:]} == {
        "trade:us", "orderbook:us", "trade:kr", "orderbook:kr", "personal:order",
    }


def test_trade_frame_is_normalized():
    event = parse_frame(json.dumps({
        "type": "message", "topic": "trade:us:XLK",
        "data": {"price": "202.1", "volume": "3", "timestamp": "2026-10-28T22:31:00+09:00", "currency": "USD"},
    }), received_at=10.0)
    assert event.kind == "trade" and event.market == Market.US and event.symbol == "XLK"
    assert event.received_at == 10.0


def test_order_frame_keeps_partial_fill_state():
    event = parse_frame(json.dumps({
        "type": "message", "topic": "personal:order:3",
        "data": {"event": "PARTIAL_FILL", "accountSeq": "3", "order": {
            "orderId": "o1", "symbol": "XLK", "currency": "USD",
            "status": "PARTIAL_FILLED", "orderedAt": "2026-10-28T22:31:00+09:00",
            "execution": {"filledQuantity": "0.5"},
        }},
    }))
    assert event.kind == "order" and event.market == Market.US
    assert event.data["state"] == "PARTIAL_FILLED"


def test_unknown_frame_fails_loudly():
    with pytest.raises(TossStreamProtocolError):
        parse_frame('{"type":"mystery"}')


def test_topic_limit_is_enforced():
    symbols = [(Market.US, f"S{i}") for i in range(51)]
    with pytest.raises(ValueError):
        subscription_declaration(symbols)
