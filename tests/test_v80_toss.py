"""
[v8.0] 토스 이관 회귀 테스트 — run_bot 호환 계층(brokers.compat) + 실시간 캐시 fast path.

잠그는 것:
  - 규칙 7/9: 보유 0 ≠ 조회 실패 (빈 목록 vs None), 수량·외화 조회 실패 = None
  - 규칙 6: KR tot_evlu_amt 는 예수금 포함, KR 금액은 int() 로 읽히는 정수 문자열
  - 규칙 11: US 매수 = 현재가 +1% 지정가 (run_bot 의 1주 판정과 같은 단가)
  - 접수 ≠ 체결: 거부·취소(체결 0)는 rt_cd '1', 부분체결은 체결분만, shadow 는 표시
  - 속도: 웹소켓 체결가·주문 이벤트가 있으면 REST 를 기다리지 않는다
"""
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from modules.brokers import compat, toss_stream  # noqa: E402
from modules.brokers.models import (  # noqa: E402
    AccountSnapshot, Bar, BrokerError, ExecutionMode, Holding, Market, OrderAck, OrderState, Quote,
)


def holding(sym, market, qty, last, avg, pl, rate):
    return Holding(sym, sym, market, "USD" if market == Market.US else "KRW", qty, qty,
                   avg, last, qty * last, pl, rate)


SNAP = AccountSnapshot("1", {"KRW": 1234567.0, "USD": 50.5}, [
    holding("SPY", Market.US, 2.5, 600, 550, 125, 9.09),
    holding("069500", Market.KR, 10, 40000, 38000, 20000, 5.26),
])


class FakeBroker:
    def __init__(self, mode="live", snapshot=SNAP, ack=None, error=None):
        self.execution_mode = ExecutionMode(mode)
        self.base_url = "https://x"
        self.account_seq = "1"
        self.snapshot, self.ack, self.error = snapshot, ack, error
        self.orders, self.requests = [], []

    def authenticate(self, force=False):
        return "t"

    def account_snapshot(self):
        if isinstance(self.snapshot, Exception):
            raise self.snapshot
        return self.snapshot

    def _buying_power(self, currency):
        if isinstance(self.snapshot, Exception):
            raise self.snapshot
        return self.snapshot.cash[currency]

    def quote(self, symbol, market):
        return Quote(symbol, market, "USD", 500.0)

    def bars(self, symbol, market, interval="1d", count=200, before=None):
        return getattr(self, "pages", {}).get(before, [])

    def sellable_quantity(self, symbol):
        if self.error:
            raise self.error
        return 7.9

    def submit_and_wait(self, order, timeout_sec=15.0, poll_sec=0.5):
        self.orders.append(order)
        if self.error:
            raise self.error
        return self.ack or OrderAck("oid", "cid", OrderState.FILLED, order.symbol,
                                    order.quantity, 500.0)

    def _request(self, method, path, **kw):
        self.requests.append((method, path, kw))
        return {"result": {}}


@pytest.fixture
def use(monkeypatch):
    def _use(fake, cls):
        monkeypatch.setattr(compat, "_BROKER", fake)
        return cls()
    return _use


class TestBalance:
    def test_us_empty_is_empty_list_not_none(self, use):
        c = use(FakeBroker(snapshot=AccountSnapshot("1", {"KRW": 0, "USD": 0}, [])), compat.TossOverseas)
        assert c.get_balance()["output1"] == []

    def test_failure_is_none(self, use):
        c = use(FakeBroker(snapshot=BrokerError("down", code="internal-error")), compat.TossOverseas)
        assert c.get_balance() is None
        assert c.get_foreign_balance() is None          # 현금 0 이 아니다

    def test_us_filters_market_and_floors_fractional(self, use):
        rows = use(FakeBroker(), compat.TossOverseas).get_balance()["output1"]
        assert [r["ovrs_pdno"] for r in rows] == ["SPY"]
        assert rows[0]["ovrs_cblc_qty"] == "2" and rows[0]["now_pric2"] == "600"

    def test_kr_total_includes_cash_and_amounts_are_int_strings(self, use):
        bal = use(FakeBroker(), compat.TossDomestic).get_balance()
        o2 = bal["output2"][0]
        assert [r["pdno"] for r in bal["output1"]] == ["069500"]
        assert int(o2["tot_evlu_amt"]) == 400000 + 1234567          # 규칙 6
        for key in ("dnca_tot_amt", "prvs_rcdl_excc_amt", "ord_psbl_cash", "evlu_pfls_smtl_amt"):
            int(o2[key])                                              # 호출부가 int(str) 로 읽는다

    def test_holding_qty(self, use):
        assert use(FakeBroker(), compat.TossDomestic).get_holding_qty("069500") == 7
        assert use(FakeBroker(error=BrokerError("x", code="internal-error")),
                   compat.TossDomestic).get_holding_qty("069500") is None
        assert use(FakeBroker(error=BrokerError("x", code="stock-not-found")),
                   compat.TossDomestic).get_holding_qty("069500") == 0


