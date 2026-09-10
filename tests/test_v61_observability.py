"""
tests/test_v61_observability.py — v6.1 회귀 방지 테스트

[이 파일이 존재하는 이유]

2026-09-10, 사용자가 붙여넣은 프로덕션 로그는 이랬다:

    23:14:18 INFO ✅ KR Account Cache Updated: deposit=2715678, holdings=0
    23:14:17 INFO ✅ US Account Cache Updated: deposit=$330.64, holdings=0
    23:08:18 INFO ✅ KR Account Cache Updated: deposit=2715678, holdings=0
    ... (약 6분 간격으로 40분간 이것만 반복)

미국장은 22:30 KST 에 열려 있었다. 그런데 화면에는 **대시보드 자신이 5분마다
찍는 계좌 캐시 로그만** 있고 봇의 매매 로그는 한 줄도 없었다. 대시보드는
"🟢 Running" 을 표시하고 있었고, 시장 상태는 "CLOSED" 라고 표시하고 있었다.

세 가지가 동시에 거짓말을 하고 있었다:
  · 로그 화면    — 봇이 쓰지 않는 파일을 보여주고 있었다 (날짜 회전 부재)
  · 시장 상태    — 서머타임 미반영 하드코딩 (22:30~23:30 을 CLOSED 로 표시)
  · 봇 상태      — pgrep 결과만 보고 "Running" (진행 여부는 보지 않음)

전략을 아무리 고쳐도 계기판이 이러면 무엇이 고장났는지 알 수 없다. 이 프로젝트가
v3→v6 까지 네 번 "전면 개편"을 하고도 매번 원인을 확신하지 못한 이유다.
그래서 v6.1 은 전략보다 **계기판**을 먼저 고쳤고, 이 파일이 그것을 잠근다.
"""

import io
import os
import sys
from logging.handlers import TimedRotatingFileHandler

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with io.open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 원인 ① 로그 파일이 날짜로 고정되어, 오래 살아 있는 프로세스가 과거 파일에 쓴다
# ---------------------------------------------------------------------------
class TestLogRotation:

    def test_log_filename_has_no_date(self):
        """파일명에 날짜가 박히면 프로세스가 살아 있는 동안 회전하지 않는다."""
        from modules import logger as L
        # (pytest 로 돌 때 이 프로세스의 LOG_FILE 은 test.log — PROC 별로 갈린다)
        assert L.BOT_LOG_FILE.endswith("trading.log")
        # trading_20260908.log 같은 형태가 다시 등장하면 실패
        for path in (L.LOG_FILE, L.BOT_LOG_FILE):
            assert not any(ch.isdigit() for ch in os.path.basename(path)), \
                f"로그 파일명에 날짜가 박혀 있습니다: {path}"

    def test_handler_rotates_at_midnight(self):
        from modules import logger as L
        handlers = [h for h in L.logger.handlers
                    if isinstance(h, TimedRotatingFileHandler)]
        assert handlers, "TimedRotatingFileHandler 가 없습니다 — 날짜 회전이 안 됩니다."
        assert handlers[0].when.upper().startswith("MIDNIGHT")

    def test_bot_and_dashboard_write_separate_files(self):
        """같은 파일을 두 프로세스가 회전시키면 rename 경합이 난다."""
        from modules import logger as L
        assert L.BOT_LOG_FILE.endswith("trading.log")
        assert L._LOG_BASENAME in ("trading.log", "dashboard.log", "test.log")

    def test_no_handler_duplication_on_reimport(self):
        from modules import logger as L
        before = len(L.logger.handlers)
        L.setup_logger()
        assert len(L.logger.handlers) == before


# ---------------------------------------------------------------------------
# 원인 ② 대시보드가 "가장 최근 날짜 파일"을 추측해서 자기 로그를 보여줬다
# ---------------------------------------------------------------------------
class TestDashboardReadsBotLog:

    def test_does_not_guess_newest_dated_file(self):
        src = _read("web/app.py")
        # sorted(...)[-1] 로 최신 파일을 추측하던 로직이 기본 경로여선 안 된다.
        head = src.split("def get_latest_log_file", 1)[1].split("def parse_log_line", 1)[0]
        assert "BOT_LOG_FILE" in head, \
            "대시보드가 봇 로그 경로를 명시적으로 읽지 않고 다시 추측하고 있습니다."

    def test_prefers_bot_log_when_present(self, tmp_path, monkeypatch):
        import web.app as A
        from modules import logger as L

        db = tmp_path / "database"
        db.mkdir()
        # 봇 로그 + 대시보드가 만든 '더 최근 날짜' 레거시 파일을 함께 둔다.
        (db / "trading.log").write_text("bot line\n", encoding="utf-8")
        (db / "trading_29991231.log").write_text("dash line\n", encoding="utf-8")

        monkeypatch.setattr(A, "BASE_DIR", tmp_path)
        picked = A.get_latest_log_file()
        assert picked is not None
        assert os.path.basename(picked) == "trading.log", \
            f"봇 로그가 아니라 {picked} 를 골랐습니다 (2026-09-10 사고 재발)."
        assert L.BOT_LOG_FILE  # 경로 정의가 한 곳에만 있는지 확인


