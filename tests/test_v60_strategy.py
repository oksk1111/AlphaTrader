"""
tests/test_v60_strategy.py — v6.0 회귀 테스트

이 파일의 모든 테스트는 "몇 달째 거래가 안 되던" 실제 원인 5가지에 1:1로 대응한다.
각 테스트는 v5.1 코드에서는 반드시 실패하고, v6.0 에서는 통과해야 한다.
"""

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import market_clock, risk_state, health_monitor
from modules.decision_engine import (
    TickerContext, PortfolioContext, evaluate_buy,
)


def _utc(iso):
    return dt.datetime.fromisoformat(iso).replace(tzinfo=dt.timezone.utc)


def _pctx(**kw):
    base = dict(available_cash=100000.0, minutes_since_open=120.0, minutes_to_close=200.0)
    base.update(kw)
    return PortfolioContext(**base)


# ===========================================================================
# 원인 ①: 미국 서머타임(DST) 미반영 — 하드코딩된 KST 23:30~06:00
# ===========================================================================
class TestMarketClockDST:
    def test_us_open_during_dst_is_2230_kst(self):
        """서머타임(9월)에는 22:30 KST 에 이미 개장해 있어야 한다.
        v5.1 은 23:30 이전을 CLOSED 로 봐서 매일 첫 1시간을 통째로 버렸다."""
        assert market_clock.get_market_status(_utc("2026-09-08T13:30")) == "US"  # 22:30 KST
        assert market_clock.get_market_status(_utc("2026-09-08T13:25")) == "CLOSED"

    def test_us_closed_after_0500_kst_during_dst(self):
        """서머타임에는 05:00 KST 에 폐장. v5.1 은 06:00 까지 '개장'으로 믿고
        닫힌 시장에 손절 시장가를 던졌다."""
        assert market_clock.get_market_status(_utc("2026-09-08T19:59")) == "US"     # 04:59 KST
        assert market_clock.get_market_status(_utc("2026-09-08T20:01")) == "CLOSED"  # 05:01 KST

    def test_us_standard_time_shifts_one_hour(self):
        """표준시(1월)에는 23:30 KST 개장이 맞다 — 두 경우 모두 성립해야 한다."""
        assert market_clock.get_market_status(_utc("2026-01-15T14:30")) == "US"
        assert market_clock.get_market_status(_utc("2026-01-15T14:25")) == "CLOSED"

    def test_us_holiday_is_closed(self):
        """2026-09-07 Labor Day 는 휴장. v5.1 은 휴장일 개념이 없었다."""
        assert market_clock.get_market_status(_utc("2026-09-07T14:00")) == "CLOSED"

    def test_kr_session_window(self):
        # 2026-09-08(화) 00:30 UTC = 09:30 KST → 개장
        assert market_clock.get_market_status(_utc("2026-09-08T00:30")) == "KR"
        # 06:30 UTC = 15:30 KST → 종가 단일가 구간, 시장가 체결 불가
        assert market_clock.get_market_status(_utc("2026-09-08T06:30")) == "CLOSED"

    def test_session_key_stable_across_kst_midnight(self):
        """세션 키는 거래소 현지 영업일 기준이라 KST 자정(=미국장 한복판)에
        바뀌면 안 된다. v5.1 은 KST 날짜를 써서 진행 중인 US 세션의 플래그가
        리셋됐다."""
        before = market_clock.session_key("US", _utc("2026-09-08T14:50"))  # 23:50 KST
        after = market_clock.session_key("US", _utc("2026-09-08T15:10"))   # 00:10 KST(익일)
        assert before == after == "US:2026-09-08"

    def test_is_market_open_for_matches_status(self):
        t = _utc("2026-09-08T13:35")
        assert market_clock.is_market_open_for("US", t) is True
        assert market_clock.is_market_open_for("KR", t) is False


