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
    base = date(2026, 9, 19)
    for day in range(sessions):
        session = str(base + timedelta(days=day))
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
