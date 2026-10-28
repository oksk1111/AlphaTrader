import json

import pytest

from modules.brokers.models import (
    BrokerError, ExecutionMode, Market, OrderRequest, OrderSide, OrderState, OrderType,
)
from modules.brokers.toss import TossBroker


class Response:
    def __init__(self, body, status=200, headers=None):
        self._body = body
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._body


class Session:
    def __init__(self):
        self.calls = []
        self.responses = []

    def queue(self, *responses):
        self.responses.extend(responses)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def broker(session, mode="shadow"):
    b = TossBroker("client", "secret", "1", mode, session)
    b.access_token = "token"
    b.token_expiry = 10 ** 12
    return b


def test_live_mode_rejects_nonofficial_base_url(monkeypatch):
    monkeypatch.setenv("TOSS_BASE_URL", "https://example.test")
    with pytest.raises(ValueError):
        TossBroker("client", "secret", "1", "live", Session())


def test_paper_mode_is_rejected_because_toss_has_no_documented_sandbox():
    with pytest.raises(ValueError):
        TossBroker("client", "secret", "1", "paper", Session())


def test_authentication_reuses_shared_token_cache(tmp_path, monkeypatch):
    from modules.brokers import toss
    monkeypatch.setattr(toss, "TOKEN_FILE", tmp_path / "token.json")
    first_session = Session()
    first_session.queue(Response({"access_token": "shared", "expires_in": 86400}))
    assert TossBroker("client", "secret", "1", "shadow", first_session).authenticate() == "shared"
    second_session = Session()
    assert TossBroker("client", "secret", "1", "shadow", second_session).authenticate() == "shared"
    assert second_session.calls == []


def test_empty_holdings_is_success_not_failure():
    s = Session()
    s.queue(
        Response({"result": {
            "marketValue": {"amount": {"krw": "0", "usd": None}},
            "profitLoss": {"amount": {"krw": "0", "usd": None}},
            "items": [],
        }}),
        Response({"result": {"currency": "KRW", "cashBuyingPower": "0"}}),
        Response({"result": {"currency": "USD", "cashBuyingPower": "586.12"}}),
    )
    snap = broker(s).account_snapshot()
    assert snap.holdings == []
    assert snap.cash_for(Market.KR) == 0
    assert snap.cash_for(Market.US) == pytest.approx(586.12)


def test_holdings_are_normalized_and_markets_stay_separate():
    s = Session()
    s.queue(
        Response({"result": {
            "marketValue": {"amount": {"krw": "720000", "usd": "205.50"}},
            "profitLoss": {"amount": {"krw": "20000", "usd": "5.50"}},
            "items": [{
                "symbol": "XLK", "name": "Technology Select Sector SPDR Fund",
                "marketCountry": "US", "currency": "USD", "quantity": "1",
                "lastPrice": "205.50", "averagePurchasePrice": "200",
                "marketValue": {"amount": "205.50"},
                "profitLoss": {"amount": "5.50", "rate": "0.0275"},
            }],
        }}),
        Response({"result": {"currency": "KRW", "cashBuyingPower": "1000000"}}),
        Response({"result": {"currency": "USD", "cashBuyingPower": "123.45"}}),
    )
    snap = broker(s).account_snapshot()
    holding = snap.holdings_for(Market.US)[0]
    assert holding.symbol == "XLK" and holding.quantity == 1
    assert holding.profit_rate_pct == pytest.approx(2.75)
    assert snap.cash_for(Market.KR) == 1_000_000
    assert snap.cash_for(Market.US) == pytest.approx(123.45)


def test_shadow_order_never_calls_api():
    s = Session()
    b = broker(s, "shadow")
    ack = b.submit_order(OrderRequest(
        symbol="XLK", market=Market.US, side=OrderSide.BUY,
        order_type=OrderType.MARKET, amount=50,
        client_order_id="shadow-1",
    ))
    assert ack.state == OrderState.ACCEPTED
    assert ack.order_id == "shadow-shadow-1"
    assert s.calls == []


