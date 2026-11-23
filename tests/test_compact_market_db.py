import hashlib
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from modules.brokers.models import Market
from modules.brokers.toss_stream import MarketEvent
from modules.market_data_store import MarketDataStore
from scripts import compact_market_db as compact_db
from scripts.compact_market_db import KST, backfill, backfill_phase, connect_read_only, inspect


def event(kind, when, price="700.12"):
    data = {"currency": "USD", "price": price, "volume": "1"}
    if kind == "orderbook":
        data = {"currency": "USD", "bids": [{"price": price, "volume": "1"}],
                "asks": [{"price": "700.50", "volume": "1"}]}
    return MarketEvent(kind, f"{kind}:us:SPY", "SPY", Market.US, None, data, when.timestamp())


def kst(month, day, hour, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=KST)


def build(tmp_path):
    path = tmp_path / "market_data.sqlite"
    store = MarketDataStore(path)
    monday = kst(11, 16, 19, 5)
    store.record_event(event("trade", monday))
    store.record_event(event("orderbook", monday + timedelta(minutes=1)))
    store.record_event(event("trade", kst(11, 17, 0), price="700"))         # 정규장 기준가
    store.record_event(event("orderbook", kst(11, 17, 7, 1), price="700.30"))
    store.record_event(event("trade", kst(11, 17, 7, 1), price="710"))
    store.record_signal("US:2026-11-16", "US", "SPY", "defer", .4, 0, [], {}, 700)
    store.upsert_trade(event("trade", monday))
    store.upsert_extended_minute({
        "symbol": "IWM", "minute": "2026-11-24T06:10", "phase": "after", "open": 1, "high": 1,
        "low": 1, "close": 1, "volume": 1, "trades": 1, "spread_bps_avg": None,
        "spread_bps_max": None, "bid_volume": None, "ask_volume": None, "ref_price": None})
    store.close()
    return path


@pytest.fixture
def idle(monkeypatch):
    monkeypatch.setattr(compact_db, "_collector_running", lambda: False)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checkpoint(path):
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


def test_inspect_reports_keep_tables_and_extended_days(tmp_path):
    path = build(tmp_path)
    report = inspect(path)
    assert report["keep_tables"]["shadow_signals"]["rows"] == 1
    assert report["missing_keep_tables"] == []
    assert report["raw_events"]["id_span"] == 5
    coverage = report["raw_events"]["extended_coverage"]
    assert coverage["days_with_pre"] >= 1 and coverage["days_with_after"] >= 1
    assert report["raw_events"]["orderbook_shape"] == {"sampled": 2, "usable": 2}


def test_inspect_does_not_modify_database(tmp_path):
    path = build(tmp_path)
    checkpoint(path)
    before = digest(path)
    inspect(path)
    assert digest(path) == before


