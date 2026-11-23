from datetime import datetime, timezone
from pathlib import Path

from modules import market_clock
from modules.brokers.models import Market
from modules.brokers.toss_stream import MarketEvent
from modules.extended_watch import ExtendedWatch, NewsPoller, is_watch_only, parse_feed
from modules.market_data_store import MarketDataStore


CALENDAR = {
    "previousBusinessDay": None,
    "today": {
        "dayMarket": {"startTime": "2026-03-25T09:00:00+09:00", "endTime": "2026-03-25T16:50:00+09:00"},
        "preMarket": {"startTime": "2026-03-25T17:00:00+09:00", "endTime": "2026-03-25T22:30:00+09:00"},
        "regularMarket": {"startTime": "2026-03-25T22:30:00+09:00", "endTime": "2026-03-26T05:00:00+09:00"},
        "afterMarket": {"startTime": "2026-03-26T05:00:00+09:00", "endTime": "2026-03-26T07:00:00+09:00"},
    },
    "nextBusinessDay": None,
}


def at(text):
    return datetime.fromisoformat(f"2026-03-{text}+09:00").timestamp()


def trade(symbol, price, when, volume=10):
    return MarketEvent("trade", f"trade:us:{symbol}", symbol, Market.US, None,
                       {"price": str(price), "volume": str(volume), "currency": "USD"}, when)


def book(symbol, bid, ask, when):
    return MarketEvent("orderbook", f"orderbook:us:{symbol}", symbol, Market.US, None,
                       {"currency": "USD", "bids": [{"price": str(bid), "volume": "7"}],
                        "asks": [{"price": str(ask), "volume": "9"}]}, when)


def make(tmp_path):
    store = MarketDataStore(tmp_path / "ext.sqlite")
    sent = []
    return store, ExtendedWatch(store, lambda: CALENDAR, sent.append), sent


def test_phase_comes_from_calendar_and_unknown_is_not_closed():
    phase = lambda text: market_clock.us_session_phase(
        CALENDAR, datetime.fromtimestamp(at(text), timezone.utc))
    assert phase("25T10:00:00") == "day"
    assert phase("25T16:55:00") == "closed"
    assert phase("25T18:00:00") == "pre"
    assert phase("25T23:00:00") == "regular"
    assert phase("26T06:00:00") == "after"
    assert phase("27T06:00:00") == "unknown"
    assert market_clock.us_session_phase({}) == "unknown"


def test_extended_minute_stores_price_and_spread(tmp_path):
    store, watch, _ = make(tmp_path)
    watch.on_event(trade("IWM", 100, at("25T23:59:00")))
    watch.tick(at("25T23:59:10"))
    watch.on_event(book("IWM", 100.9, 101.1, at("26T05:10:01")))
    watch.on_event(trade("IWM", 101, at("26T05:10:02")))
    watch.on_event(trade("IWM", 102, at("26T05:10:30"), volume=5))
    watch.tick(at("26T05:11:00"))
    row = store._conn.execute(
        "SELECT phase, open, high, close, volume, trades, ref_price, bid_volume, spread_bps_avg "
        "FROM extended_minutes").fetchone()
    assert row[:8] == ("after", 101.0, 102.0, 102.0, 15.0, 2, 100.0, 7.0)
    assert round(row[8], 1) == 19.8
    store.close()


def test_one_bad_print_does_not_alert_two_minutes_do(tmp_path):
    store, watch, sent = make(tmp_path)
    watch.on_event(trade("IWM", 100, at("25T23:59:00")))
    watch.on_event(trade("IWM", 105, at("26T05:10:00")))
    watch.on_event(trade("IWM", 100.5, at("26T05:11:00")))
    watch.on_event(trade("IWM", 104, at("26T05:12:00")))
    watch.tick(at("26T05:13:00"))
    assert sent == []
    watch.on_event(trade("IWM", 104, at("26T05:13:00")))
    watch.tick(at("26T05:14:00"))
    assert len(sent) == 1 and "IWM +4.0%" in sent[0] and "주문하지 않습니다" in sent[0]
    watch.on_event(trade("IWM", 104.5, at("26T05:14:00")))
    watch.tick(at("26T05:15:00"))
    assert len(sent) == 1
    store.close()


def test_leveraged_watch_uses_wider_threshold(tmp_path):
    store, watch, sent = make(tmp_path)
    watch.on_event(trade("SOXL", 100, at("25T23:59:00")))
    for minute in (10, 11, 12):
        watch.on_event(trade("SOXL", 105, at(f"26T05:{minute}:00")))
    watch.tick(at("26T05:13:00"))
    assert sent == []
    store.close()


def test_reference_survives_restart(tmp_path):
    store, watch, _ = make(tmp_path)
    watch.on_event(trade("XLK", 150, at("25T23:59:00")))
    watch.tick(at("25T23:59:10"))
    assert ExtendedWatch(store, lambda: CALENDAR, lambda _m: None)._refs["XLK"] == 150
    store.close()


def test_watch_only_is_separated_from_universe():
    assert is_watch_only(trade("SOXL", 40, 0))
    assert is_watch_only(trade("SOXS", 40, 0))
    assert not is_watch_only(trade("SPY", 700, 0))


def test_watch_module_has_no_order_path():
    source = Path("modules/extended_watch.py").read_text(encoding="utf-8")
    for forbidden in ("submit_order", "submit_and_wait", "OrderRequest", "/api/v1/orders"):
        assert forbidden not in source


class Response:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


def test_news_baseline_is_separated_from_new_arrivals(tmp_path):
    store, watch, _ = make(tmp_path)
    feed = [b"<rss><channel><item><title>Old &lt;story</title><link>http://a</link></item></channel></rss>"]
    poller = NewsPoller(store, watch, fetch=lambda _url, timeout: Response(feed[0]))
    poller.poll_once("pre", at=1000)
    assert poller.new_items == 0 and store.recent_news(0) == []
    feed[0] = feed[0].replace(b"</channel>", b"<item><title>New</title><link>http://b</link></item></channel>")
    poller.poll_once("pre", at=2000)
    assert poller.new_items == 3 and store.recent_news(1500) == ["New"]
    store.close()


def test_news_failure_is_visible_and_not_fatal(tmp_path):
    store, watch, _ = make(tmp_path)
    def fail(_url, timeout):
        raise ConnectionError("down")
    poller = NewsPoller(store, watch, fetch=fail)
    poller.poll_once("pre", at=1000)
    assert len(poller.status()["news_failed_feeds"]) == 3
    store.close()


def test_parse_feed_skips_empty_titles():
    content = b"<rss><channel><item><title></title></item><item><title>A</title></item></channel></rss>"
    assert [item["title"] for item in parse_feed("x", content)] == ["A"]