def test_live_order_is_accepted_not_falsely_filled():
    s = Session()
    s.queue(Response({"result": {"orderId": "order-1", "clientOrderId": "client-1"}}))
    b = broker(s, "live")
    ack = b.submit_order(OrderRequest(
        symbol="005930", market=Market.KR, side=OrderSide.BUY,
        order_type=OrderType.LIMIT, quantity=1, price=70000,
        client_order_id="client-1",
    ))
    assert ack.state == OrderState.ACCEPTED
    body = s.calls[0][2]["json"]
    assert body == {
        "clientOrderId": "client-1", "symbol": "005930", "side": "BUY",
        "orderType": "LIMIT", "timeInForce": "DAY", "quantity": "1",
        "price": "70000",
    }


def test_submit_and_wait_returns_only_confirmed_fill():
    s = Session()
    s.queue(
        Response({"result": {"orderId": "order-1", "clientOrderId": "client-1"}}),
        Response({"result": {"orderId": "order-1", "symbol": "005930", "status": "PENDING",
                              "execution": {"filledQuantity": "0"}}}),
        Response({"result": {"orderId": "order-1", "symbol": "005930", "status": "FILLED",
                              "execution": {"filledQuantity": "1", "averageFilledPrice": "70000"}}}),
    )
    b = broker(s, "live")
    ack = b.submit_and_wait(OrderRequest(
        symbol="005930", market=Market.KR, side=OrderSide.BUY,
        order_type=OrderType.LIMIT, quantity=1, price=70000,
        client_order_id="client-1",
    ), timeout_sec=1, poll_sec=.01)
    assert ack.state == OrderState.FILLED
    assert ack.filled_quantity == 1


def test_submit_and_wait_cancels_unfilled_order_on_timeout(monkeypatch):
    s = Session()
    s.queue(
        Response({"result": {"orderId": "order-1", "clientOrderId": "client-1"}}),
        Response({"result": {"orderId": "order-1", "symbol": "XLK", "status": "PENDING",
                              "execution": {"filledQuantity": "0"}}}),
        Response({"result": {}}),
        Response({"result": {"orderId": "order-1", "symbol": "XLK", "status": "CANCELED",
                              "execution": {"filledQuantity": "0"}}}),
    )
    b = broker(s, "live")
    ack = b.submit_and_wait(OrderRequest(
        symbol="XLK", market=Market.US, side=OrderSide.BUY,
        order_type=OrderType.LIMIT, quantity=1, price=200,
        client_order_id="client-1",
    ), timeout_sec=0)
    assert ack.state == OrderState.CANCELED


def test_order_detail_distinguishes_partial_and_filled():
    for status, expected in [("PARTIAL_FILLED", OrderState.PARTIAL_FILLED),
                             ("FILLED", OrderState.FILLED)]:
        s = Session()
        s.queue(Response({"result": {
            "orderId": "order-1", "symbol": "XLK", "status": status,
            "execution": {"filledQuantity": "0.5", "averageFilledPrice": "200.25"},
        }}))
        ack = broker(s, "live").order("order-1")
        assert ack.state == expected
        assert ack.filled_quantity == pytest.approx(0.5)
        assert ack.average_filled_price == pytest.approx(200.25)


def test_rate_limit_fails_bounded_instead_of_recursing_forever():
    s = Session()
    s.queue(Response({"error": {"code": "rate-limit-exceeded", "message": "slow down"}},
                     status=429, headers={"X-Request-Id": "req-1"}))
    with pytest.raises(BrokerError) as caught:
        broker(s).quote("XLK", Market.US)
    assert caught.value.code == "rate-limit-exceeded"
    assert caught.value.retryable is True
    assert len(s.calls) == 1


def test_order_request_rejects_invalid_amount_order():
    request = OrderRequest(
        symbol="005930", market=Market.KR, side=OrderSide.BUY,
        order_type=OrderType.MARKET, amount=100000,
    )
    with pytest.raises(ValueError):
        request.validate()


def test_oauth2_string_error_surfaces_real_cause(monkeypatch, tmp_path):
    """토큰 발급 실패는 OAuth2 형식 {"error": "access_denied"} — 문자열 error 를 dict 로 읽어
    AttributeError 로 원인을 가리면 안 된다 (2026-10-29 실서버)."""
    from modules.brokers import toss
    monkeypatch.setattr(toss, "TOKEN_FILE", tmp_path / "token.json")
    s = Session()
    s.queue(Response({"error": "access_denied", "error_description": "IP not allowed"}, status=403))
    with pytest.raises(BrokerError) as caught:
        TossBroker("client", "secret", "1", "shadow", s).authenticate()
    assert caught.value.code == "access_denied" and "IP not allowed" in str(caught.value)