class TestOrders:
    def test_us_buy_is_limit_plus_one_percent(self, use):
        fake = FakeBroker()
        res = use(fake, compat.TossOverseas).buy_market_order("SPY", 3, "AMS")
        order = fake.orders[-1]
        assert res["rt_cd"] == "0" and res["filled_qty"] == 3 and not res["shadow"]
        assert order.order_type.value == "LIMIT" and order.price == 505.0 and order.quantity == 3

    def test_rejected_or_cancelled_without_fill_is_failure(self, use):
        for state in (OrderState.REJECTED, OrderState.CANCELED):
            ack = OrderAck("oid", "cid", state, "069500", 0.0)
            res = use(FakeBroker(ack=ack), compat.TossDomestic).sell_market_order("069500", 5)
            assert res["rt_cd"] == "1", "접수 후 거부·취소를 체결로 세면 안 된다"

    def test_partial_fill_reports_filled_quantity(self, use):
        ack = OrderAck("oid", "cid", OrderState.CANCELED, "069500", 3.0, 40000.0)   # 3/5 체결 후 잔량 취소
        res = use(FakeBroker(ack=ack), compat.TossDomestic).buy_market_order("069500", 5)
        assert res["rt_cd"] == "0" and res["filled_qty"] == 3 and res["partial"]

    def test_shadow_mode_is_flagged(self, use):
        ack = OrderAck("shadow-x", "x", OrderState.ACCEPTED, "069500")
        res = use(FakeBroker(mode="shadow", ack=ack), compat.TossDomestic).buy_market_order("069500", 2)
        assert res["rt_cd"] == "0" and res["shadow"] and "SHADOW" in res["msg1"]

    def test_broker_error_maps_to_toss_code(self, use):
        res = use(FakeBroker(error=BrokerError("부족", code="insufficient-buying-power")),
                  compat.TossOverseas).sell_market_order("SPY", 1)
        assert res["rt_cd"] == "1" and res["msg_cd"] == "insufficient-buying-power"

    def test_account_restriction_codes_known_to_bot(self):
        import run_bot
        assert run_bot.ACCOUNT_RESTRICTED_CODES == compat.ACCOUNT_RESTRICTED_CODES
        assert {"account-restricted", "prerequisite-required"} <= set(compat.ACCOUNT_RESTRICTED_CODES)

    def test_kr_limit_sell_rounds_down_to_tick(self, use):
        fake = FakeBroker()
        use(fake, compat.TossDomestic).sell_limit_order("069500", 1, 40070.7)
        assert fake.orders[-1].price == 40050


class TestDailyBars:
    def test_us_bar_date_is_new_york_trading_day(self):
        # FAQ: 미국 2026-10-05 거래일 → 2026-10-05T13:00:00+09:00 (서머타임)
        assert compat._bar_date("2026-10-05T13:00:00+09:00", "US") == "20261005"
        assert compat._bar_date("2026-12-07T14:00:00+09:00", "US") == "20261207"
        assert compat._bar_date("2026-10-05T00:00:00+09:00", "KR") == "20261005"

    def test_history_is_oldest_first_and_paginates(self, use):
        def bar(day, close):
            return Bar("069500", Market.KR, "KRW", f"2026-{day}T00:00:00+09:00", close, close, close, close, 1, "1d")
        fake = FakeBroker()
        page1 = [bar("10-02", 3)] + [bar("10-01", 2)] * 199          # 200개 → 다음 페이지 요청
        fake.pages = {None: page1, "2026-10-01T00:00:00+09:00": [bar("10-01", 2), bar("09-30", 1)]}
        rows = use(fake, compat.TossDomestic).get_daily_history("069500", bars=10)
        assert [r["date"] for r in rows] == ["20260930", "20261001", "20261002"]
        assert [r["close"] for r in rows] == [1.0, 2.0, 3.0]


