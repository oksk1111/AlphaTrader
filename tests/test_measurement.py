"""Trade Journal + Measurement 단위 테스트."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from modules import trade_journal
from modules import measurement


class TestTradeJournal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "journal.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_close_basic_pnl(self):
        rec = trade_journal.record_close(
            ticker="005930",
            market="KR",
            trigger="stop_loss",
            exit_price=68000,
            sold_qty=10,
            monitor_data={"buy_price": 70000, "buys": 10, "exchange": None,
                          "entry_time": "2026-05-20 10:00:00"},
            phase="market",
            path=self.path,
        )
        self.assertIsNotNone(rec)
        self.assertEqual(rec["pnl_per_share"], -2000.0)
        self.assertEqual(rec["pnl_total"], -20000.0)
        self.assertAlmostEqual(rec["pnl_pct"], -2.857142857142857, places=3)
        self.assertEqual(rec["trigger"], "stop_loss")
        self.assertEqual(rec["phase"], "market")
        self.assertIsNotNone(rec["duration_min"])

    def test_record_close_already_flat_zero_qty(self):
        rec = trade_journal.record_close(
            ticker="TQQQ", market="US", trigger="stop_loss",
            exit_price=0.0, sold_qty=0,
            monitor_data={"buy_price": 50.0, "buys": 0},
            phase="already_flat", path=self.path,
        )
        self.assertIsNotNone(rec)
        self.assertEqual(rec["sold_qty"], 0)
        self.assertEqual(rec["phase"], "already_flat")

    def test_record_close_appends(self):
        for i in range(3):
            trade_journal.record_close(
                ticker="AAA", market="KR", trigger="time_cut",
                exit_price=100 + i, sold_qty=1,
                monitor_data={"buy_price": 100, "buys": 1},
                phase="market", path=self.path,
            )
        records = trade_journal.load_journal(self.path)
        self.assertEqual(len(records), 3)

    def test_record_close_handles_invalid_input_gracefully(self):
        # ticker None → None 반환
        rec = trade_journal.record_close(
            ticker=None, market="KR", trigger="x",
            exit_price=1, sold_qty=1, path=self.path,
        )
        self.assertIsNone(rec)


class TestMeasurement(unittest.TestCase):
    def _mk(self, ticker, trigger, market, version, pnl_pct, pnl_total):
        return {
            "schema_version": 1,
            "trade_id": f"{ticker}_x",
            "ticker": ticker,
            "market": market,
            "trigger": trigger,
            "pnl_pct": pnl_pct,
            "pnl_total": pnl_total,
            "version": version,
            "exit_time": "2026-05-23 10:00:00",
        }

    def test_overall_metrics(self):
        recs = [
            self._mk("A", "stop_loss", "KR", "v2.3", -5.0, -50000),
            self._mk("A", "trailing_stop", "KR", "v2.3", 7.0, 70000),
            self._mk("B", "time_cut", "US", "v2.3", 0.0, 0),
        ]
        m = measurement.build_metrics(recs)
        self.assertEqual(m["total_trades"], 3)
        ov = m["overall"]
        self.assertEqual(ov["n"], 3)
        self.assertEqual(ov["wins"], 1)
        self.assertEqual(ov["losses"], 1)
        self.assertEqual(ov["flats"], 1)
        self.assertAlmostEqual(ov["win_rate"], 1 / 3, places=3)
        self.assertAlmostEqual(ov["total_pnl"], 20000.0, places=2)
        self.assertAlmostEqual(ov["profit_factor"], 70000 / 50000, places=4)

    def test_group_by_trigger_and_ticker(self):
        recs = [
            self._mk("A", "stop_loss", "KR", "v2.3", -5.0, -50000),
            self._mk("A", "stop_loss", "KR", "v2.3", -3.0, -30000),
            self._mk("B", "trailing_stop", "US", "v2.3", 4.0, 40000),
        ]
        m = measurement.build_metrics(recs)
        self.assertEqual(m["by_trigger"]["stop_loss"]["n"], 2)
        self.assertEqual(m["by_trigger"]["trailing_stop"]["n"], 1)
        self.assertEqual(m["by_ticker"]["A"]["n"], 2)
        self.assertEqual(m["by_ticker"]["B"]["n"], 1)

    def test_empty_metrics_safe(self):
        m = measurement.build_metrics([])
        self.assertEqual(m["total_trades"], 0)
        self.assertEqual(m["overall"]["n"], 0)
        self.assertEqual(m["overall"]["win_rate"], 0.0)

    def test_format_brief(self):
        m_empty = measurement.build_metrics([])
        self.assertIn("측정 데이터 없음", measurement.format_brief(m_empty))
        recs = [self._mk("A", "stop_loss", "KR", "v2.3", -2.0, -20000)]
        m = measurement.build_metrics(recs)
        brief = measurement.format_brief(m)
        self.assertIn("누적 1건", brief)


if __name__ == "__main__":
    unittest.main()
