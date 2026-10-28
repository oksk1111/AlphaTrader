"""
[v8.0] 토스증권 어댑터 회귀 테스트 — 네트워크 없이 TossSession.request 를 대역으로 바꿔 검증한다.

잠그는 것:
  - 규칙 7/9: 보유 0 ≠ 조회 실패 (빈 목록 vs None), 수량·외화 조회 실패 = None
  - 규칙 6: KR tot_evlu_amt 는 예수금을 포함, KR 금액은 int() 로 읽히는 정수 문자열
  - 규칙 11: US 매수 = 현재가 +1% 지정가 (run_bot 의 1주 판정과 같은 단가)
  - 주문 실패 → rt_cd '1' + 토스 에러코드(msg_cd), 계좌 제한 코드는 run_bot 이 인식
  - 속도: 웹소켓 체결가·주문 이벤트가 있으면 REST 를 부르지 않는다
  - 토큰: 다른 프로세스가 새로 받은 토큰을 재사용 (서로 revoke 하는 루프 방지)
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from modules import toss_api  # noqa: E402
from modules.toss_api import TossApiError, TossDomestic, TossOverseas  # noqa: E402


class FakeSession:
    def __init__(self, routes):
        self.routes = routes          # path -> result | Exception | callable(params, body)
        self.calls = []
        self.base_url = "https://x"

    def request(self, method, path, group, params=None, body=None, account=False, idempotent=None):
        self.calls.append((method, path, params, body))
        r = self.routes.get(path)
        if isinstance(r, Exception):
            raise r
        return r(params, body) if callable(r) else r

    def token(self):
        return "t"


def client(cls, routes):
    c = cls.__new__(cls)
    c.s = FakeSession(routes)
    c.url = "https://x"
    return c


def item(sym, mc, qty, last, avg, mv, pl, rate, buy):
    return {"symbol": sym, "name": sym, "marketCountry": mc, "currency": "USD" if mc == "US" else "KRW",
            "quantity": str(qty), "lastPrice": str(last), "averagePurchasePrice": str(avg),
            "marketValue": {"amount": str(mv), "purchaseAmount": str(buy)},
            "profitLoss": {"amount": str(pl), "rate": str(rate)}}


HOLDINGS = {"items": [item("SPY", "US", 2.5, 600, 550, 1500, 125, 0.0909, 1375),
                      item("069500", "KR", 10, 40000, 38000, 400000, 20000, 0.0526, 380000)]}


@pytest.fixture(autouse=True)
def _clean():
    toss_api.LIVE_PRICES.clear()
    toss_api.ORDER_EVENTS.clear()
    yield


class TestBalance:
    def test_us_empty_is_empty_list_not_none(self):
        c = client(TossOverseas, {"/api/v1/holdings": {"items": []}})
        assert c.get_balance()["output1"] == []

    def test_us_failure_is_none(self):
        c = client(TossOverseas, {"/api/v1/holdings": TossApiError(500, "internal-error", "x")})
        assert c.get_balance() is None

    def test_us_filters_market_and_floors_fractional(self):
        c = client(TossOverseas, {"/api/v1/holdings": HOLDINGS})
        rows = c.get_balance()["output1"]
        assert [r["ovrs_pdno"] for r in rows] == ["SPY"]
        assert rows[0]["ovrs_cblc_qty"] == "2" and rows[0]["now_pric2"] == "600"

    def test_kr_total_includes_cash_and_amounts_are_int_strings(self):
        c = client(TossDomestic, {"/api/v1/holdings": HOLDINGS,
                                  "/api/v1/buying-power": {"currency": "KRW", "cashBuyingPower": "1234567"}})
        bal = c.get_balance()
        o2 = bal["output2"][0]
        assert [r["pdno"] for r in bal["output1"]] == ["069500"]
        assert int(o2["tot_evlu_amt"]) == 400000 + 1234567      # 규칙 6
        for k in ("dnca_tot_amt", "prvs_rcdl_excc_amt", "ord_psbl_cash", "evlu_pfls_smtl_amt"):
            int(o2[k])                                            # 호출부가 int(str) 로 읽는다
        assert int(bal["output1"][0]["hldg_qty"]) == 10

    def test_kr_cash_failure_fails_whole_balance(self):
        """현금을 0 으로 읽으면 '자금 부족'으로 기록돼 원인이 가려진다."""
        c = client(TossDomestic, {"/api/v1/holdings": HOLDINGS,
                                  "/api/v1/buying-power": TossApiError(500, "internal-error", "x")})
        assert c.get_balance() is None

    def test_foreign_balance_failure_is_none_not_zero(self):
        c = client(TossOverseas, {"/api/v1/buying-power": TossApiError(0, "network-error", "x")})
        assert c.get_foreign_balance() is None
        c = client(TossOverseas, {"/api/v1/buying-power": {"currency": "USD", "cashBuyingPower": "0"}})
        assert c.get_foreign_balance() == {"deposit": 0.0, "withdraw_possible": 0.0}

    def test_holding_qty_failure_is_none(self):
        c = client(TossDomestic, {"/api/v1/sellable-quantity": TossApiError(500, "internal-error", "x")})
        assert c.get_holding_qty("069500") is None
        c = client(TossDomestic, {"/api/v1/sellable-quantity": {"sellableQuantity": "7"}})
        assert c.get_holding_qty("069500") == 7


class TestOrders:
    def test_us_buy_is_limit_plus_one_percent(self):
        c = client(TossOverseas, {"/api/v1/prices": [{"symbol": "SPY", "lastPrice": "500.00"}],
                                  "/api/v1/orders": {"orderId": "oid-1"}})
        res = c.buy_market_order("SPY", 3, "AMS")
        body = c.s.calls[-1][3]
        assert res["rt_cd"] == "0" and res["order_id"] == "oid-1"
        assert body["orderType"] == "LIMIT" and body["price"] == "505.0" and body["quantity"] == "3"
        assert body["clientOrderId"].startswith("sniper-")      # 재전송 멱등성 키

    def test_sell_is_market(self):
        c = client(TossDomestic, {"/api/v1/orders": {"orderId": "oid-2"}})
        c.sell_market_order("069500", 5)
        assert c.s.calls[-1][3]["orderType"] == "MARKET"

    def test_order_error_maps_to_rt_cd_1_with_toss_code(self):
        c = client(TossOverseas, {"/api/v1/orders": TossApiError(422, "insufficient-buying-power", "부족")})
        res = c.sell_market_order("SPY", 1)
        assert res["rt_cd"] == "1" and res["msg_cd"] == "insufficient-buying-power"

    def test_account_restriction_codes_known_to_bot(self):
        assert "account-restricted" in toss_api.ACCOUNT_RESTRICTED_CODES
        assert "prerequisite-required" in toss_api.ACCOUNT_RESTRICTED_CODES

    def test_kr_limit_sell_rounds_down_to_tick(self):
        c = client(TossDomestic, {"/api/v1/orders": {"orderId": "o"}})
        c.sell_limit_order("069500", 1, 40070.7)
        assert c.s.calls[-1][3]["price"] == "40050"


class TestSpeed:
    def test_fresh_websocket_price_skips_rest(self):
        c = client(TossOverseas, {"/api/v1/prices": TossApiError(500, "x", "should not be called")})
        toss_api.record_trade("SPY", 512.3)
        assert c.get_current_price("SPY") == 512.3 and c.s.calls == []

    def test_stale_websocket_price_falls_back_to_rest(self):
        c = client(TossOverseas, {"/api/v1/prices": [{"symbol": "SPY", "lastPrice": "500"}]})
        toss_api.LIVE_PRICES["SPY"] = (512.3, time.monotonic() - 60)
        assert c.get_current_price("SPY") == 500.0

    def test_wait_order_returns_on_websocket_event_without_rest(self):
        c = client(TossOverseas, {"/api/v1/orders/oid": TossApiError(500, "x", "no rest")})
        toss_api.record_order_event({"orderId": "oid", "status": "FILLED",
                                     "execution": {"filledQuantity": "3"}})
        t0 = time.monotonic()
        snap = c.wait_order("oid", timeout_sec=5)
        assert snap["status"] == "FILLED" and time.monotonic() - t0 < 0.5
        assert not any(p.startswith("/api/v1/orders/") for _, p, _, _ in c.s.calls)

    def test_wait_order_falls_back_to_rest(self):
        c = client(TossOverseas, {"/api/v1/orders/oid": {"orderId": "oid", "status": "REJECTED"}})
        assert c.wait_order("oid", timeout_sec=3)["status"] == "REJECTED"


class TestDailyBars:
    def test_us_bar_date_is_new_york_trading_day(self):
        # FAQ: 미국 2026-10-05 거래일 → 2026-10-05T13:00:00+09:00 (서머타임)
        assert toss_api._bar_date("2026-10-05T13:00:00+09:00", "US") == "20261005"
        assert toss_api._bar_date("2026-12-07T14:00:00+09:00", "US") == "20261207"
        assert toss_api._bar_date("2026-10-05T00:00:00+09:00", "KR") == "20261005"

    def test_history_is_oldest_first_and_paginates(self):
        pages = {None: {"candles": [{"timestamp": "2026-10-02T00:00:00+09:00", "closePrice": "3"},
                                    {"timestamp": "2026-10-01T00:00:00+09:00", "closePrice": "2"}],
                        "nextBefore": "2026-09-30T00:00:00+09:00"},
                 "2026-09-30T00:00:00+09:00": {"candles": [{"timestamp": "2026-09-30T00:00:00+09:00",
                                                            "closePrice": "1"}], "nextBefore": None}}
        c = client(TossDomestic, {"/api/v1/candles": lambda p, b: pages[p.get("before")]})
        rows = c.get_daily_history("069500", bars=10)
        assert [r["date"] for r in rows] == ["20260930", "20261001", "20261002"]
        assert [r["close"] for r in rows] == [1.0, 2.0, 3.0]


class TestToken:
    def test_recover_adopts_token_from_other_process(self, tmp_path, monkeypatch):
        """봇이 401 을 받았는데 대시보드가 이미 새 토큰을 받아 뒀다면 재발급하지 않는다
        (재발급하면 대시보드 토큰이 revoke 되고, 그쪽도 재발급 → 서로 끊는 루프)."""
        monkeypatch.setattr(toss_api, "TOKEN_FILE", str(tmp_path / "tok.json"))
        monkeypatch.setattr(toss_api, "_LOCK_FILE", str(tmp_path / "tok.lock"))
        s = toss_api.TossSession(base_url="https://x", client_id="id", client_secret="sec")
        s._write_cache("fresh", time.time() + 3600)
        issued = []
        monkeypatch.setattr(s, "_issue", lambda: issued.append(1))
        s._recover_auth("stale")
        assert s._token == "fresh" and issued == []
        s._recover_auth("fresh")                 # 파일 토큰도 죽었다 → 그때만 재발급
        assert issued == [1]


class TestWebSocketDispatch:
    def test_trade_and_order_frames(self):
        import asyncio
        import json
        from modules.toss_websocket import TossWebSocket
        got = []
        ws = TossWebSocket(["SPY"], lambda t, p: got.append((t, p)), market="US")
        asyncio.run(ws._dispatch(json.dumps({"type": "message", "topic": "trade:us:SPY",
                                             "data": {"price": "601.5", "volume": "1"}})))
        asyncio.run(ws._dispatch(json.dumps({"type": "message", "topic": "personal:order:3",
                                             "data": {"event": "FILL", "order": {"orderId": "z",
                                                                                  "status": "FILLED"}}})))
        assert got == [("SPY", 601.5)] and toss_api.live_price("SPY") == 601.5
        assert toss_api.ORDER_EVENTS["z"]["status"] == "FILLED"


def test_bot_treats_rejected_order_as_failure():
    import run_bot

    class B:
        def wait_order(self, oid, timeout_sec=0):
            return {"orderId": oid, "status": "REJECTED", "execution": {"filledQuantity": "0"}}
    ok, note = run_bot._confirm_order(B(), {"rt_cd": "0", "order_id": "x"}, "SPY")
    assert not ok and "REJECTED" in note
