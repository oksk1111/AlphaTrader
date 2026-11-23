"""[v8.6] market_data.sqlite 정리 1단계: 읽기 전용 점검.

원본 틱(market_events)이 32GB 까지 쌓였다. 정리 전에 유지할 테이블의 크기와 과거 장외 데이터가
실제로 있는지 본다. DB 를 쓰기로 열지 않는다. 출력은 공개 Actions 로그에 남으므로
행 수·크기·날짜만 찍는다 — 가격·잔량·계좌 값을 출력하지 마라.
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

KEEP_TABLES = ("minute_bars", "daily_bars", "shadow_signals",
               "extended_minutes", "extended_refs", "extended_news")
US_SYMBOLS = ("SPY", "QQQ", "SMH", "IWM", "XLK")
KST = timezone(timedelta(hours=9))
ROW_OVERHEAD_BYTES = 30
INDEX_FACTOR = 1.5                    # 기본키·인덱스 몫을 넉넉히 잡는다
FREE_MARGIN_BYTES = 1024 ** 3
SAMPLE_CAP = 20000


def connect_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _tables(conn: sqlite3.Connection) -> List[str]:
    return [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]


def table_stats(conn: sqlite3.Connection, table: str) -> Dict[str, int]:
    columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
    lengths = " + ".join(f'COALESCE(LENGTH(CAST("{c}" AS BLOB)), 0)' for c in columns)
    rows, payload = conn.execute(f'SELECT COUNT(*), COALESCE(SUM({lengths}), 0) FROM "{table}"').fetchone()
    return {"rows": int(rows), "estimated_bytes": int((payload + rows * ROW_OVERHEAD_BYTES) * INDEX_FACTOR)}


def _count(conn: sqlite3.Connection, symbol: str, kind: str, start: float, end: float) -> int:
    return conn.execute(
        """SELECT COUNT(*) FROM (SELECT 1 FROM market_events
           WHERE market='US' AND symbol=? AND kind=? AND received_at>=? AND received_at<? LIMIT ?)""",
        (symbol, kind, start, end, SAMPLE_CAP),
    ).fetchone()[0]


def _window(day: datetime, hour: int, minutes: int = 30):
    start = day.replace(hour=hour, minute=0, second=0, microsecond=0)
    return start.timestamp(), (start + timedelta(minutes=minutes)).timestamp()


def extended_coverage(conn: sqlite3.Connection, first: float, last: float) -> Dict[str, Any]:
    """평일 KST 19:00(프리마켓)·07:00(애프터마켓) 30분 표본. 서머타임과 무관하게 장외인 시각이다."""
    day = datetime.fromtimestamp(first, KST).replace(hour=0, minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp(last, KST)
    weekdays = pre_days = after_days = 0
    totals = {"pre_trade": 0, "pre_orderbook": 0, "after_trade": 0, "after_orderbook": 0}
    while day <= end:
        if day.weekday() <= 4:
            weekdays += 1
            pre, after = 0, 0
            for symbol in US_SYMBOLS:
                for kind in ("trade", "orderbook"):
                    n_pre = _count(conn, symbol, kind, *_window(day, 19))
                    next_day = day + timedelta(days=1)
                    n_after = _count(conn, symbol, kind, *_window(next_day, 7))
                    totals[f"pre_{kind}"] += n_pre
                    totals[f"after_{kind}"] += n_after
                    pre += n_pre
                    after += n_after
            pre_days += int(pre > 0)
            after_days += int(after > 0)
        day += timedelta(days=1)
    return {"weekdays": weekdays, "days_with_pre": pre_days, "days_with_after": after_days,
            "sample_events": totals}


def orderbook_shape(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        """SELECT payload FROM market_events
           WHERE market='US' AND symbol='SPY' AND kind='orderbook'
           ORDER BY received_at DESC LIMIT 100"""
    ).fetchall()
    usable = 0
    for row in rows:
        try:
            data = json.loads(row[0])
            float(data["bids"][0]["price"]), float(data["asks"][0]["price"])
            usable += 1
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return {"sampled": len(rows), "usable": usable}


def inspect(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    conn = connect_read_only(path)
    try:
        existing = _tables(conn)
        keep = {t: table_stats(conn, t) for t in KEEP_TABLES if t in existing}
        keep_bytes = sum(item["estimated_bytes"] for item in keep.values())
        free = shutil.disk_usage(str(path.parent)).free
        report: Dict[str, Any] = {
            "db_bytes": path.stat().st_size,
            "wal_bytes": os.path.getsize(f"{path}-wal") if os.path.exists(f"{path}-wal") else 0,
            "disk_free_bytes": free,
            "keep_tables": keep,
            "missing_keep_tables": [t for t in KEEP_TABLES if t not in existing],
            "keep_estimated_bytes": keep_bytes,
            "required_free_bytes": keep_bytes * 2 + FREE_MARGIN_BYTES,
            "enough_space_for_copy": free >= keep_bytes * 2 + FREE_MARGIN_BYTES,
        }
        if "market_events" in existing:
            ends = conn.execute("SELECT MIN(id), MAX(id) FROM market_events").fetchone()
            if ends[0] is not None:
                first = conn.execute("SELECT received_at FROM market_events WHERE id=?", (ends[0],)).fetchone()[0]
                last = conn.execute("SELECT received_at FROM market_events WHERE id=?", (ends[1],)).fetchone()[0]
                report["raw_events"] = {
                    "id_span": int(ends[1] - ends[0] + 1),
                    "first_kst": datetime.fromtimestamp(first, KST).strftime("%Y-%m-%d %H:%M"),
                    "last_kst": datetime.fromtimestamp(last, KST).strftime("%Y-%m-%d %H:%M"),
                    "extended_coverage": extended_coverage(conn, first, last),
                    "orderbook_shape": orderbook_shape(conn),
                }
        return report
    finally:
        conn.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inspect"])
    parser.add_argument("--db", default="database/market_data.sqlite")
    args = parser.parse_args(argv)
    print(json.dumps(inspect(Path(args.db)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