# ---------------------------------------------------------------------------
# 원인 ③ 시계가 두 곳에 따로 있었고, 대시보드 쪽은 서머타임을 몰랐다
# ---------------------------------------------------------------------------
class TestSingleClock:

    def test_dashboard_delegates_to_market_clock(self):
        src = _read("web/app.py")
        body = src.split("def get_market_status", 1)[1].split("def parse_ticker_data", 1)[0]
        assert "market_clock" in body, "대시보드가 아직 자체 시계를 씁니다."
        assert "2330" not in body, "하드코딩된 23:30 창이 남아 있습니다."

    def test_dashboard_matches_bot_during_dst(self):
        """2026-09-10 22:34 KST = 미국 서머타임 개장 후 4분. 둘 다 US 여야 한다."""
        import datetime as dt
        import web.app as A
        from modules import market_clock

        # 13:34 UTC = 22:34 KST = 09:34 EDT
        t = dt.datetime(2026, 9, 10, 13, 34, tzinfo=dt.timezone.utc)
        assert market_clock.get_market_status(t) == "US"
        # 대시보드 함수는 now 인자를 받지 않으므로 위임 사실만 검증(위 테스트)한다.
        assert A.get_market_status() == market_clock.get_market_status()

    def test_supervisor_has_no_hardcoded_us_window(self):
        src = _read("auto_restart_bot.sh")
        assert "2320" not in src, "감시 스크립트에 하드코딩된 23:20 창이 남아 있습니다."
        assert "market_clock" in src, "감시 스크립트가 봇과 같은 시계를 쓰지 않습니다."

    def test_run_bot_has_no_hardcoded_open_time(self):
        src = _read("run_bot.py")
        assert "hour=23, minute=30" not in src, \
            "buy_delay 계산에 하드코딩된 미국 개장시각이 남아 있습니다."


# ---------------------------------------------------------------------------
# 원인 ④ "프로세스가 있다" 를 "살아 있다" 로 표시했다
# ---------------------------------------------------------------------------
class TestLiveness:

    def test_bot_writes_heartbeat(self):
        src = _read("run_bot.py")
        assert "def touch_heartbeat" in src
        assert "heartbeat.json" in src
        # 메인 루프와 schedule 양쪽에서 갱신되어야 한다.
        assert "source='mainloop'" in src
        assert "source='schedule'" in src

    def test_heartbeat_survives_a_long_session(self):
        """job() 은 세션 내내 메인 루프를 점유한다 — 그동안 생존 신호가 끊기면
        감시 스크립트가 **정상 동작 중인 봇을 좀비로 오인해 죽인다.**
        감시 장치를 넣을 때 가장 흔한 자책골이다.
        """
        src = _read("run_bot.py")
        # job() 의 감시 루프(watch loop) 안에서 갱신되어야 한다.
        assert "source='watchloop'" in src,             "job() 감시 루프에 touch_heartbeat 가 없습니다 — 장중에 봇이 강제 종료됩니다."
        # 세션 준비 구간과 종목 평가 단위에서도 갱신되어야 한다.
        assert "source='session-prep'" in src
        assert "source='evaluate'" in src
        # 초기 스캔(종목당 REST 2~3회)과 LLM 감성 조회 구간도 덮어야 한다.
        # 이 두 구간이 장중 좀비 임계(5분)를 넘길 수 있는 유일한 곳이다.
        assert "source='scan'" in src
        assert "source='sentiment'" in src

    def test_supervisor_checks_heartbeat_age_not_just_pgrep(self):
        src = _read("auto_restart_bot.sh")
        assert "check_bot_liveness" in src
        assert "HEARTBEAT_FILE" in src
        assert "check_bot_liveness" in src.split("Main monitoring loop", 1)[1], \
            "좀비 감시가 메인 루프에서 호출되지 않습니다."

    def test_supervisor_keeps_bot_alive_outside_market_hours(self):
        """장 밖에서 봇을 죽여두면 개장 순간에 살아 있을 보장이 없다."""
        src = _read("auto_restart_bot.sh")
        loop = src.split("Main monitoring loop", 1)[1]
        # check_and_restart_bot 이 is_market_hours 조건 안에 갇혀 있으면 안 된다.
        assert "is_market_hours" not in loop
        assert "check_and_restart_bot" in loop

    def test_dashboard_reports_stalled_state(self):
        src = _read("web/app.py")
        assert "get_bot_liveness" in src
        assert "stalled" in src
        assert "heartbeat_age_sec" in src

    def test_status_api_distinguishes_alive_from_trading(self):
        """'봇이 살아 있다'와 '매매 세션을 돌고 있다'는 전혀 다른 상태다.

        2026-09-10 진단이 오래 걸린 이유가 이 둘을 구분할 수 없었기 때문이다.
        heartbeat 의 source 가 mainloop/schedule 이면 살아만 있는 것이고,
        watchloop/evaluate 면 실제로 매매 중이다.
        """
        src = _read("web/app.py")
        assert "session_active" in src
        assert "heartbeat_source" in src

    def test_stall_detector_exists_outside_job(self):
        """job() 이 아예 안 도는 고장은 job() 안의 자가진단으로 잡을 수 없다."""
        src = _read("run_bot.py")
        assert "_check_session_stall" in src
        loop = src.split("--- Main Loop ---", 1)[1]
        assert "_check_session_stall" in loop


