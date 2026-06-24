"""v3.0 전략 개정 단위 테스트.

- 레버리지 비대칭 스케일링 (get_leverage_factor / scale_profit_threshold / scale_loss_threshold)
- ATR 동적 손절 cap 의 레버리지 확장 (effective_stop_loss_pct)
- 추세붕괴 손절 게이트 로직 (버퍼/배율 상호작용)
- 종목선정 복합 점수 (PortfolioManager.get_composite_score)
"""
import os
import sys
import types
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# run_bot 의 무거운 의존성 차단
_FAKE = {
    'schedule': MagicMock(),
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
}

_run_bot = None


def _get_run_bot():
    global _run_bot
    if _run_bot is None:
        for k, v in _FAKE.items():
            sys.modules.setdefault(k, v)
        import run_bot
        _run_bot = run_bot
    return _run_bot


class TestLeverageScaling(unittest.TestCase):
    def setUp(self):
        self.rb = _get_run_bot()
        self.rb.LEVERAGED_SCALING_ENABLED = True

    def test_factor_known_and_unknown(self):
        self.assertEqual(self.rb.get_leverage_factor('SOXL'), 3.0)
        self.assertEqual(self.rb.get_leverage_factor('NVDL'), 2.0)
        self.assertEqual(self.rb.get_leverage_factor('QQQ'), 1.0)   # 1x ETF
        self.assertEqual(self.rb.get_leverage_factor('AAPL'), 1.0)  # 개별주
        self.assertEqual(self.rb.get_leverage_factor(None), 1.0)

    def test_profit_threshold_full_factor(self):
        # 상승 임계는 풀 배율: SOXL(3x) 트레일링 활성 +5% → +15%
        self.assertAlmostEqual(self.rb.scale_profit_threshold(5.0, 'SOXL'), 15.0)
        self.assertAlmostEqual(self.rb.scale_profit_threshold(3.0, 'NVDL'), 6.0)  # 2x
        self.assertAlmostEqual(self.rb.scale_profit_threshold(5.0, 'QQQ'), 5.0)   # 1x 변화 없음

    def test_loss_threshold_conservative_factor(self):
        # 하락 임계는 보수적 배율 (1+N)/2: SOXL(3x) → ×2.0, NVDL(2x) → ×1.5
        self.assertAlmostEqual(self.rb.scale_loss_threshold(-6.0, 'SOXL'), -12.0)
        self.assertAlmostEqual(self.rb.scale_loss_threshold(-6.0, 'NVDL'), -9.0)
        self.assertAlmostEqual(self.rb.scale_loss_threshold(-6.0, 'QQQ'), -6.0)   # 1x 변화 없음

    def test_scaling_disabled_noop(self):
        self.rb.LEVERAGED_SCALING_ENABLED = False
        self.assertAlmostEqual(self.rb.scale_profit_threshold(5.0, 'SOXL'), 5.0)
        self.assertAlmostEqual(self.rb.scale_loss_threshold(-6.0, 'SOXL'), -6.0)
        self.rb.LEVERAGED_SCALING_ENABLED = True

    def test_effective_stop_cap_scales_with_leverage(self):
        # 변동성이 큰 일봉 → ATR stop 이 cap 에 걸림. 레버리지면 cap 이 (1+N)/2 배로 확장.
        ohlc = [{'high': 110, 'low': 90, 'clos': 100}] * 20
        eff_1x = self.rb.effective_stop_loss_pct(-6.0, ohlc, 1.0)
        eff_3x = self.rb.effective_stop_loss_pct(-6.0, ohlc, 3.0)
        # 3x cap = ATR_STOP_MAX_PCT * 2.0 → 1x 대비 더 느슨(절대값 큼)
        self.assertLess(eff_3x, eff_1x)
        self.assertGreaterEqual(eff_1x, self.rb.ATR_STOP_MAX_PCT)
        self.assertGreaterEqual(eff_3x, self.rb.ATR_STOP_MAX_PCT * 2.0)


