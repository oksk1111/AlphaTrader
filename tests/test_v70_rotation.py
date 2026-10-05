"""
tests/test_v70_rotation.py — v7.0 모멘텀 로테이션 회귀 테스트

[이 파일이 잠그는 것]

2026-10-01 실데이터 백테스트(scripts/backtest_real.py)에서 v6.1 구조는 KR CAGR +4.7%
/ MDD -46.7%, US CAGR -12.4% / MDD -79.5% 였다. 문제는 매수 타이밍도 손절폭도
아니라 **무엇을 들고 있느냐**(21종목 희석 + 3x 레버리지 + 장중 꼬리 손절)였다.
v7.0 은 참고 저장소 두 곳의 구조를 옮겼다:
  · kr-quant-engine : 듀얼 모멘텀 순위, 상위 N 보유, 버퍼 히스테리시스, 시장필터
  · prism-insight   : 종가 기준 손절 (장중 꼬리는 매도 사유가 아님)

층별로:
  1. 순수 판단 (modules/momentum_rotation)
  2. 배선 (run_bot.run_rotation_session — 가짜 KIS 로 실제 주문 경로까지)
  3. 관측 (대시보드 로그 소스 / 총자산 이중계산)
  4. 검증 루프가 운영 설정을 읽는가
"""

import datetime as dt
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from modules import momentum_rotation as mr  # noqa: E402


def _series(start, step, n=200):
    return [start * (1 + step) ** i for i in range(n)]


# ---------------------------------------------------------------------------
# 1. 순수 판단
# ---------------------------------------------------------------------------
class TestRanking:

    def test_trend_filter_excludes_downtrend(self):
        up = mr.compute_features("UP", _series(100, 0.003))
        down = mr.compute_features("DN", _series(100, -0.003))
        assert up.trend_ok and not down.trend_ok
        assert [t for t, _ in mr.rank_universe([up, down])] == ["UP"]

    def test_stronger_momentum_ranks_first(self):
        a = mr.compute_features("A", _series(100, 0.004))
        b = mr.compute_features("B", _series(100, 0.002))
        c = mr.compute_features("C", _series(100, 0.001))
        assert [t for t, _ in mr.rank_universe([c, a, b])] == ["A", "B", "C"]

    def test_short_history_is_ineligible_not_error(self):
        """신규 상장 ETF 는 121봉 전까지 순위에서 빠질 뿐, 예외를 내지 않는다."""
        f = mr.compute_features("NEW", _series(100, 0.01, n=50))
        assert not f.eligible
        assert mr.rank_universe([f]) == []

    def test_oneil_rs_matches_prism_formula(self):
        closes = _series(100, 0.001, n=300)
        p0 = closes[-1]
        r = lambda n: p0 / closes[-1 - n] - 1  # noqa: E731
        assert mr.oneil_rs_raw(closes) == pytest.approx(2 * r(63) + r(126) + r(189) + r(252))


class TestPlan:

    def _feats(self, strengths):
        return [mr.compute_features(t, _series(100, s)) for t, s in strengths]

    def test_hysteresis_keeps_holding_inside_buffer(self):
        """보유 종목은 3위여도(버퍼 3) 유지 — 순위가 조금 밀렸다고 팔지 않는다."""
        feats = self._feats([("A", .005), ("B", .004), ("C", .003), ("D", .002)])
        plan = mr.plan_rotation(feats, ["C"], True, slots=2, sell_buffer=3)
        assert "C" in plan.keep and not plan.sell
        assert plan.targets == ["C", "A"]

    def test_sell_when_rank_falls_outside_buffer(self):
        feats = self._feats([("A", .006), ("B", .005), ("C", .004), ("D", .002)])
        plan = mr.plan_rotation(feats, ["D"], True, slots=2, sell_buffer=3)
        assert [s for s, _ in plan.sell] == ["D"]
        assert plan.targets == ["A", "B"]

    def test_sell_when_trend_breaks(self):
        feats = self._feats([("A", .004), ("DN", -.003)])
        plan = mr.plan_rotation(feats, ["DN"], True, slots=2, sell_buffer=3)
        assert plan.sell and plan.sell[0][0] == "DN"

    def test_risk_off_liquidates_and_buys_nothing(self):
        """시장필터 veto. 자동 해제 조건: 지수 복귀 (market_risk_on 이 True 가 되는 즉시)."""
        feats = self._feats([("A", .004), ("B", .003)])
        plan = mr.plan_rotation(feats, ["A"], False, slots=2)
        assert plan.targets == [] and [s for s, _ in plan.sell] == ["A"]

    def test_never_holds_more_than_slots(self):
        feats = self._feats([("A", .006), ("B", .005), ("C", .004)])
        plan = mr.plan_rotation(feats, ["A", "B", "C"], True, slots=2, sell_buffer=3)
        assert len(plan.targets) == 2 and [s for s, _ in plan.sell] == ["C"]