# ===========================================================================
# 원인 ②③: 하루 1회 이진 게이트 + DCA 와 모순되는 추세 필터
# ===========================================================================
class TestDecisionEngine:
    def _pullback(self, **kw):
        """장기추세는 살아있고 중기 조정 중 — v5.1 이 'MA20 하회'로 전량 차단하던 상태."""
        base = dict(ticker="QQQ", market="US", price=100.0,
                    ma5=101.0, ma20=103.0, ma60=95.0, day_low=99.0, gap_pct=-1.5)
        base.update(kw)
        return TickerContext(**base)

    def test_pullback_buys_instead_of_blocking(self):
        """v5.1: MA20 하회 → 차단(그날 끝). v6.0: 눌림목으로 인식해 매수."""
        d = evaluate_buy(self._pullback(), _pctx())
        assert d.action == "buy"
        assert 0.0 < d.size_multiplier <= 1.0

    def test_uptrend_buys_larger_than_pullback(self):
        """정배열은 눌림목보다 사이즈가 커야 한다 (점수→사이즈 단조성)."""
        up = TickerContext("QQQ", "US", 110.0, ma5=108.0, ma20=104.0, ma60=98.0, day_low=107.0)
        assert evaluate_buy(up, _pctx()).size_multiplier > \
               evaluate_buy(self._pullback(), _pctx()).size_multiplier

    def test_falling_knife_defers_not_vetoes(self):
        """전 이평 하회 + 저점 부근 = 매수 안 함. 단 veto 가 아니라 defer 여야
        한다 — 장중 반등하면 같은 세션에서 다시 살 수 있어야 하기 때문."""
        knife = TickerContext("SOXL", "US", 80.0, ma5=88.0, ma20=95.0, ma60=105.0,
                              day_low=79.9, gap_pct=-3.0, consec_decline_pct=9.0,
                              is_leveraged=True)
        d = evaluate_buy(knife, _pctx())
        assert d.action == "defer"
        assert d.veto_reason == ""

    def test_same_knife_buys_after_intraday_bounce(self):
        """같은 종목이 당일 저점 대비 +2% 반등하면 소액 매수로 전환.
        이것이 v6.0 의 핵심 — 판단은 하루 1회가 아니라 장중 내내 갱신된다."""
        bounced = TickerContext("SOXL", "US", 80.0, ma5=88.0, ma20=95.0, ma60=105.0,
                                day_low=78.4, gap_pct=-3.0, consec_decline_pct=9.0,
                                is_leveraged=True)
        d = evaluate_buy(bounced, _pctx())
        assert d.action == "buy"
        assert d.size_multiplier < 0.5, "하락추세에서의 매수는 소액이어야 한다"

    def test_leveraged_sized_smaller_than_plain(self):
        common = dict(market="US", price=100.0, ma5=99.0, ma20=97.0, ma60=90.0, day_low=98.0)
        plain = evaluate_buy(TickerContext(ticker="QQQ", **common), _pctx())
        lev = evaluate_buy(TickerContext(ticker="TQQQ", is_leveraged=True, **common), _pctx())
        assert lev.size_multiplier < plain.size_multiplier

    def test_opening_30min_halves_size(self):
        c = TickerContext("QQQ", "US", 110.0, ma5=108.0, ma20=104.0, ma60=98.0, day_low=107.0)
        early = evaluate_buy(c, _pctx(minutes_since_open=5))
        later = evaluate_buy(c, _pctx(minutes_since_open=120))
        assert early.size_multiplier == pytest.approx(later.size_multiplier / 2, rel=0.05)


class TestHardVetoes:
    def _ok(self, **kw):
        base = dict(ticker="QQQ", market="US", price=100.0, ma5=99.0, ma20=97.0,
                    ma60=90.0, day_low=98.0)
        base.update(kw)
        return TickerContext(**base)

    def test_sector_crash_vetoes(self):
        """v4.0 사망 원인: 반도체 붕괴 뉴스를 무시하고 SOXL 을 계속 물타기."""
        c = self._ok(sector_sentiment={"market_condition": "CRASH", "reason": "수출규제"})
        assert evaluate_buy(c, _pctx()).action == "veto"

    def test_panic_gap_down_vetoes(self):
        assert evaluate_buy(self._ok(gap_pct=-9.0), _pctx()).action == "veto"

    def test_position_cap_is_hard_veto_not_a_score_penalty(self):
        """노출 한도는 감점이 아니라 하드 한도. 감점(가중치 0.10)으로 두면
        다른 지표가 좋을 때 한도를 넘겨서도 계속 사버린다."""
        c = self._ok(position_value=1000.0, max_position_value=1000.0)
        assert evaluate_buy(c, _pctx()).action == "veto"

    def test_position_below_cap_still_buys(self):
        c = self._ok(position_value=500.0, max_position_value=1000.0)
        assert evaluate_buy(c, _pctx()).action == "buy"

    def test_insufficient_cash_vetoes(self):
        assert evaluate_buy(self._ok(), _pctx(available_cash=10.0)).action == "veto"

    def test_near_close_vetoes(self):
        assert evaluate_buy(self._ok(), _pctx(minutes_to_close=10)).action == "veto"

    def test_stopped_out_today_vetoes(self):
        """v5.1 휩쏘 가드 유지 — 손절 직후 재매수 금지."""
        assert evaluate_buy(self._ok(stopped_out_today=True), _pctx()).action == "veto"

    def test_correlation_cap_only_blocks_new_entries(self):
        """상관그룹 한도는 신규 진입만 막고, 이미 보유한 종목의 추가 분할매수는
        막지 않아야 한다 (그렇지 않으면 DCA 자체가 불가능해진다)."""
        new = self._ok(correlation_capped=True, holding_qty=0)
        held = self._ok(correlation_capped=True, holding_qty=10, holding_avg_price=105.0)
        assert evaluate_buy(new, _pctx()).action == "veto"
        assert evaluate_buy(held, _pctx()).action == "buy"


