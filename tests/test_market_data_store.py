import time

from modules.brokers.models import Bar, Market
from modules.brokers.toss_stream import MarketEvent
from modules.market_data_store import MarketDataStore


def trade(price, volume, second):
    return MarketEvent(
        kind="trade", topic="trade:us:XLK", symbol="XLK", market=Market.US,
        timestamp=f"2026-10-28T22:31:{second:02d}+09:00",
        data={"price": str(price), "volume": str(volume),
              "timestamp": f"2026-10-28T22:31:{second:02d}+09:00", "currency": "USD"},
        received_at=time.time(),
    )


def test_trades_are_aggregated_into_one_minute_bar(tmp_path):
    store = MarketDataStore(tmp_path / "ticks.sqlite")
    store.upsert_trade(trade(200, 2, 1))
    store.upsert_trade(trade(201, 3, 20))
    store.upsert_trade(trade(199, 1, 50))
    row = store._conn.execute(
        "SELECT open, high, low, close, volume FROM minute_bars"
    ).fetchone()
    assert row == (200.0, 201.0, 199.0, 199.0, 6.0)
    bars = store.minute_bars("US", "XLK")
    assert len(bars) == 1 and bars[0].close == 199 and bars[0].volume == 6
    store.close()


def test_daily_bars_are_upserted_for_backtest(tmp_path):
    store = MarketDataStore(tmp_path / "daily.sqlite")
    original = Bar("XLK", Market.US, "USD", "2026-10-28T13:00:00+09:00",
                   200, 202, 199, 201, 1000, "1d")
    corrected = Bar("XLK", Market.US, "USD", "2026-10-28T13:00:00+09:00",
                    200, 203, 198, 202, 1200, "1d")
    store.upsert_daily_bars([original, corrected])
    bars = store.daily_bars("US", "XLK")
    assert len(bars) == 1
    assert (bars[0].high, bars[0].low, bars[0].close, bars[0].volume) == (203, 198, 202, 1200)
    store.close()


def test_shadow_sessions_are_counted(tmp_path):
    store = MarketDataStore(tmp_path / "signals.sqlite")
    store.record_signal("US:2026-10-28", "US", "XLK", "buy", .8, .5, [], {}, 200)
    store.record_signal("US:2026-10-28", "US", "IWM", "defer", .5, 0, [], {}, 280)
    store.record_signal("US:2026-10-29", "US", "XLK", "buy", .9, .8, [], {}, 201)
    assert store.collected_sessions("US") == 2
    assert store.collected_sessions("KR") == 0
    store.close()