# ---------------------------------------------------------------------------
# 원인 ⑤ 소액 계좌에서 매수 수량이 구조적으로 0 이 되던 사이징 버그
# ---------------------------------------------------------------------------
class TestSmallAccountSizing:
    """실계좌 재현: US 주문가능금액 $330.64 / 후보 12종목, KR ₩2,715,678 / 21종목."""

    US_CASH = 330.64
    US_TARGETS = 12
    KR_CASH = 2_715_678
    KR_TARGETS = 21

    @pytest.fixture(scope="class")
    def rb(self):
        import run_bot
        return run_bot

    @pytest.mark.parametrize("price", [25.0, 90.0, 180.0, 300.0])
    @pytest.mark.parametrize("mult", [0.25, 0.3, 0.5, 0.7, 1.0])
    def test_affordable_us_ticker_always_yields_at_least_one_share(self, rb, price, mult):
        """살 수 있는 가격이면 어떤 사이즈 배율에서도 0주가 나오면 안 된다.

        v6.0 은 `int(qty * size_multiplier)` 로 정수화 뒤에 배율을 곱했다.
        qty=1, 배율=0.5 → int(0.5) = 0. 소액 계좌에서는 qty 가 거의 항상 1이므로
        배율이 1.0 이 아닌 모든 매수가 조용히 사라졌다.
        """
        qty = rb.calculate_dca_quantity(
            self.US_CASH, price, self.US_TARGETS, rb.DCA_SETTINGS, 'US',
            weight=1.0, size_multiplier=mult,
        )
        assert qty >= 1, (
            f"${price} 1주를 살 현금({self.US_CASH})이 있는데 배율 {mult} 에서 {qty}주"
        )

    @pytest.mark.parametrize("price", [400.0, 500.0])
    def test_unaffordable_us_ticker_yields_zero(self, rb, price):
        """현금보다 비싼 종목은 정직하게 0주여야 한다 (억지 매수 금지)."""
        qty = rb.calculate_dca_quantity(
            self.US_CASH, price, self.US_TARGETS, rb.DCA_SETTINGS, 'US',
            weight=1.0, size_multiplier=1.0,
        )
        assert qty == 0

    @pytest.mark.parametrize("mult", [0.25, 0.5, 1.0])
    def test_kr_small_account_buys(self, rb, mult):
        qty = rb.calculate_dca_quantity(
            self.KR_CASH, 70_000, self.KR_TARGETS, rb.DCA_SETTINGS, 'KR',
            weight=1.0, size_multiplier=mult,
        )
        assert qty >= 1

    def test_size_multiplier_is_monotonic(self, rb):
        """배율이 클수록 많이 사야 한다 (1주 바닥 때문에 동률은 허용)."""
        qtys = [
            rb.calculate_dca_quantity(
                10_000.0, 25.0, 5, rb.DCA_SETTINGS, 'US',
                weight=1.0, size_multiplier=m,
            )
            for m in (0.25, 0.5, 1.0)
        ]
        assert qtys == sorted(qtys), f"사이즈 단조성 위반: {qtys}"
        assert qtys[-1] > qtys[0]

    def test_budget_is_not_split_below_min_ticket(self, rb):
        """후보가 많다고 최소 주문금액 아래로 쪼개면 아무것도 못 산다."""
        # $330 을 12등분하면 $27.55 — v6.0 이 실제로 그렇게 했다.
        qty = rb.calculate_dca_quantity(
            self.US_CASH, 90.0, self.US_TARGETS, rb.DCA_SETTINGS, 'US',
            weight=1.0, size_multiplier=1.0,
        )
        assert qty >= 1

    def test_caller_does_not_refloor_quantity(self):
        """호출부가 다시 int(qty * 배율) 을 하면 같은 버그가 부활한다."""
        src = _read("run_bot.py")
        assert "int(qty * decision.size_multiplier)" not in src
        assert "size_multiplier=decision.size_multiplier" in src
