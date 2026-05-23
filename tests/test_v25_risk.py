"""v2.5 신규 리스크 기능 단위 테스트.

- Breakeven Stop (walk-forward 시뮬레이터 + run_bot 모듈)
- Correlation Cap (is_correlation_capped)
- Losing Streak Throttle (_record_loss_event / is_losing_streak_pause)
- ATR 동적 손절 (calculate_atr_pct / effective_stop_loss_pct)
- 슬리피지/수수료 모델 (walk_forward)
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# 외부 의존성 차단 (telegram, schedule, google.*)
fake_modules = {
    'schedule': MagicMock(),
    'pytz': MagicMock(),
    'requests': MagicMock(),
    'modules.kis_api': MagicMock(),
    'modules.kis_websocket': MagicMock(),
    'modules.kis_domestic': MagicMock(),
    'modules.telegram_notifier': MagicMock(),
    'modules.market_scanner': MagicMock(),
    'modules.auto_strategy': MagicMock(),
    'modules.account_manager': MagicMock(),
    'modules.portfolio_manager': MagicMock(),
    'modules.profit_tracker': MagicMock(),
    'modules.opro_optimizer': MagicMock(),
    'modules.measurement': MagicMock(),
    'modules.trade_journal': MagicMock(),
    'modules.multi_llm': MagicMock(),
    'modules.gemini_analyst': MagicMock(),
    'modules.grok_analyst': MagicMock(),
    'modules.deepseek_analyst': MagicMock(),
    'modules.groq_analyst': MagicMock(),
}


class TestV25BreakevenWalkForward(unittest.TestCase):
    """walk_forward 시뮬레이터 breakeven_stop 트리거 검증."""

    def test_breakeven_arms_and_stops(self):
        from modules.walk_forward import _simulate_position
        # day0: 매수 @100, day1: +5% 고점(105), day2: -1% 로 100 보다 약간 위(100.2 floor)
        # → breakeven_trigger 3% 도달 → floor=100.2, day2 low(99) 가 floor 미달 → 청산
        ohlc = [
            {"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0},  # entry
            {"open": 100.0, "high": 105.0, "low": 100.0, "close": 104.0},  # arm
            {"open": 104.0, "high": 104.0, "low": 99.0, "close": 100.0},  # hit floor
        ]
        params = {
            "stop_loss_pct": -10.0,
            "trailing_activation_pct": 7.0,
            "trailing_drop_pct": 3.0,
            "time_cut_days": 5,
            "partial_tp_ratio": 0.0,
            "breakeven_trigger_pct": 3.0,
            "breakeven_buffer_pct": 0.2,
        }
        trade = _simulate_position(ohlc, 0, params)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.trigger, "breakeven_stop")
        # buffer 0.2% 익절분 근처 (수수료 0)
        self.assertAlmostEqual(trade.pnl_pct, 0.2, places=2)

    def test_breakeven_not_armed_below_trigger(self):
        from modules.walk_forward import _simulate_position
        ohlc = [
            {"open": 100.0, "high": 100.5, "low": 99.0, "close": 100.0},
            {"open": 100.0, "high": 102.0, "low": 100.0, "close": 101.0},  # 2% 만 → arm X
            {"open": 101.0, "high": 101.0, "low": 95.0, "close": 96.0},   # -5% 손절
        ]
        params = {
            "stop_loss_pct": -5.0,
            "trailing_activation_pct": 7.0,
            "trailing_drop_pct": 3.0,
            "time_cut_days": 5,
            "partial_tp_ratio": 0.0,
            "breakeven_trigger_pct": 3.0,
            "breakeven_buffer_pct": 0.2,
        }
        trade = _simulate_position(ohlc, 0, params)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.trigger, "stop_loss")
        self.assertAlmostEqual(trade.pnl_pct, -5.0, places=2)

    def test_breakeven_prevents_loss_conversion(self):
        """+3% 도달 후 큰 하락 → 본전 청산으로 손실 전환 방지."""
        from modules.walk_forward import _simulate_position
        ohlc_v25 = [
            {"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0},
            {"open": 100.0, "high": 104.0, "low": 100.0, "close": 103.0},  # arm
            {"open": 103.0, "high": 103.0, "low": 96.0, "close": 97.0},   # 큰 하락
        ]
        # v2.5: breakeven 작동
        params_v25 = {
            "stop_loss_pct": -5.0,
            "trailing_activation_pct": 7.0,
            "trailing_drop_pct": 3.0,
            "time_cut_days": 5,
            "partial_tp_ratio": 0.0,
            "breakeven_trigger_pct": 3.0,
            "breakeven_buffer_pct": 0.2,
        }
        # v2.4: breakeven 없음 → 손절가에 청산
        params_v24 = dict(params_v25)
        params_v24["breakeven_trigger_pct"] = 0.0
        t25 = _simulate_position(ohlc_v25, 0, params_v25)
        t24 = _simulate_position(ohlc_v25, 0, params_v24)
        self.assertGreater(t25.pnl_pct, t24.pnl_pct)
        self.assertGreaterEqual(t25.pnl_pct, 0.0)


class TestV25SlippageFee(unittest.TestCase):
    def test_costs_reduce_pnl(self):
        from modules.walk_forward import _simulate_position
        ohlc = [
            {"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0},
            {"open": 100.0, "high": 110.0, "low": 100.0, "close": 109.0},
            {"open": 109.0, "high": 109.0, "low": 106.0, "close": 107.0},  # 상한선 청산 (trailing)
        ]
        base = {
            "stop_loss_pct": -10.0,
            "trailing_activation_pct": 5.0,
            "trailing_drop_pct": 3.0,
            "time_cut_days": 5,
            "partial_tp_ratio": 0.0,
        }
        with_cost = dict(base)
        with_cost["slippage_pct"] = 0.05
        with_cost["fee_pct"] = 0.015
        t0 = _simulate_position(ohlc, 0, base)
        t1 = _simulate_position(ohlc, 0, with_cost)
        # 비용 = 0.05 + 2*0.015 = 0.08 차감
        self.assertAlmostEqual(t0.pnl_pct - t1.pnl_pct, 0.08, places=3)


class TestV25ATR(unittest.TestCase):
    def test_atr_basic(self):
        from strategies.technical import calculate_atr_pct
        # 15개 일봉 (index 0 = 최근). KIS 포맷 키 'high','low','clos'
        ohlc = []
        price = 100.0
        for _ in range(20):
            ohlc.append({'high': price * 1.02, 'low': price * 0.98, 'clos': price})
            price += 0.5
        ohlc.reverse()  # index 0 = 최신
        atr = calculate_atr_pct(ohlc, period=14)
        self.assertIsNotNone(atr)
        # 진폭 4% 안팎이므로 ATR% 도 그 부근
        self.assertGreater(atr, 3.0)
        self.assertLess(atr, 6.0)

    def test_effective_stop_picks_more_conservative(self):
        # ATR 가 충분히 크면 stop 이 더 느슨해져야 함
        sys.modules.update(fake_modules)
        import importlib
        if 'run_bot' in sys.modules:
            del sys.modules['run_bot']
        run_bot = importlib.import_module('run_bot')
        ohlc = [{'high': 110, 'low': 90, 'clos': 100}] * 20  # 큰 변동성
        eff = run_bot.effective_stop_loss_pct(-3.0, ohlc)
        # ATR ≈ 20%, mult=2 → -40% 인데 ATR_STOP_MAX_PCT=-10% 로 클램프
        self.assertLessEqual(eff, -3.0)  # 더 느슨해짐 (절대값 ↑)
        self.assertGreaterEqual(eff, run_bot.ATR_STOP_MAX_PCT)


class TestV25CorrelationCap(unittest.TestCase):
    def test_under_cap_allows(self):
        sys.modules.update(fake_modules)
        import importlib
        if 'run_bot' in sys.modules:
            del sys.modules['run_bot']
        run_bot = importlib.import_module('run_bot')
        targets = {
            "TQQQ": {"status": "bought", "buys": 5},
        }
        capped, _ = run_bot.is_correlation_capped("SOXL", targets)
        self.assertFalse(capped)  # 1개 보유 < cap(2)

    def test_at_cap_blocks(self):
        sys.modules.update(fake_modules)
        import importlib
        if 'run_bot' in sys.modules:
            del sys.modules['run_bot']
        run_bot = importlib.import_module('run_bot')
        targets = {
            "TQQQ": {"status": "bought", "buys": 5},
            "TECL": {"status": "bought", "buys": 3},
        }
        capped, grp = run_bot.is_correlation_capped("SOXL", targets)
        self.assertTrue(capped)
        self.assertIsNotNone(grp)


class TestV25LosingStreak(unittest.TestCase):
    def setUp(self):
        sys.modules.update(fake_modules)
        import importlib
        if 'run_bot' in sys.modules:
            del sys.modules['run_bot']
        self.run_bot = importlib.import_module('run_bot')
        # 상태 초기화
        self.run_bot._daily_state['date'] = None
        self.run_bot._daily_state['stop_count'] = 0
        self.run_bot._daily_state['realized_pnl_pct'] = 0.0
        self.run_bot._daily_state['paused'] = False

    def test_one_stop_does_not_pause(self):
        self.run_bot._record_loss_event(-2.0, 'stop_loss')
        self.assertFalse(self.run_bot.is_losing_streak_pause())

    def test_max_stops_triggers_pause(self):
        for _ in range(self.run_bot.LOSING_STREAK_MAX_STOPS):
            self.run_bot._record_loss_event(-0.5, 'stop_loss')
        self.assertTrue(self.run_bot.is_losing_streak_pause())

    def test_cumulative_pnl_triggers_pause(self):
        # 누적 손실이 임계치 이하 → pause
        self.run_bot._record_loss_event(-5.0, 'trailing_stop')
        self.assertTrue(self.run_bot.is_losing_streak_pause())


if __name__ == '__main__':
    unittest.main()
