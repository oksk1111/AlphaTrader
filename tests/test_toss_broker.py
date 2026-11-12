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


@pytest.fixture(autouse=True)
def live_release(monkeypatch):
    """Normal adapter tests exercise order serialization, not deployment release policy."""
    monkeypatch.setattr("modules.live_gate.assert_live_order_allowed", lambda market: None)


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


def _too_many():
    return Response({"error": {"code": "rate-limit-exceeded", "message": "slow down"}},
                    status=429, headers={"X-Request-Id": "req-1"})


def test_rate_limit_fails_bounded_instead_of_recursing_forever(monkeypatch):
    from modules.brokers import toss
    monkeypatch.setattr(toss.time, "sleep", lambda s: None)
    s = Session()
    s.queue(*[_too_many() for _ in range(len(toss._RATE_LIMIT_BACKOFF) + 1)])
    with pytest.raises(BrokerError) as caught:
        broker(s).quote("XLK", Market.US)
    assert caught.value.code == "rate-limit-exceeded"
    assert caught.value.retryable is True
    assert len(s.calls) == len(toss._RATE_LIMIT_BACKOFF) + 1


def test_get_429_is_retried_then_succeeds(monkeypatch):
    """[v8.2] 2026-10-30 KR: 봇·수집기 합산 호출이 한도를 넘어 일봉 1건이 429 → 방어자산 매수 보류."""
    from modules.brokers import toss
    waits = []
    monkeypatch.setattr(toss.time, "sleep", waits.append)
    s = Session()
    s.queue(_too_many(), Response({"result": [{"symbol": "XLK", "lastPrice": "200"}]}))
    assert broker(s).quote("XLK", Market.US).price == 200
    assert len(s.calls) == 2 and waits and waits[-1] >= 1.0


def test_order_post_429_is_never_retried(monkeypatch):
    """접수됐을 수 있는 주문을 다시 내면 중복 주문이다."""
    from modules.brokers import toss
    monkeypatch.setattr(toss.time, "sleep", lambda s: None)
    s = Session()
    s.queue(_too_many(), _too_many())
    with pytest.raises(BrokerError):
        broker(s)._request("POST", "/api/v1/orders", account=False, body={})
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


# ---------------------------------------------------------------------------
# [v8.5] 토큰 — 재발급은 이전 토큰으로 열린 웹소켓과 다른 프로세스를 끊는다
# ---------------------------------------------------------------------------
def _token_file(tmp_path, monkeypatch):
    from modules.brokers import toss
    path = tmp_path / "token.json"
    monkeypatch.setattr(toss, "TOKEN_FILE", path)
    return path


def _write_token(path, token, ttl):
    import os
    import time
    path.write_text(json.dumps({"access_token": token, "expiry": time.time() + ttl}), encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))   # 파일시스템 mtime 해상도와 무관하게


def _auth_of(call):
    return call[2]["headers"]["Authorization"]


def test_token_reissued_by_another_process_is_adopted_without_a_401(tmp_path, monkeypatch):
    """다른 프로세스가 재발급하면 메모리의 토큰은 이미 무효다 — 파일이 바뀐 것을 보고 바로 갈아탄다."""
    path = _token_file(tmp_path, monkeypatch)
    _write_token(path, "T1", 86400)
    s = Session()
    b = TossBroker("client", "secret", "1", "shadow", s)
    assert b.authenticate() == "T1"
    _write_token(path, "T2", 86400)                      # 수집기·대시보드가 재발급
    assert b.authenticate() == "T2"
    assert s.calls == [], "발급 호출 없이 갈아타야 한다"