class TestTrendExitGate(unittest.TestCase):
    """추세붕괴 손절의 핵심 게이트: curr < MA20*(1 - buf/factor/100)."""

    def setUp(self):
        self.rb = _get_run_bot()

    def _would_exit(self, curr, ma20, lev_factor, pnl_pct):
        buf = self.rb.TREND_EXIT_BUFFER_PCT / max(1.0, lev_factor)
        gate_price = ma20 * (1.0 - buf / 100.0)
        return (pnl_pct <= self.rb.TREND_EXIT_MIN_LOSS_PCT) and (curr < gate_price)

    def test_leveraged_more_sensitive_than_1x(self):
        # 가격이 MA20 대비 0.5% 아래. 3x(버퍼 0.33%)는 청산, 1x(버퍼 1.0%)는 보류.
        ma20 = 100.0
        curr = 99.5  # -0.5%
        self.assertTrue(self._would_exit(curr, ma20, 3.0, -3.0))   # 레버리지 → 청산
        self.assertFalse(self._would_exit(curr, ma20, 1.0, -3.0))  # 1x → 아직 보류(노이즈 허용)

    def test_no_exit_when_profitable(self):
        # 손실 게이트(min_loss) 미달이면 추세 하회여도 청산 안 함
        self.assertFalse(self._would_exit(98.0, 100.0, 3.0, +0.5))

    def test_exit_when_clearly_below_trend(self):
        self.assertTrue(self._would_exit(95.0, 100.0, 1.0, -5.0))


class TestCompositeScore(unittest.TestCase):
    """PortfolioManager.get_composite_score — 추세추종 복합 점수."""

    def setUp(self):
        # portfolio_manager 는 KIS 클래스를 임포트 → 더미로 치환 후 임포트
        for name in ('modules.kis_domestic', 'modules.kis_api'):
            stub = types.ModuleType(name)
            stub.KisDomestic = type('KisDomestic', (), {'__init__': lambda self, *a, **k: None})
            stub.KisOverseas = type('KisOverseas', (), {'__init__': lambda self, *a, **k: None})
            sys.modules[name] = stub
        # MagicMock 으로 덮인 portfolio_manager 를 제거하고 실제 모듈 로드
        sys.modules.pop('modules.portfolio_manager', None)
        from modules.portfolio_manager import PortfolioManager
        self.pm = PortfolioManager()

    @staticmethod
    def _uptrend_ohlc(n=70, start=100.0, step=1.0, amp=1.0):
        """매끄러운 상승 일봉 (index 0 = 최신)."""
        bars = []
        price = start
        for _ in range(n):
            bars.append({'clos': price, 'high': price + amp, 'low': price - amp})
            price += step
        bars.reverse()  # index 0 = 최신(가장 높은 가격)
        return bars

    @staticmethod
    def _downtrend_ohlc(n=70, start=170.0, step=1.0, amp=1.0):
        bars = []
        price = start
        for _ in range(n):
            bars.append({'clos': price, 'high': price + amp, 'low': price - amp})
            price -= step
        bars.reverse()  # index 0 = 최신(가장 낮은 가격)
        return bars

    def test_uptrend_trend_ok_and_positive(self):
        comp = self.pm.get_composite_score(self._uptrend_ohlc())
        self.assertTrue(comp['trend_ok'])
        self.assertGreater(comp['score'], 0)
        self.assertGreater(comp['momentum'], 0)

    def test_downtrend_demoted(self):
        comp = self.pm.get_composite_score(self._downtrend_ohlc())
        self.assertFalse(comp['trend_ok'])
        # 하락추세는 음수 모멘텀 → 점수도 음수(강등)
        self.assertLess(comp['score'], 0)

    def test_uptrend_outranks_downtrend(self):
        up = self.pm.get_composite_score(self._uptrend_ohlc())
        down = self.pm.get_composite_score(self._downtrend_ohlc())
        self.assertGreater(up['score'], down['score'])

    def test_insufficient_data_fallback(self):
        comp = self.pm.get_composite_score([{'clos': 100, 'high': 101, 'low': 99}])
        self.assertEqual(comp['score'], -999.0)
        self.assertFalse(comp['trend_ok'])


if __name__ == '__main__':
    unittest.main()