# ===========================================================================
# 원인 ④: 드로다운 '서킷브레이커'가 해제조건 없는 영구 래치였던 문제
# ===========================================================================
class TestRiskState:
    @pytest.fixture(autouse=True)
    def _clean(self, tmp_path, monkeypatch):
        monkeypatch.setattr(risk_state, "STATE_DIR", str(tmp_path))
        monkeypatch.setattr(risk_state, "STATE_FILE", str(tmp_path / "risk_state.json"))
        yield

    def test_drawdown_is_measured_from_peak_not_cost(self):
        risk_state.update_equity(10_000_000)
        snap = risk_state.update_equity(9_000_000)
        assert snap.drawdown_pct == pytest.approx(10.0)

    def test_recovery_does_not_keep_drawdown(self):
        risk_state.update_equity(10_000_000)
        risk_state.update_equity(9_000_000)
        snap = risk_state.update_equity(10_500_000)
        assert snap.drawdown_pct == 0.0
        assert snap.peak_equity == pytest.approx(10_500_000)

    def test_ladder_scales_size_instead_of_full_block(self):
        """-7% 에서 v5.1 은 전면 차단(영구)했다. v6.0 은 60% 사이즈로 계속 매수."""
        risk_state.update_equity(10_000_000)
        snap = risk_state.update_equity(9_300_000)
        assert snap.halted is False
        assert snap.size_multiplier == pytest.approx(0.6)

    def test_hard_halt_only_at_extreme_drawdown(self):
        risk_state.update_equity(10_000_000)
        snap = risk_state.update_equity(8_100_000)   # -19%
        assert snap.halted is True
        assert snap.size_multiplier == 0.0

    def test_halt_auto_releases_on_recovery(self):
        """해제 조건이 존재해야 한다. v5.1 에는 아예 없었다."""
        risk_state.update_equity(10_000_000)
        assert risk_state.update_equity(8_100_000).halted is True
        released = risk_state.update_equity(9_100_000)   # 낙폭 9% <= 19%/2
        assert released.halted is False
        assert released.size_multiplier > 0

    def test_zero_equity_does_not_poison_peak(self):
        """잔고 조회 실패(0)가 고점을 0으로 만들어 낙폭 계산을 망가뜨리면 안 된다."""
        risk_state.update_equity(10_000_000)
        snap = risk_state.update_equity(0)
        assert snap.peak_equity == pytest.approx(10_000_000)


# ===========================================================================
# 원인 ⑤: 조용한 실패 — 몇 달간 아무도 몰랐던 문제
# ===========================================================================
class TestHealthMonitor:
    @pytest.fixture(autouse=True)
    def _clean(self, tmp_path, monkeypatch):
        monkeypatch.setattr(health_monitor, "STATE_DIR", str(tmp_path))
        monkeypatch.setattr(health_monitor, "HEALTH_FILE", str(tmp_path / "health.json"))
        yield

    def _dry(self, n, reason="주문가능금액 부족 (현금 0 < 1주 512)", market="US"):
        for i in range(1, n + 1):
            health_monitor.record_session(health_monitor.SessionRecord(
                session_key=f"{market}:2026-08-{i:02d}", market=market, started_at="x",
                candidates=5, buys=0, evaluations=60, block_reasons={reason: 60}))

    def test_detects_long_dry_streak_as_critical(self):
        self._dry(12)
        d = health_monitor.diagnose("US")
        assert d.level == "critical"
        assert d.dry_streak == 12

    def test_short_streak_is_not_alerted(self):
        """하루 0건은 정상. 노이즈를 내면 알림 자체가 무시된다."""
        self._dry(2)
        assert health_monitor.diagnose("US").should_alert is False

    def test_reports_dominant_reason_and_remedy(self):
        """'거래가 없습니다'가 아니라 '왜' 와 '무엇을 하라'를 말해야 한다."""
        self._dry(8)
        d = health_monitor.diagnose("US")
        assert "주문가능금액" in d.dominant_reason
        assert "예수금" in d.remedy

    def test_streak_resets_after_a_fill(self):
        self._dry(8)
        health_monitor.record_session(health_monitor.SessionRecord(
            session_key="US:2026-08-20", market="US", started_at="x",
            candidates=5, buys=1))
        assert health_monitor.diagnose("US").dry_streak == 0

    def test_alerts_even_when_holdings_exist(self):
        """v5.0 워치독의 사각지대: 보유 종목이 있으면 영원히 침묵했다.
        실제 사고가 정확히 이 상태였다."""
        for i in range(1, 9):
            health_monitor.record_session(health_monitor.SessionRecord(
                session_key=f"US:2026-08-{i:02d}", market="US", started_at="x",
                candidates=5, buys=0, sells=0,
                block_reasons={"추세 이탈": 5}))
        assert health_monitor.diagnose("US").should_alert is True

    def test_sessions_without_candidates_are_not_counted_dry(self):
        """휴장/포트폴리오 공백 세션을 무매수로 세면 거짓 경보가 난다."""
        for i in range(1, 6):
            health_monitor.record_session(health_monitor.SessionRecord(
                session_key=f"KR:2026-08-{i:02d}", market="KR", started_at="x",
                candidates=0, buys=0))
        assert health_monitor.diagnose("KR").dry_streak == 0
