import hashlib
import json
import sqlite3
from datetime import datetime, timedelta

from modules.brokers.models import Market
from modules.brokers.toss_stream import MarketEvent
from modules.market_data_store import MarketDataStore
from scripts.compact_market_db import KST, connect_read_only, inspect


def event(kind, when, price="700.12"):
    data = {"currency": "USD", "price": price, "volume": "1"}
    if kind == "orderbook":
        data = {"currency": "USD", "bids": [{"price": price, "volume": "1"}],
                "asks": [{"price": "700.50", "volume": "1"}]}
    return MarketEvent(kind, f"{kind}:us:SPY", "SPY", Market.US, None, data, when.timestamp())


def build(tmp_path):
    path = tmp_path / "market.sqlite"
    store = MarketDataStore(path)
    monday = datetime(2026, 11, 16, 19, 5, tzinfo=KST)
    store.record_event(event("trade", monday))
    store.record_event(event("orderbook", monday + timedelta(minutes=1)))
    store.record_event(event("orderbook", monday + timedelta(hours=12)))
    store.record_event(event("trade", monday + timedelta(days=1, hours=4)))
    store.record_signal("US:2026-11-16", "US", "SPY", "defer", .4, 0, [], {}, 700)
    store.close()
    return path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_inspect_reports_keep_tables_and_extended_days(tmp_path):
    path = build(tmp_path)
    report = inspect(path)
    assert report["keep_tables"]["shadow_signals"]["rows"] == 1
    assert report["missing_keep_tables"] == []
    assert report["raw_events"]["id_span"] == 4
    coverage = report["raw_events"]["extended_coverage"]
    assert coverage["days_with_pre"] >= 1 and coverage["days_with_after"] >= 1
    assert report["raw_events"]["orderbook_shape"] == {"sampled": 2, "usable": 2}


def test_inspect_does_not_modify_database(tmp_path):
    path = build(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    before = digest(path)
    inspect(path)
    assert digest(path) == before


def test_read_only_connection_rejects_writes(tmp_path):
    path = build(tmp_path)
    conn = connect_read_only(path)
    try:
        try:
            conn.execute("DELETE FROM market_events")
            assert False, "write must fail"
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()


def test_report_has_no_prices_for_public_logs(tmp_path):
    path = build(tmp_path)
    text = json.dumps(inspect(path))
    assert "700.12" not in text and "700.50" not in text
