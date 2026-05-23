"""Walk-Forward Simulator 단위 테스트 (외부 API 미사용)."""

import unittest

from modules.walk_forward import (
    PARAM_SETS,
    compare_param_sets,
    simulate_exit_rules,
    _simulate_position,
)


def _bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


class TestWalkForward(unittest.TestCase):
    def test_stop_loss_trigger(self):
        # 진입 100, 다음 바 저가 90 (-10%) → v2.3_us (-5%) stop_loss 발동
        ohlc = [_bar(100, 101, 99, 100), _bar(100, 102, 90, 95)]
        params = PARAM_SETS["v2.3_us"]
        t = _simulate_position(ohlc, 0, params)
        self.assertIsNotNone(t)
        self.assertEqual(t.trigger, "stop_loss")
        self.assertAlmostEqual(t.pnl_pct, -5.0, places=2)

    def test_trailing_stop_trigger(self):
        # 진입 100, 고가 110 (+10% → trail 활성), 저가 105 (고점대비 -4.5% > 3%) → trailing_stop
        ohlc = [_bar(100, 100, 100, 100), _bar(100, 110, 105, 106)]
        params = PARAM_SETS["v2.3_us"]
        t = _simulate_position(ohlc, 0, params)
        self.assertIsNotNone(t)
        self.assertEqual(t.trigger, "trailing_stop")
        # 110 * (1-3%) = 106.7
        self.assertAlmostEqual(t.exit_price, 106.7, places=1)

    def test_time_cut_when_neither(self):
        # 변동성 거의 없음 → time_cut 으로 마지막 바 청산
        ohlc = [_bar(100, 100.5, 99.8, 100.2)] * 6
        params = PARAM_SETS["v2.3_us"]
        t = _simulate_position(ohlc, 0, params)
        self.assertIsNotNone(t)
        self.assertIn(t.trigger, ("time_cut", "end_of_series"))

    def test_v22_vs_v23_loose_stop_keeps_position(self):
        # 일중 -4% 저점 → v2.2_us (-3%) 손절 / v2.3_us (-5%) 보유 → 종가 +2%
        ohlc = [_bar(100, 102, 96, 102)] + [_bar(102, 103, 101, 102)] * 5
        v22 = _simulate_position(ohlc, 0, PARAM_SETS["v2.2_us"])
        v23 = _simulate_position(ohlc, 0, PARAM_SETS["v2.3_us"])
        self.assertEqual(v22.trigger, "stop_loss")
        self.assertAlmostEqual(v22.pnl_pct, -3.0, places=2)
        # v2.3 은 손절 안 맞고 trailing 또는 time_cut 으로 양수 수익
        self.assertNotEqual(v23.trigger, "stop_loss")
        self.assertGreaterEqual(v23.pnl_pct, 0.0)

    def test_compare_param_sets_returns_metrics(self):
        # 5일 상승 시나리오
        ohlc = [_bar(100 + i, 102 + i, 99 + i, 101 + i) for i in range(20)]
        cmp_ = compare_param_sets(ohlc, set_names=("v2.2_us", "v2.3_us"))
        self.assertIn("v2.2_us", cmp_)
        self.assertIn("v2.3_us", cmp_)
        for v in cmp_.values():
            self.assertIn("n", v)
            self.assertIn("total_pnl_pct", v)

    def test_simulate_full_series_produces_multiple_trades(self):
        ohlc = [_bar(100, 105, 98, 102) for _ in range(30)]
        res = simulate_exit_rules(ohlc, PARAM_SETS["v2.3_us"], "v2.3_us")
        self.assertGreater(res.n, 0)
        m = res.metrics()
        self.assertGreater(m["n"], 0)

    def test_empty_ohlc_safe(self):
        res = simulate_exit_rules([], PARAM_SETS["v2.3_us"], "v2.3_us")
        self.assertEqual(res.n, 0)
        self.assertEqual(res.metrics()["n"], 0)


if __name__ == "__main__":
    unittest.main()