def test_read_only_connection_rejects_writes(tmp_path):
    path = build(tmp_path)
    conn = connect_read_only(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM market_events")
    finally:
        conn.close()


def test_report_has_no_prices_for_public_logs(tmp_path):
    path = build(tmp_path)
    text = json.dumps([inspect(path), backfill(path)])
    assert "700.12" not in text and "700.50" not in text


@pytest.mark.parametrize("when,phase", [
    (kst(11, 16, 19), "pre"),
    (kst(11, 17, 0), "regular"),
    (kst(11, 17, 7), "after"),
    (kst(11, 17, 11), "day"),
    (kst(11, 16, 23, 27), "excluded"),      # 개장 직전 경계
    (kst(11, 21, 11), "excluded"),          # 금요일 밤 ET → 다음 날이 토요일
    (kst(11, 26, 19), "excluded"),          # 추수감사절
    (kst(11, 28, 7), "excluded"),           # 조기폐장일 뒤
    (kst(10, 30, 6), "after"),              # 서머타임: 17:00 ET
    (kst(10, 30, 7, 30), "excluded"),       # 서머타임: 애프터마켓 종료 뒤
])
def test_backfill_phase_uses_only_safe_windows(when, phase):
    assert backfill_phase(when.timestamp()) == phase


def test_backfill_builds_minutes_without_touching_source(tmp_path):
    path = build(tmp_path)
    checkpoint(path)
    before = digest(path)
    report = backfill(path)
    assert digest(path) == before
    assert report["minutes_by_phase"] == {"after": 1, "pre": 1}
    conn = sqlite3.connect(str(tmp_path / "market_data.backfill.sqlite"))
    row = conn.execute(
        "SELECT close, ref_price, spread_bps_avg FROM extended_minutes WHERE phase='after'").fetchone()
    conn.close()
    assert row[0] == 710 and row[1] == 700 and round(row[2], 1) == 2.9


def test_backfill_refuses_to_overwrite(tmp_path):
    path = build(tmp_path)
    backfill(path)
    with pytest.raises(FileExistsError):
        backfill(path)


def test_compact_keeps_tables_drops_raw_and_keeps_old(tmp_path, idle):
    path = build(tmp_path)
    checkpoint(path)
    before = digest(path)
    backfill(path)
    report = compact_db.compact(path)
    assert report["ok"] and report["swap"]["swapped"]
    assert digest(tmp_path / "market_data.sqlite.old") == before
    store = MarketDataStore(path)
    assert store._conn.execute("SELECT COUNT(*) FROM market_events").fetchone()[0] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM shadow_signals").fetchone()[0] == 1
    # 운영 행 1 + 복원 행 2
    assert store._conn.execute("SELECT COUNT(*) FROM extended_minutes").fetchone()[0] == 3
    store.record_signal("US:2026-11-17", "US", "SPY", "defer", .4, 0, [], {}, 700)
    assert store.collected_sessions() == 2
    store.close()


def test_compact_requires_backfill(tmp_path, idle):
    path = build(tmp_path)
    with pytest.raises(FileNotFoundError):
        compact_db.compact(path)
    assert not (tmp_path / "market_data.sqlite.old").exists()


def test_compact_refuses_while_collector_runs(tmp_path, monkeypatch):
    path = build(tmp_path)
    backfill(path)
    monkeypatch.setattr(compact_db, "_collector_running", lambda: True)
    with pytest.raises(RuntimeError):
        compact_db.compact(path)
    assert not (tmp_path / "market_data.compact.sqlite").exists()


def test_compact_refuses_during_deploy(tmp_path, idle):
    path = build(tmp_path)
    backfill(path)
    (tmp_path / ".deploying").touch()
    with pytest.raises(RuntimeError):
        compact_db.compact(path)


def test_failed_verification_does_not_swap(tmp_path, idle, monkeypatch):
    path = build(tmp_path)
    checkpoint(path)
    before = digest(path)
    backfill(path)
    monkeypatch.setattr(compact_db, "verify", lambda _db: {"ok": False, "failures": ["x"], "rows": {}})
    report = compact_db.compact(path)
    assert not report["ok"] and digest(path) == before
    assert not (tmp_path / "market_data.sqlite.old").exists()
    assert not (tmp_path / "market_data.compact.sqlite").exists()


def test_verify_detects_lost_row(tmp_path, idle):
    path = build(tmp_path)
    backfill(path)
    compact_db.build(path)
    conn = sqlite3.connect(str(tmp_path / "market_data.compact.sqlite"))
    with conn:
        conn.execute("DELETE FROM shadow_signals")
    conn.close()
    report = compact_db.verify(path)
    assert not report["ok"] and any("shadow_signals" in item for item in report["failures"])
    assert not (tmp_path / "market_data.compact.verified.json").exists()


def test_swap_refuses_changed_file_after_verify(tmp_path, idle):
    path = build(tmp_path)
    backfill(path)
    compact_db.build(path)
    assert compact_db.verify(path)["ok"]
    with open(tmp_path / "market_data.compact.sqlite", "ab") as file:
        file.write(b"x")
    with pytest.raises(RuntimeError):
        compact_db.swap(path)
    assert not (tmp_path / "market_data.sqlite.old").exists()


def test_rollback_restores_original(tmp_path, idle):
    path = build(tmp_path)
    checkpoint(path)
    before = digest(path)
    backfill(path)
    compact_db.compact(path)
    report = compact_db.rollback(path)
    assert report["rolled_back"] and digest(path) == before
    assert (tmp_path / "market_data.compact.rollback.sqlite").exists()


def test_script_has_no_permanent_delete_of_original():
    source = open("scripts/compact_market_db.py", encoding="utf-8").read()
    assert "DROP TABLE" not in source and "DELETE FROM" not in source
    assert '"delete"' not in source and "_remove_sqlite(old" not in source