class TestRealtimeFastPath:
    def setup_method(self):
        toss_stream.LIVE_PRICES.clear()
        toss_stream.ORDER_EVENTS.clear()

    def _broker(self, responses):
        # tests/ is not a package in every pytest invocation. Importing the
        # sibling module through the test package therefore fails outside CI.
        from test_toss_broker import Response, Session, broker
        s = Session()
        s.queue(*[Response(r) for r in responses])
        return broker(s, "live"), s

    def test_fresh_streamed_price_skips_rest(self):
        b, s = self._broker([])
        toss_stream.LIVE_PRICES["SPY"] = (512.3, time.monotonic())
        assert b.quote("SPY", Market.US).price == 512.3 and s.calls == []

    def test_stale_streamed_price_falls_back_to_rest(self):
        b, s = self._broker([{"result": [{"symbol": "SPY", "lastPrice": "500", "currency": "USD"}]}])
        toss_stream.LIVE_PRICES["SPY"] = (512.3, time.monotonic() - 60)
        assert b.quote("SPY", Market.US).price == 500.0 and len(s.calls) == 1

    def test_submit_and_wait_takes_order_event_without_polling(self, monkeypatch):
        from modules.brokers.models import OrderRequest, OrderSide, OrderType
        monkeypatch.setattr("modules.live_gate.assert_live_order_allowed", lambda market: None)
        b, s = self._broker([
            {"result": {"orderId": "o1", "clientOrderId": "c1"}},
            {"result": {"orderId": "o1", "symbol": "SPY", "status": "PENDING", "execution": {"filledQuantity": "0"}}},
        ])
        toss_stream.ORDER_EVENTS["o1"] = {"orderId": "o1", "symbol": "SPY", "status": "FILLED",
                                          "execution": {"filledQuantity": "2", "averageFilledPrice": "501"}}
        t0 = time.monotonic()
        ack = b.submit_and_wait(OrderRequest("SPY", Market.US, OrderSide.BUY, OrderType.LIMIT, 2, price=505),
                                timeout_sec=5, poll_sec=2)
        assert ack.state == OrderState.FILLED and ack.filled_quantity == 2
        assert time.monotonic() - t0 < 0.5 and len(s.calls) == 2   # 접수 + 첫 상세 조회뿐

    def test_stream_frames_fill_caches(self):
        trade = toss_stream.parse_frame(json.dumps({"type": "message", "topic": "trade:us:SPY",
                                                    "data": {"price": "601.5", "volume": "1"}}))
        order = toss_stream.parse_frame(json.dumps({"type": "message", "topic": "personal:order:1", "data": {
            "event": "FILL", "order": {"orderId": "z", "symbol": "SPY", "currency": "USD", "status": "FILLED"}}}))
        toss_stream.remember(trade)
        toss_stream.remember(order)
        assert toss_stream.live_price("SPY") == 601.5
        assert toss_stream.ORDER_EVENTS["z"]["status"] == "FILLED"

    def test_websockets_header_kwarg_matches_installed_version(self):
        import inspect
        src = inspect.getsource(toss_stream._connect)
        assert "additional_headers" in src and "extra_headers" in src


def test_empty_execution_mode_env_means_shadow(monkeypatch):
    """2026-10-29 배포 직후 US 세션 크래시: 배포 셸이 EXECUTION_MODE="" 를 봇에 물려줬고
    (load_dotenv 는 기존 환경변수를 덮지 않는다) TossBroker 가 빈 값을 거부했다. 빈 값 = shadow."""
    from modules.brokers.toss import TossBroker
    monkeypatch.setenv("EXECUTION_MODE", "")
    assert TossBroker("c", "s", "1").execution_mode == ExecutionMode.SHADOW
