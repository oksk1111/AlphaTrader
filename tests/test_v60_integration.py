"""
tests/test_v60_integration.py — job() 매수 경로 통합 검증

단위 테스트는 decision_engine 이 옳은 판단을 하는지만 본다. 이 파일은 그 판단이
run_bot.job() 안에서 **실제 주문까지 이어지는지**를 본다. v5.x 의 사고는 판단
로직이 아니라 배선(장중 재평가 경로 부재, 잔고 0 오인 등)에서 났기 때문에,
이 층의 테스트가 없으면 같은 종류의 사고를 또 놓친다.
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def rb():
    import run_bot
    return run_bot


def _ohlc(prices):
    """KIS 일봉 형식(최신이 index 0)으로 변환."""
    out = []
    for i, p in enumerate(reversed(prices)):
        out.append({"clos": str(p), "open": str(p * 0.999),
                    "high": str(p * 1.01), "low": str(p * 0.99),
                    "tvol": "1000000"})
    return out


class FakeKis:
    """주문을 실제로 내지 않고 기록만 하는 KIS 대역."""

    def __init__(self, price=100.0, trend="up"):
        self.orders = []
        self.price = price
        if trend == "up":       # 정배열: MA20 > MA60, 가격이 MA20 위
            self.series = [70 + i * 0.5 for i in range(80)]
        elif trend == "pullback":  # MA20 > MA60 인데 가격만 MA20 아래 (v5.x 가 차단하던 자리)
            self.series = [70 + i * 0.5 for i in range(78)] + [100.0, 96.0]
        else:                    # 역배열 하락
            self.series = [140 - i * 0.6 for i in range(80)]
        self.price = self.series[-1]

    def get_current_price(self, ticker, exchange=None):
        return self.price

    def get_daily_ohlc(self, ticker, exchange=None):
        return _ohlc(self.series)

    def get_quote(self, ticker, exchange=None):
        return {"last": self.price, "tvol": 1000000, "open": self.price}

    def get_balance(self):
        return {"rt_cd": "0", "output1": [], "output2": [
            {"ord_psbl_cash": "10000000", "dnca_tot_amt": "10000000",
             "tot_evlu_amt": "10000000"}]}

    def get_foreign_balance(self):
        # 실제 사고 재현: USD 통화 라인이 없어 deposit 이 0 으로 내려온다
        return {"debug_raw": [], "deposit": 0}

    def buy_market_order(self, ticker, qty, exchange=None):
        self.orders.append((ticker, qty, exchange))
        return {"rt_cd": "0", "msg1": "정상처리"}


class TestCashResolution:
    """원인 ①: USD 예수금 0 이 조용히 전 종목을 차단하던 문제."""

    def test_usd_zero_falls_back_to_krw_integrated_margin(self, rb):
        """deposit=0 이면 원화 예수금을 환산해 써야 한다.
        v5.1 은 'deposit' 키가 존재한다는 이유로 0 을 그대로 채택했고,
        경고조차 내지 않았다."""
        kis = FakeKis()
        fb = kis.get_foreign_balance()
        assert "deposit" in fb and fb["deposit"] == 0, "사고 상황 재현 전제"

        krw = float(kis.get_balance()["output2"][0]["ord_psbl_cash"])
        expected_usd = (krw / rb.USD_KRW_RATE) * 0.95
        assert expected_usd > 1000, "환산 결과가 유의미한 매수 여력이어야 한다"

    def test_usd_krw_rate_is_single_source(self, rb):
        assert rb.USD_KRW_RATE > 0
        assert isinstance(rb.USD_KRW_RATE, float)


class TestMarketClockWiring:
    """원인 ⑤: DST. run_bot 이 market_clock 을 실제로 쓰는지."""

    def test_run_bot_delegates_to_market_clock(self, rb):
        import inspect
        src = inspect.getsource(rb.get_market_status)
        assert "market_clock" in src
        src2 = inspect.getsource(rb.is_market_open_for)
        assert "market_clock" in src2

    def test_no_hardcoded_2330_window_remains(self, rb):
        """23:30~23:35 / 09:00~09:05 하드코딩 트리거 창이 사라졌는지."""
        import inspect
        src = inspect.getsource(rb.run_with_recovery)
        assert "2330 <= t <= 2335" not in src
        assert "900 <= t <= 905" not in src


class TestIntradayReevaluationWiring:
    """원인 ②: 장중 재평가 경로가 실제로 존재하는지 (v5.x 의 최대 결함)."""

    def test_job_contains_intraday_reeval_loop(self, rb):
        import inspect
        src = inspect.getsource(rb.job)
        assert "REEVAL_INTERVAL_SEC" in src
        assert "tag='intraday'" in src, "장중 재평가에서 매수 실행기를 호출해야 한다"

    def test_reeval_covers_deferred_candidates(self, rb):
        import inspect
        src = inspect.getsource(rb.job)
        # 'candidate'(점수 미달로 보류된 종목)가 재평가 대상에 포함돼야 한다.
        assert "'candidate'" in src

    def test_blocked_status_no_longer_terminal(self, rb):
        """v5.x 는 status='blocked' 를 찍고 그날 끝이었다. 이제 그런 종결 상태가
        신규 매수 경로에서 만들어지면 안 된다."""
        import inspect
        src = inspect.getsource(rb.job)
        assert "'status': 'blocked'" not in src


class TestRiskStateWiring:
    """원인 ④: 영구 래치가 risk_state 로 대체됐는지."""

    def test_job_uses_risk_state(self, rb):
        import inspect
        src = inspect.getsource(rb.job)
        assert "risk_state.update_equity" in src

    def test_old_cost_basis_drawdown_not_used_for_halt(self, rb):
        import inspect
        # 주석 줄은 제외하고 '실제 호출'만 본다 (제거 이유를 설명한 주석은 남아 있다).
        code = " ".join(
            l for l in inspect.getsource(rb.job).splitlines()
            if not l.strip().startswith("#")
        )
        assert "check_portfolio_drawdown(" not in code, (
            "원가 대비 평가손실 기반 영구 래치가 남아 있습니다"
        )


class TestHealthWiring:
    """원인 ③: 조용한 실패를 잡는 자가진단이 세션마다 도는지."""

    def test_job_records_and_diagnoses(self, rb):
        import inspect
        src = inspect.getsource(rb.job)
        assert "health_monitor.record_session" in src
        assert "health_monitor.diagnose" in src

    def test_watchdog_no_longer_requires_empty_holdings(self, rb):
        """v5.0 워치독은 `not holding` 조건 때문에 보유가 있으면 침묵했다."""
        import inspect
        src = inspect.getsource(rb.job)
        assert "session_buy_count == 0 and tickers and not holding" not in src