class TestMarketFilter:

    def test_uptrend_is_risk_on(self):
        assert mr.market_risk_on(_series(100, 0.002))

    def test_downtrend_is_risk_off(self):
        assert not mr.market_risk_on(_series(100, -0.002))

    def test_insufficient_data_fails_open(self):
        """데이터 조회 실패가 곧 전면 매수 중단이면 v5.x 의 조용한 영구 정지가 재현된다."""
        assert mr.market_risk_on([100.0] * 50)


class TestSlots:

    def test_small_account_gets_fewer_slots(self):
        assert mr.fillable_slots(300_000, 200_000, 2) == 1
        assert mr.fillable_slots(2_000_000, 200_000, 2) == 2
        assert mr.fillable_slots(0, 200_000, 2) == 1


# ---------------------------------------------------------------------------
# 2. 배선 — run_rotation_session 이 실제로 주문까지 가는가
# ---------------------------------------------------------------------------
TODAY = "2026-10-01"


def _rows(closes, include_today=True):
    base = dt.date(2026, 10, 1) - dt.timedelta(days=len(closes) - 1)
    rows = [{"date": (base + dt.timedelta(days=i)).strftime("%Y%m%d"), "close": c,
             "open": c, "high": c, "low": c} for i, c in enumerate(closes)]
    if not include_today:
        rows = rows[:-1]
    return rows


class FakeKR:
    """주문을 기록만 하는 KisDomestic 대역."""

    def __init__(self, series, holdings=None, cash=2_000_000):
        self.series = series                 # code -> closes
        self.holdings = dict(holdings or {})  # code -> (qty, avg)
        self.cash = cash
        self.buys, self.sells = [], []

    def get_daily_history(self, code, exchange=None, bars=200, max_pages=4):
        return _rows(self.series.get(code, []))

    def get_current_price(self, code, exchange=None):
        s = self.series.get(code)
        return s[-1] if s else None

    def get_balance(self):
        o1 = []
        for code, (qty, avg) in self.holdings.items():
            px = self.get_current_price(code) or avg
            o1.append({"pdno": code, "hldg_qty": str(qty), "pchs_avg_pric": str(avg), "prpr": str(px)})
        total = self.cash + sum(q * (self.get_current_price(c) or a) for c, (q, a) in self.holdings.items())
        return {"rt_cd": "0", "output1": o1,
                "output2": [{"prvs_rcdl_excc_amt": str(self.cash), "dnca_tot_amt": str(self.cash),
                             "tot_evlu_amt": str(total)}]}

    def get_holding_qty(self, code):
        return self.holdings.get(code, (0, 0))[0]

    def buy_market_order(self, code, qty, exchange=None):
        px = self.get_current_price(code)
        self.buys.append((code, qty))
        q0, a0 = self.holdings.get(code, (0, 0))
        self.holdings[code] = (q0 + qty, (q0 * a0 + qty * px) / (q0 + qty))
        self.cash -= qty * px
        return {"rt_cd": "0", "msg1": "정상처리"}

    def sell_market_order(self, code, qty, exchange=None):
        self.sells.append((code, qty))
        q0, a0 = self.holdings.pop(code)
        self.cash += q0 * self.get_current_price(code)
        return {"rt_cd": "0", "msg1": "정상처리"}