def test_401_adopts_the_shared_token_instead_of_reissuing(tmp_path, monkeypatch):
    """401 마다 무조건 재발급하면 방금 다른 프로세스가 받은 토큰을 무효로 만든다 (핑퐁)."""
    path = _token_file(tmp_path, monkeypatch)
    _write_token(path, "T2", 86400)
    s = Session()
    b = TossBroker("client", "secret", "1", "shadow", s)
    b.access_token, b.token_expiry = "T1", 10 ** 12      # 무효가 된 줄 모르는 메모리 토큰
    s.queue(Response({"error": {"code": "invalid-token"}}, status=401),
            Response({"result": [{"symbol": "XLK", "lastPrice": "200"}]}))
    assert b.quote("XLK", Market.US).price == 200
    assert [c[0] for c in s.calls] == ["GET", "GET"], "토큰 발급(POST)이 없어야 한다"
    assert _auth_of(s.calls[0]) == "Bearer T1" and _auth_of(s.calls[1]) == "Bearer T2"


def test_401_on_the_shared_token_itself_reissues_once(tmp_path, monkeypatch):
    path = _token_file(tmp_path, monkeypatch)
    _write_token(path, "T1", 86400)
    s = Session()
    b = TossBroker("client", "secret", "1", "shadow", s)
    s.queue(Response({"error": {"code": "invalid-token"}}, status=401),
            Response({"access_token": "T2", "expires_in": 86400}),
            Response({"result": [{"symbol": "XLK", "lastPrice": "200"}]}))
    assert b.quote("XLK", Market.US).price == 200
    assert [c[0] for c in s.calls] == ["GET", "POST", "GET"]
    assert json.loads(path.read_text(encoding="utf-8"))["access_token"] == "T2"


def test_min_ttl_refreshes_before_the_session_not_at_decision_time(tmp_path, monkeypatch):
    """2026-11-09~11: 만료 갱신이 매일 23:40(US 판단 시각)에 걸려 봇 스트림이 auth-revoked 로 끊겼다.
    세션 시작 때 남은 수명이 세션보다 짧으면 미리 갱신한다."""
    path = _token_file(tmp_path, monkeypatch)
    _write_token(path, "T1", 600)                        # 10분 뒤 만료 — 판단 시각에 걸린다
    s = Session()
    b = TossBroker("client", "secret", "1", "shadow", s)
    assert b.authenticate() == "T1" and s.calls == []    # 평소 호출은 아직 갱신하지 않는다
    s.queue(Response({"access_token": "T2", "expires_in": 86400}))
    assert b.authenticate(min_ttl=7 * 3600) == "T2"
    assert b.authenticate(min_ttl=7 * 3600) == "T2" and len(s.calls) == 1


def test_failed_early_refresh_keeps_the_still_valid_token(tmp_path, monkeypatch):
    path = _token_file(tmp_path, monkeypatch)
    _write_token(path, "T1", 3600)
    s = Session()
    b = TossBroker("client", "secret", "1", "shadow", s)
    s.queue(Response({"error": "temporarily_unavailable"}, status=503))
    assert b.authenticate(min_ttl=7 * 3600) == "T1", "사전 갱신 실패가 세션을 죽이면 안 된다"


def test_candle_budget_is_below_the_rate_that_hit_429_every_morning():
    from modules.brokers import toss
    assert dict(toss._GROUP_TPS)["/api/v1/candles"] <= 10


def test_429_log_carries_rate_limit_headers(monkeypatch, caplog):
    """토스의 실제 한도를 추측하지 않고 로그에서 읽는다."""
    from modules.brokers import toss
    monkeypatch.setattr(toss.time, "sleep", lambda s: None)
    s = Session()
    s.queue(Response({"error": {"code": "rate-limit-exceeded"}}, status=429,
                     headers={"X-RateLimit-Limit": "10", "Retry-After": "1", "X-Request-Id": "r"}),
            Response({"result": [{"symbol": "XLK", "lastPrice": "200"}]}))
    with caplog.at_level("WARNING", logger="toss"):
        broker(s).quote("XLK", Market.US)
    assert "X-RateLimit-Limit" in caplog.text and "X-Request-Id" not in caplog.text
