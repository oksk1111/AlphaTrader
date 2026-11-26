import sqlite3

from modules.brokers.models import Bar, Market
from modules.market_data_store import MarketDataStore
from scripts.walk_forward_intraday import simulate_market


def seed_store(path, sessions=20, up=True):
    store = MarketDataStore(path)
    daily = []
    for i in range(80):
        close = 80 + i * 0.5
        daily.append(Bar("XLK", Market.US, "USD", f"2026-07-{(i % 28) + 1:02d}",
                         close, close + 1, close - 1, close, 100000, "1d"))
    # 날짜 중복을 피하도록 직접 최근 80일을 생성한다.
    from datetime import date, timedelta
    daily = [Bar("XLK", Market.US, "USD", str(date(2026, 7, 1) + timedelta(days=i)),
                 80 + i * .5, 81 + i * .5, 79 + i * .5, 80 + i * .5, 100000, "1d")
             for i in range(80)]
    store.upsert_daily_bars(daily)
    # 거래일(평일)만. 주말 날짜는 세션이 아니다 (판정 스크립트가 정규장 밖 분봉을 버린다).
    days, d = [], date(2026, 9, 21)
    while len(days) < sessions:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    for day, session_date in enumerate(days):
        session = str(session_date)
        for minute in range(35):
            price = 120 + day * .2 + minute * (.08 if up else -.08)
            store._conn.execute(
                """INSERT INTO minute_bars
                   (market,symbol,minute,currency,open,high,low,close,volume)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                ("US", "XLK", f"{session}T22:{minute:02d}", "USD",
                 price, price + .05, price - .05, price, 1000 + minute * 20),
            )
    store._conn.commit()
    store.close()


def config():
    return {
        "min_buy_score": .62,
        "min_overnight_score": .72,
        "max_spread_pct": .35,
        "min_shadow_sessions": 20,
    }


def test_gate_holds_when_sessions_are_insufficient(tmp_path):
    path = tmp_path / "short.sqlite"
    seed_store(path, sessions=5)
    conn = sqlite3.connect(str(path))
    result = simulate_market(conn, "US", ["XLK"], config())
    conn.close()
    assert not result.passed
    assert any("수집 세션" in reason for reason in result.reasons)


def test_gate_reports_metrics_from_shadow_data(tmp_path):
    path = tmp_path / "enough.sqlite"
    seed_store(path, sessions=20)
    conn = sqlite3.connect(str(path))
    result = simulate_market(conn, "US", ["XLK"], config())
    conn.close()
    assert result.sessions == 20
    assert result.trades >= 0
    assert isinstance(result.profit_factor, float)
    assert result.turnover == result.trades * 2


# --- 2026-11-26 사전 등록 판정용 회귀 ---------------------------------------------------------
from datetime import date, datetime, timedelta  # noqa: E402

from scripts.walk_forward_intraday import infer_source_tz  # noqa: E402


def seed_us_kst(path, sessions=22, daily_fmt="%Y%m%d"):
    """토스 US 체결 timestamp 는 KST(+09:00) — 정규장 하루가 KST 자정을 넘는다 (11월 EST: 23:30~06:00)."""
    store = MarketDataStore(path)
    store.upsert_daily_bars([
        Bar("XLK", Market.US, "USD", (date(2026, 6, 1) + timedelta(days=i)).strftime(daily_fmt),
            80 + i * .3, 81 + i * .3, 79 + i * .3, 80 + i * .3, 100000, "1d") for i in range(150)])
    day, made = date(2026, 11, 2), 0
    while made < sessions:
        if day.weekday() < 5 and day != date(2026, 11, 26):
            start = datetime.combine(day, datetime.min.time()) + timedelta(hours=23, minutes=30)  # KST
            for m in range(390):
                ts = (start + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M")
                price = 120 + m * .01
                store._conn.execute(
                    "INSERT INTO minute_bars VALUES (?,?,?,?,?,?,?,?,?)",
                    ("US", "XLK", ts, "USD", price, price + .02, price - .02, price, 1000))
            made += 1
        day += timedelta(days=1)
    store._conn.commit()
    store.close()


def test_us_session_crossing_kst_midnight_is_one_session(tmp_path):
    path = tmp_path / "kst.sqlite"
    seed_us_kst(path, sessions=22)
    conn = sqlite3.connect(str(path))
    minutes = [r[0] for r in conn.execute("SELECT minute FROM minute_bars")]
    tz, share = infer_source_tz(minutes, "US")
    result = simulate_market(conn, "US", ["XLK"], config())
    conn.close()
    assert tz == "Asia/Seoul" and share > 99
    assert result.sessions == 22, "KST 자정에서 세션이 두 개로 쪼개지면 안 된다"
    assert result.complete_sessions == 22
    assert result.baseline_trades == 22, "yyyymmdd 일봉도 세션 이전 일봉으로 읽혀야 한다"
    assert result.verdict in ("PASS", "REJECT")


def test_too_few_complete_sessions_is_invalid_not_reject(tmp_path):
    path = tmp_path / "few.sqlite"
    seed_us_kst(path, sessions=8, daily_fmt="%Y-%m-%d")
    conn = sqlite3.connect(str(path))
    result = simulate_market(conn, "US", ["XLK"], config())
    conn.close()
    assert result.verdict == "INVALID"
    assert result.baseline_trades == 8