@pytest.fixture
def rb(monkeypatch, tmp_path):
    import run_bot
    from modules import health_monitor, market_clock

    def fake_session(market, now_utc=None):
        return market_clock.SessionInfo(
            market=market, is_open=True, local_now=dt.datetime(2026, 10, 1, 10, 0),
            open_at=None, close_at=None, minutes_since_open=60.0, minutes_to_close=10.0,
            session_date=TODAY)

    monkeypatch.setattr(market_clock, "session_info", fake_session)
    monkeypatch.setattr(run_bot, "is_market_open_for", lambda m: True)
    monkeypatch.setattr(run_bot, "touch_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(run_bot, "send_alert", lambda *a, **k: None)
    monkeypatch.setattr(run_bot, "_journal_record", lambda *a, **k: None)
    monkeypatch.setattr(run_bot.time, "sleep", lambda s: None)
    monkeypatch.setattr(run_bot, "ROTATION_STATE_FILE", str(tmp_path / "rotation_state.json"))
    monkeypatch.setattr(health_monitor, "HEALTH_FILE", str(tmp_path / "health.json"))
    return run_bot


def _cfg(**over):
    kr = {"universe": ["A", "B", "C", "BENCH"], "benchmark": "BENCH", "slots": 2,
          "sell_buffer": 3, "rebalance_days": 10, "market_filter": True,
          "close_stop_pct": -15.0, "min_slot_value": 200000, "liquidate_non_universe": True}
    kr.update(over)
    return {"strategy": "momentum_rotation",
            "momentum_rotation": {"entry_delay_min": 10, "close_check_before_min": 20, "kr": kr}}


UNIVERSE = {"A": _series(10000, .004), "B": _series(10000, .003),
            "C": _series(10000, .001), "BENCH": _series(10000, .0015)}


class TestSessionWiring:

    def test_first_session_buys_top_two(self, rb, monkeypatch):
        fake = FakeKR(dict(UNIVERSE), cash=2_000_000)
        monkeypatch.setattr(rb, "KisDomestic", lambda: fake)
        rb.run_rotation_session("KR", _cfg())
        assert [c for c, _ in fake.buys] == ["A", "B"]
        assert fake.cash >= 0, "현금을 초과해 주문하면 안 된다"

    def test_legacy_holdings_outside_universe_are_migrated(self, rb, monkeypatch):
        """v6.1 DCA 가 남긴 유니버스 밖 종목(개별주 등)은 첫 리밸런싱에서 정리."""
        series = dict(UNIVERSE, OLD=_series(5000, .004))
        fake = FakeKR(series, holdings={"OLD": (10, 5000)}, cash=1_500_000)
        monkeypatch.setattr(rb, "KisDomestic", lambda: fake)
        rb.run_rotation_session("KR", _cfg())
        assert ("OLD", 10) in fake.sells
        assert {c for c, _ in fake.buys} == {"A", "B"}

    def test_no_new_buys_between_rebalances(self, rb, monkeypatch):
        fake = FakeKR(dict(UNIVERSE), cash=2_000_000)
        monkeypatch.setattr(rb, "KisDomestic", lambda: fake)
        rb.run_rotation_session("KR", _cfg())
        n = len(fake.buys)
        # 다음 영업일로 세션 키만 바꾼다
        from modules import market_clock
        orig = market_clock.session_info
        monkeypatch.setattr(market_clock, "session_info",
                            lambda m, now_utc=None: orig(m).__class__(**{**orig(m).__dict__, "session_date": "2026-10-02"}))
        fake.cash += 1_000_000   # 입금이 생겨도 리밸런싱일 전에는 사지 않는다
        rb.run_rotation_session("KR", _cfg())
        assert len(fake.buys) == n
        state = json.load(open(rb.ROTATION_STATE_FILE, encoding="utf-8"))
        assert state["KR"]["sessions_since_rebalance"] == 1

    def test_failed_buy_is_retried_before_next_rebalance(self, rb, monkeypatch):
        """리밸런싱일 주문 실패 → 10거래일 현금 방치가 아니라 다음 세션에 재시도."""
        fake = FakeKR(dict(UNIVERSE), cash=2_000_000)
        real_buy = fake.buy_market_order
        fake.buy_market_order = lambda c, q, e=None: {"rt_cd": "1", "msg1": "주문가능금액을 초과"} if c == "B" else real_buy(c, q)
        monkeypatch.setattr(rb, "KisDomestic", lambda: fake)
        rb.run_rotation_session("KR", _cfg())
        assert [c for c, _ in fake.buys] == ["A"]
        state = json.load(open(rb.ROTATION_STATE_FILE, encoding="utf-8"))
        assert state["KR"]["pending_buys"] == ["B"]
        fake.buy_market_order = real_buy
        from modules import market_clock
        orig = market_clock.session_info
        monkeypatch.setattr(market_clock, "session_info",
                            lambda m, now_utc=None: orig(m).__class__(**{**orig(m).__dict__, "session_date": "2026-10-02"}))
        rb.run_rotation_session("KR", _cfg())
        assert [c for c, _ in fake.buys] == ["A", "B"]
        state = json.load(open(rb.ROTATION_STATE_FILE, encoding="utf-8"))
        assert state["KR"]["pending_buys"] == [] and state["KR"]["sessions_since_rebalance"] == 1

    def test_risk_off_liquidates_and_state_records_it(self, rb, monkeypatch):
        series = dict(UNIVERSE, BENCH=_series(10000, -.003))
        fake = FakeKR(series, holdings={"A": (10, 10000)}, cash=500_000)
        monkeypatch.setattr(rb, "KisDomestic", lambda: fake)
        rb.run_rotation_session("KR", _cfg())
        assert ("A", 10) in fake.sells and not fake.buys
        state = json.load(open(rb.ROTATION_STATE_FILE, encoding="utf-8"))
        assert state["KR"]["risk_on"] is False

    def test_close_stop_uses_closing_basis(self, rb, monkeypatch):
        """-15% 아래로 마감 직전에 있으면 판다 (장중 꼬리가 아니라 폐장 20분 전 가격)."""
        fake = FakeKR(dict(UNIVERSE), holdings={"A": (10, UNIVERSE["A"][-1] / 0.80)}, cash=0)
        monkeypatch.setattr(rb, "KisDomestic", lambda: fake)
        rb.run_rotation_session("KR", _cfg(rebalance_days=10 ** 9))
        assert ("A", 10) in fake.sells

    def test_us_zero_cash_orders_nothing_and_reports_cause(self, rb, monkeypatch):
        """2026-10-01 실상태: USD 예수금 $0. 주문을 내면 안 되고, 자가진단에는
        '주문가능금액' 사유가 남아 조치 힌트(REMEDY_HINTS)와 매칭돼야 한다.
        원화 예수금을 끌어다 쓰지도 않는다 (시장 간 자금 분리)."""
        from modules import health_monitor
        fake = FakeKR({"SPY": _series(500, .002), "QQQ": _series(400, .003),
                       "SMH": _series(200, .004)}, cash=5_000_000)
        fake.get_foreign_balance = lambda: {"deposit": 0}
        monkeypatch.setattr(rb, "KisOverseas", lambda: fake)
        cfg = {"momentum_rotation": {"us": {
            "universe": [{"symbol": "SPY", "exchange": "AMS"}, {"symbol": "QQQ", "exchange": "NAS"},
                         {"symbol": "SMH", "exchange": "NAS"}],
            "benchmark": "SPY", "slots": 2, "market_filter": False, "min_slot_value": 50}}}
        rb.run_rotation_session("US", cfg)
        assert fake.buys == []
        rec = health_monitor._recent("US", 1)[-1]
        assert rec["buys"] == 0 and rec["candidates"] > 0
        assert any("주문가능금액" in k for k in rec["block_reasons"])

    def test_us_empty_holdings_is_not_a_crash(self, rb):
        """2026-10-01 배포 이후 US 세션이 매일 '잔고 조회 실패'로 크래시했다.
        KisOverseas.get_balance() 가 보유 0 을 None(=실패)으로 반환했기 때문."""
        fake = FakeKR({"SPY": _series(500, .002)}, cash=0)
        fake.get_balance = lambda: {"output1": [], "_failed_exchanges": []}
        fake.get_foreign_balance = lambda: {"deposit": 1000.0}
        holdings, cash, equity = rb._rotation_account(fake, "US")
        assert holdings == {} and cash == 1000.0 and equity == 1000.0

    @pytest.mark.parametrize("bal", [None, {"output1": [], "_failed_exchanges": ["AMEX"]}])
    def test_balance_failure_still_raises(self, rb, bal):
        """반대 방향: 조회 실패를 '보유 0'으로 읽으면 이미 가진 종목을 또 산다."""
        fake = FakeKR({}, cash=0)
        fake.get_balance = lambda: bal
        fake.get_foreign_balance = lambda: {"deposit": 1000.0}
        with pytest.raises(RuntimeError):
            rb._rotation_account(fake, "US")

    def test_kis_overseas_balance_distinguishes_empty_from_failure(self, monkeypatch):
        # 다른 테스트가 sys.modules['modules.kis_api'] 를 스텁으로 바꿔 끼우므로 실제 파일을 직접 로드한다.
        import importlib.util
        spec = importlib.util.spec_from_file_location("_real_kis_api", os.path.join(REPO, "modules", "kis_api.py"))
        kis_api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(kis_api)

        class Resp:
            def __init__(self, body):
                self.body, self.status_code, self.text = body, 200, ""
            def raise_for_status(self):
                pass
            def json(self):
                return self.body

        k = kis_api.KisOverseas.__new__(kis_api.KisOverseas)
        k.url, k.acc_no_prefix, k.acc_no_suffix = "https://x", "1", "01"
        monkeypatch.setattr(k, "_get_headers", lambda tr: {}, raising=False)

        monkeypatch.setattr(kis_api.requests, "get", lambda *a, **kw: Resp({"rt_cd": "0", "output1": []}))
        assert k.get_balance() == {"output1": [], "_failed_exchanges": []}

        monkeypatch.setattr(kis_api.requests, "get", lambda *a, **kw: Resp({"rt_cd": "1", "msg1": "err"}))
        assert k.get_balance() is None

    def test_signal_ignores_todays_partial_bar(self, rb):
        """백테스트는 't일 종가 판단 → t+1 체결'. 봇도 오늘 미완성 봉을 쓰면 안 된다."""
        rows = _rows([1, 2, 3])
        assert rb._completed_closes(rows, "KR") == [1, 2]

    def test_job_dispatches_to_rotation(self, rb, monkeypatch):
        called = {}
        monkeypatch.setattr(rb, "load_config", lambda: _cfg())
        monkeypatch.setattr(rb, "get_market_status", lambda: "KR")
        monkeypatch.setattr(rb, "run_rotation_session", lambda m, c: called.setdefault("m", m))
        rb.job()
        assert called.get("m") == "KR"


# ---------------------------------------------------------------------------
# 3. 관측 계층
# ---------------------------------------------------------------------------
class TestObservability:

    def test_activity_reads_current_bot_log_not_only_legacy(self):
        """v6.1 은 활동 요약이 trading_*.log(구파일)만 읽어 '마지막 거래 96일 전'을 표시했다."""
        src = io.open(os.path.join(REPO, "web", "app.py"), encoding="utf-8").read()
        body = src.split("def load_recent_log_events", 1)[1].split("\ndef ", 1)[0]
        assert "bot_log_files" in body
        assert 'glob.glob(str(BASE_DIR / "database" / "trading_*.log")))[-limit_files:]' not in body

    def test_bot_log_files_orders_rotated_before_current(self, tmp_path, monkeypatch):
        import importlib
        app = importlib.import_module("web.app")
        db = tmp_path / "database"
        db.mkdir()
        for name in ("trading_20260908.log", "trading.log.2026-09-29", "trading.log.2026-09-30", "trading.log"):
            (db / name).write_text("x", encoding="utf-8")
        monkeypatch.setattr(app, "BASE_DIR", tmp_path)
        files = [os.path.basename(f) for f in app.bot_log_files()]
        assert files == ["trading_20260908.log", "trading.log.2026-09-29", "trading.log.2026-09-30", "trading.log"]

    def test_kr_total_not_double_counted(self):
        """tot_evlu_amt 는 예수금 포함. 9/11 실측: 보유 0종목에서 deposit == eval_total."""
        # test_v25/test_v30 이 sys.modules['modules.profit_tracker'] 를 MagicMock 으로
        # 바꿔 두므로, 실행 순서에 상관없이 실제 파일에서 직접 로드한다.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_pt_real", os.path.join(REPO, "modules", "profit_tracker.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        kr_account_total = mod.kr_account_total
        assert kr_account_total({"deposit": 2715678, "eval_total": 2715678}) == 2715678
        assert kr_account_total({"deposit": 1681140, "eval_total": 2030030}) == 2030030
        assert kr_account_total({"deposit": 500, "eval_total": 0}) == 500


# ---------------------------------------------------------------------------
# 4. 운영 설정 / 검증 루프
# ---------------------------------------------------------------------------
class TestConfig:

    def _cfg(self):
        return json.load(io.open(os.path.join(REPO, "user_config.json"), encoding="utf-8"))

    def test_production_uses_rotation(self):
        assert self._cfg()["strategy"] == "momentum_rotation"

    def test_no_leverage_or_single_stocks_in_universe(self):
        """실데이터에서 3x 포함 구조는 MDD -69~-80%. 개별주/단일종목 레버리지도 제외."""
        rc = self._cfg()["momentum_rotation"]
        us = {u["symbol"] for u in rc["us"]["universe"]}
        assert not us & {"TQQQ", "SOXL", "UPRO", "TECL", "FNGU", "NVDL"}
        kr = set(rc["kr"]["universe"])
        assert not kr & {"000660", "005930", "012450", "005380", "035420", "0193T0", "0193W0", "122630", "233740"}

    def test_markets_do_not_share_cash(self):
        """9/22 통합증거금으로 KR 매수가 USD 를 소진해 US 가 0 이 됐다. 로테이션 KR 은 원화만."""
        src = io.open(os.path.join(REPO, "run_bot.py"), encoding="utf-8").read()
        body = src.split("def _rotation_account", 1)[1].split("\ndef ", 1)[0]
        assert "prvs_rcdl_excc_amt" in body and "USD_KRW_RATE" not in body

    def test_backtest_reads_operational_config(self):
        """AGENTS 규칙 8 — 검증이 운영 설정을 읽지 않으면 검증하지 않는 검증이 된다."""
        sys.path.insert(0, os.path.join(REPO, "scripts"))
        import backtest_real
        assert backtest_real._load_rotation("KR") == self._cfg()["momentum_rotation"]["kr"]

    def test_opro_and_scanner_skip_in_rotation(self):
        src = io.open(os.path.join(REPO, "run_bot.py"), encoding="utf-8").read()
        scan = src.split("def run_scanner", 1)[1].split("def update_dynamic_portfolio", 1)[0]
        assert "_rotation_active()" in scan
        assert "not opro_triggered_today and _rotation_active()" in src
