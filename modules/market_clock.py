"""
market_clock.py — v6.0 DST 정확 시장 시계

[왜 이 모듈이 생겼는가]
v5.1 까지 `run_bot.get_market_status()` 는 미국장을 KST 23:30~06:00 으로 **하드코딩**
하고 있었다. 이는 미국 표준시(EST) 기준 매핑이며, 미국이 서머타임(EDT, 3월 둘째
일요일 ~ 11월 첫째 일요일 — 즉 1년의 약 2/3)일 때 실제 정규장은 KST 22:30~05:00 이다.

결과:
  1) 매일 미국장 첫 1시간(22:30~23:30 KST, 유동성이 가장 높은 구간)을 통째로 무시.
  2) 05:00~06:00 KST 에는 이미 폐장했는데 봇은 "개장 중"이라 믿고 손절/트레일링
     시장가 매도를 **닫힌 시장에 제출**해 거부당했다 (`is_market_open_for` 가 True).
  3) 감시 루프 종료 조건이 `get_market_status() != market` 이라, 폐장 후 1시간 동안
     stale 가격으로 계속 돌았다.

v6.0 은 하드코딩을 버리고 `zoneinfo` 의 실제 타임존 DB로 거래소 현지시각을 계산한다.
휴장일(정규 휴일 + 조기폐장)도 반영한다.
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 폴백
    from backports.zoneinfo import ZoneInfo  # type: ignore

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")

# 정규장 (거래소 현지시각)
US_OPEN = _dt.time(9, 30)
US_CLOSE = _dt.time(16, 0)
US_EARLY_CLOSE = _dt.time(13, 0)      # 조기폐장일 13:00 ET
KR_OPEN = _dt.time(9, 0)
KR_CLOSE = _dt.time(15, 20)           # 15:20 이후는 종가 단일가 — 시장가 체결 불가

# ---------------------------------------------------------------------------
# 휴장일. 날짜 문자열(YYYY-MM-DD) 집합. 매년 1회 갱신하면 된다.
# 목록에 없는 연도는 "주말만 휴장" 으로 취급되며, 그 경우에도 주문 거부는
# 브로커가 걸러주므로 치명적이지 않다(단, 로그로 경고).
# ---------------------------------------------------------------------------
US_HOLIDAYS = {
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
US_EARLY_CLOSE_DAYS = {
    "2026-11-27", "2026-12-24",
    "2027-11-26",
}
KR_HOLIDAYS = {
    # 2026
    "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18", "2026-03-01",
    "2026-03-02", "2026-05-05", "2026-05-24", "2026-05-25", "2026-06-06",
    "2026-08-15", "2026-08-17", "2026-09-24", "2026-09-25", "2026-09-26",
    "2026-10-03", "2026-10-05", "2026-10-09", "2026-12-25", "2026-12-31",
    # 2027
    "2027-01-01", "2027-02-06", "2027-02-08", "2027-03-01", "2027-05-05",
    "2027-05-13", "2027-06-06", "2027-08-15", "2027-08-16", "2027-09-14",
    "2027-09-15", "2027-09-16", "2027-10-03", "2027-10-04", "2027-10-09",
    "2027-10-11", "2027-12-25", "2027-12-31",
}
_KNOWN_YEARS = {2026, 2027}


@dataclass(frozen=True)
class SessionInfo:
    """현재 시장 상태 스냅샷."""
    market: str                 # 'US' | 'KR' | 'CLOSED'
    is_open: bool
    local_now: _dt.datetime     # 거래소 현지시각
    open_at: Optional[_dt.datetime]
    close_at: Optional[_dt.datetime]
    minutes_since_open: float
    minutes_to_close: float
    session_date: str           # 거래소 현지 기준 영업일 (YYYY-MM-DD)

    @property
    def is_early_session(self) -> bool:
        """개장 후 30분 — 변동성이 극단적이라 매수 사이징을 줄이는 구간."""
        return self.is_open and self.minutes_since_open < 30

    @property
    def is_closing_soon(self) -> bool:
        """폐장 15분 전 — 신규 진입 금지 구간."""
        return self.is_open and self.minutes_to_close <= 15


def _is_holiday(market: str, d: _dt.date) -> bool:
    key = d.isoformat()
    if market == "US":
        return key in US_HOLIDAYS
    return key in KR_HOLIDAYS


def _warn_unknown_year(market: str, d: _dt.date) -> None:
    if d.year not in _KNOWN_YEARS:
        logger.warning(
            "[MarketClock] %s %d년 휴장일 테이블이 없습니다 — 주말만 휴장으로 간주합니다. "
            "modules/market_clock.py 의 휴장일 목록을 갱신하세요.",
            market, d.year,
        )


def _session_bounds(market: str, local_now: _dt.datetime):
    """해당 거래소 현지 '오늘'의 정규장 시작/종료 시각(현지 tz-aware)."""
    d = local_now.date()
    if market == "US":
        close_t = US_EARLY_CLOSE if d.isoformat() in US_EARLY_CLOSE_DAYS else US_CLOSE
        tz = NY
        open_t = US_OPEN
    else:
        close_t = KR_CLOSE
        tz = KST
        open_t = KR_OPEN
    return (
        _dt.datetime.combine(d, open_t, tzinfo=tz),
        _dt.datetime.combine(d, close_t, tzinfo=tz),
    )


def session_info(market: str, now_utc: Optional[_dt.datetime] = None) -> SessionInfo:
    """지정 시장의 현재 세션 정보를 반환한다.

    now_utc 를 주면 그 시각 기준으로 계산한다(테스트/백테스트용).
    """
    market = market.upper()
    tz = NY if market == "US" else KST
    now = (now_utc or _dt.datetime.now(_dt.timezone.utc)).astimezone(tz)

    _warn_unknown_year(market, now.date())
    open_at, close_at = _session_bounds(market, now)

    weekday_ok = now.weekday() <= 4
    holiday = _is_holiday(market, now.date())
    is_open = weekday_ok and not holiday and open_at <= now < close_at

    return SessionInfo(
        market=market if is_open else "CLOSED",
        is_open=is_open,
        local_now=now,
        open_at=open_at,
        close_at=close_at,
        minutes_since_open=(now - open_at).total_seconds() / 60.0,
        minutes_to_close=(close_at - now).total_seconds() / 60.0,
        session_date=now.date().isoformat(),
    )


def get_market_status(now_utc: Optional[_dt.datetime] = None) -> str:
    """현재 열려 있는 시장을 반환. 'US' | 'KR' | 'CLOSED'.

    두 시장은 시간대가 겹치지 않으므로 단일 값으로 안전하다
    (KR 09:00~15:20 KST = 미국 현지 전날 19:00~01:20 → 미국장 폐장 이후).
    """
    for m in ("KR", "US"):
        if session_info(m, now_utc).is_open:
            return m
    return "CLOSED"


def is_market_open_for(market: str, now_utc: Optional[_dt.datetime] = None) -> bool:
    """해당 시장에 **시장가 주문이 실제로 체결될 수 있는지**.

    v5.1 까지는 하드코딩된 시간표를 믿고 폐장된 미국장(05:00~06:00 KST, DST 기간)에
    손절 매도를 계속 던졌다. 이제 실제 거래소 현지시각으로 판정한다.
    """
    try:
        return session_info(market, now_utc).is_open
    except Exception as e:  # 시간 계산 실패 시 안전하게 '닫힘'
        logger.error("[MarketClock] session_info 실패 (%s) — 닫힘으로 간주: %s", market, e)
        return False


def session_key(market: str, now_utc: Optional[_dt.datetime] = None) -> str:
    """세션 식별자. 거래소 현지 영업일 기준이므로 KST 자정을 넘겨도 안 바뀐다.

    v5.1 의 `us_triggered_today` 는 KST 날짜를 썼기 때문에 KST 자정(=미국장 한복판)에
    플래그가 리셋되는 구조적 위험이 있었다. 세션 키는 거래소 현지 날짜를 쓴다.
    """
    info = session_info(market, now_utc)
    return f"{market.upper()}:{info.session_date}"
