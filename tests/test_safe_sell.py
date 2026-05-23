"""tests/test_safe_sell.py

run_bot.safe_sell() 및 KIS 잔고 재조회 fallback 로직 단위 테스트.
실제 KIS API 를 호출하지 않도록 모듈을 mocking 한다.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# 프로젝트 루트
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, os.pardir))
sys.path.insert(0, ROOT)

# config 의존성을 우회하기 위해 환경변수 가짜 채움 (import 시 KIS 토큰 발급 시도 방지)
os.environ.setdefault("KIS_BASE_URL", "https://openapivts.koreainvestment.com:29443")
os.environ.setdefault("KIS_APP_KEY", "TEST")
os.environ.setdefault("KIS_APP_SECRET", "TEST")
os.environ.setdefault("KIS_CANO", "00000000")
os.environ.setdefault("KIS_ACNT_PRDT_CD", "01")
os.environ.setdefault("DISCORD_WEBHOOK_URL", "")


class FakeKR:
    """KIS 국내 매도 클라이언트 mock."""

    def __init__(self, holding=10, sell_responses=None, current_price=70000):
        self.holding = holding
        self.sell_responses = list(sell_responses or [])
        self.limit_responses = []
        self.current_price = current_price
        self.calls = []

    def get_holding_qty(self, ticker):
        return self.holding

    def get_current_price(self, ticker):
        return self.current_price

    def sell_market_order(self, ticker, qty):
        self.calls.append(('market', ticker, qty))
        if self.sell_responses:
            return self.sell_responses.pop(0)
        return {'rt_cd': '1', 'msg1': 'fail'}

    def sell_limit_order(self, ticker, qty, price):
        self.calls.append(('limit', ticker, qty, price))
        if self.limit_responses:
            return self.limit_responses.pop(0)
        return {'rt_cd': '1', 'msg1': 'fail'}


def _import_safe_sell():
    """run_bot 모듈을 안전하게 import (외부 의존성 mocking)."""
    # 무거운 외부 의존성 mocking
    fake_modules = {
        'modules.kis_api': MagicMock(),
        'modules.kis_domestic': MagicMock(),
        'modules.gemini_analyst': MagicMock(),
        'modules.telegram_notifier': MagicMock(),
        'modules.account_manager': MagicMock(),
        'modules.market_scanner': MagicMock(),
        'modules.multi_llm': MagicMock(),
        'modules.auto_strategy': MagicMock(),
        'modules.portfolio_manager': MagicMock(),
        'modules.trade_journal': MagicMock(),
        'schedule': MagicMock(),
    }
    with patch.dict('sys.modules', fake_modules):
        import importlib
        if 'run_bot' in sys.modules:
            del sys.modules['run_bot']
        run_bot = importlib.import_module('run_bot')
        return run_bot


class TestSafeSell(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.run_bot = _import_safe_sell()
        # 테스트 격리: 실제 trade_journal 파일 오염 방지
        cls.run_bot._journal_record = lambda *a, **k: None

    def _force_market_open(self, market='KR'):
        # is_market_open_for 가 True 반환하도록 patch
        return patch.object(self.run_bot, 'is_market_open_for', return_value=True)

    def test_already_flat_returns_success(self):
        """실제 보유 수량이 0이면 already_flat 처리(이미 청산됨)."""
        fake = FakeKR(holding=0)
        with self._force_market_open():
            res = self.run_bot.safe_sell(fake, 'KR', '005930', qty_hint=5,
                                         reason='test')
        self.assertEqual(res['phase'], 'already_flat')
        self.assertTrue(res['success'])
        self.assertEqual(res['sold_qty'], 0)
        # 매도 주문이 전혀 발생하지 않아야 함
        self.assertEqual(fake.calls, [])

    def test_market_sell_success_first_try(self):
        fake = FakeKR(holding=10, sell_responses=[{'rt_cd': '0'}])
        with self._force_market_open():
            res = self.run_bot.safe_sell(fake, 'KR', '005930', qty_hint=10,
                                         reason='test')
        self.assertTrue(res['success'])
        self.assertEqual(res['phase'], 'market')
        self.assertEqual(res['sold_qty'], 10)
        self.assertEqual(len(fake.calls), 1)

    def test_qty_hint_clamped_to_actual_holding(self):
        """캐시 수량이 실제 보유보다 클 때 실제 수량으로 줄여서 매도."""
        fake = FakeKR(holding=3, sell_responses=[{'rt_cd': '0'}])
        with self._force_market_open():
            res = self.run_bot.safe_sell(fake, 'KR', '000660', qty_hint=10,
                                         reason='test')
        self.assertTrue(res['success'])
        self.assertEqual(res['sold_qty'], 3)
        # 시장가 호출 qty 확인
        self.assertEqual(fake.calls[0], ('market', '000660', 3))

    def test_limit_fallback_when_market_fails(self):
        # 시장가 3회 모두 실패 → 지정가 1회 성공
        fake = FakeKR(
            holding=5,
            sell_responses=[
                {'rt_cd': '1', 'msg1': 'reject'},
                {'rt_cd': '1', 'msg1': 'reject'},
                {'rt_cd': '1', 'msg1': 'reject'},
            ],
            current_price=70000,
        )
        fake.limit_responses = [{'rt_cd': '0'}]
        with self._force_market_open():
            res = self.run_bot.safe_sell(fake, 'KR', '005930', qty_hint=5,
                                         reason='test')
        self.assertTrue(res['success'])
        self.assertEqual(res['phase'], 'limit')
        # 시장가 3회 + 지정가 1회 = 총 4회
        self.assertEqual(len(fake.calls), 4)
        self.assertEqual(fake.calls[-1][0], 'limit')

    def test_failed_when_both_fail(self):
        fake = FakeKR(
            holding=5,
            sell_responses=[{'rt_cd': '1', 'msg1': 'reject'}] * 3,
        )
        fake.limit_responses = [{'rt_cd': '1', 'msg1': 'limit reject'}] * 2
        with self._force_market_open():
            res = self.run_bot.safe_sell(fake, 'KR', '005930', qty_hint=5,
                                         reason='test')
        self.assertFalse(res['success'])
        self.assertEqual(res['phase'], 'failed')
        self.assertIn('reject', (res['error'] or ''))

    def test_deferred_when_market_closed(self):
        fake = FakeKR(holding=5)
        with patch.object(self.run_bot, 'is_market_open_for', return_value=False):
            res = self.run_bot.safe_sell(fake, 'KR', '005930', qty_hint=5,
                                         reason='test')
        self.assertFalse(res['success'])
        self.assertEqual(res['phase'], 'deferred')
        # 어떤 매도 주문도 발생하지 않아야 함
        self.assertEqual(fake.calls, [])

    def test_kis_domestic_tick_size(self):
        """KRX 호가단위 헬퍼 검증."""
        from modules.kis_domestic import KisDomestic
        cases = [
            (1500, 1),
            (3000, 5),
            (15000, 10),
            (45000, 50),
            (150000, 100),
            (300000, 500),
            (700000, 1000),
        ]
        for price, expected in cases:
            self.assertEqual(KisDomestic._tick_size(price), expected,
                             msg=f"price={price}")

    def test_partial_tp_constants_present(self):
        """v2.4 부분 익절 상수가 정의되어 있어야 함."""
        self.assertTrue(hasattr(self.run_bot, 'PARTIAL_TP_ENABLED'))
        self.assertTrue(hasattr(self.run_bot, 'PARTIAL_TP_RATIO'))
        self.assertTrue(hasattr(self.run_bot, 'PARTIAL_TP_MIN_QTY'))
        # 기본값 무결성
        self.assertIsInstance(self.run_bot.PARTIAL_TP_ENABLED, bool)
        self.assertGreater(self.run_bot.PARTIAL_TP_RATIO, 0.0)
        self.assertLess(self.run_bot.PARTIAL_TP_RATIO, 1.0)
        self.assertGreaterEqual(self.run_bot.PARTIAL_TP_MIN_QTY, 1)

    def test_partial_sell_via_safe_sell(self):
        """safe_sell 이 partial qty 만 매도하고 ratio 만큼만 체결하는지 검증."""
        fake = FakeKR(holding=10, sell_responses=[{'rt_cd': '0'}])
        with self._force_market_open():
            res = self.run_bot.safe_sell(
                fake, 'KR', '005930', qty_hint=5,  # 10주 중 50% = 5주
                reason='부분익절',
                monitor_data={'buy_price': 70000, 'buys': 10},
            )
        self.assertTrue(res['success'])
        self.assertEqual(res['sold_qty'], 5)
        self.assertEqual(res['phase'], 'market')
        # 5주만 시장가로 발주됐어야 함
        self.assertEqual(fake.calls, [('market', '005930', 5)])


if __name__ == '__main__':
    unittest.main(verbosity=2)
