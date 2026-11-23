"""[v8.6] market_data.sqlite 정리. 원본 삭제 기능은 일부러 없다.

원본 틱(market_events)이 32GB 까지 쌓였다. 여유 공간이 3GB 라 VACUUM 은 못 쓴다.
  inspect   읽기 전용 점검
  backfill  원본의 미국 장외 틱을 1분 집계로 별도 파일에 요약 (원본은 읽기만)
  compact   유지 테이블만 새 DB 로 복사 → 검증 → 교체. 원본은 .old 로 남긴다
  rollback  .old 로 되돌린다
compact·rollback 은 수집기가 멈춘 상태에서만 돈다. 출력은 공개 Actions 로그에 남으므로
행 수·크기·날짜만 찍는다 — 가격·잔량·계좌 값을 출력하지 마라.
"""

import argparse
import hashlib
import heapq
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import market_clock  # noqa: E402

KEEP_TABLES = ("minute_bars", "daily_bars", "shadow_signals",
               "extended_minutes", "extended_refs", "extended_news")
US_SYMBOLS = ("SPY", "QQQ", "SMH", "IWM", "XLK")
KST = timezone(timedelta(hours=9))
ROW_OVERHEAD_BYTES = 30
INDEX_FACTOR = 1.5                    # 기본키·인덱스 몫을 넉넉히 잡는다
FREE_MARGIN_BYTES = 1024 ** 3
SAMPLE_CAP = 20000
BATCH_ROWS = 5000
BATCH_PAUSE_SEC = 0.02                # 봇과 같은 서버다. 디스크를 독점하지 않는다
COLLECTOR_PATTERN = "[p]ython.*modules[.]shadow_collector"


def connect_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _tables(conn: sqlite3.Connection) -> List[str]:
    return [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]


def _columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> List[str]:
    return [row[1] for row in conn.execute(f'PRAGMA {schema}.table_info("{table}")')]


def table_stats(conn: sqlite3.Connection, table: str) -> Dict[str, int]:
    lengths = " + ".join(f'COALESCE(LENGTH(CAST("{c}" AS BLOB)), 0)' for c in _columns(conn, table))
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
    """평일 KST 19:00·07:00 30분 표본. 존재 확인용이다 — 세션 판정은 backfill_phase 가 한다."""
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


# --- backfill -----------------------------------------------------------------------------

_PHASE_CACHE: Dict[int, str] = {}


def _plain_us_day(day: date) -> bool:
    key = day.isoformat()
    return day.weekday() <= 4 and key not in market_clock.US_HOLIDAYS \
        and key not in market_clock.US_EARLY_CLOSE_DAYS


def _classify_minute(now: datetime) -> str:
    if market_clock.is_market_open_for("US", now):
        return "regular"
    ny = now.astimezone(market_clock.NY)
    day, clock = ny.date(), ny.time()
    if _plain_us_day(day):
        if datetime.strptime("04:05", "%H:%M").time() <= clock < datetime.strptime("09:25", "%H:%M").time():
            return "pre"
        if datetime.strptime("16:05", "%H:%M").time() <= clock < datetime.strptime("17:55", "%H:%M").time():
            return "after"
    target = None
    if clock >= datetime.strptime("20:05", "%H:%M").time():
        target = day + timedelta(days=1)
    elif clock < datetime.strptime("03:45", "%H:%M").time():
        target = day
    if target and _plain_us_day(target):
        before = target - timedelta(days=1)
        if before.isoformat() not in market_clock.US_HOLIDAYS:
            return "day"
    return "excluded"


def backfill_phase(at: float) -> str:
    """과거 시각의 세션. 과거 토스 캘린더가 없으므로 ET 고정 구간의 안쪽만 인정한다.

    세션 경계 5분과 휴장 전후·조기폐장일은 'excluded' — 애매한 시각을 장외로 세지 않는다.
    """
    minute = int(at // 60)
    phase = _PHASE_CACHE.get(minute)
    if phase is None:
        phase = _classify_minute(datetime.fromtimestamp(minute * 60, timezone.utc))
        _PHASE_CACHE[minute] = phase
    return phase


def _iter_raw(conn: sqlite3.Connection, symbol: str, kind: str) -> Iterator[Tuple[float, int, str, str]]:
    # 배치마다 읽기 트랜잭션을 끝낸다. 긴 스냅샷은 수집기의 WAL 체크포인트를 막는다.
    last_at, last_id = -1.0, -1
    while True:
        rows = conn.execute(
            """SELECT received_at, id, payload FROM market_events
               INDEXED BY idx_market_events_lookup
               WHERE market='US' AND symbol=? AND kind=? AND (received_at, id) > (?, ?)
               ORDER BY received_at, id LIMIT ?""",
            (symbol, kind, last_at, last_id, BATCH_ROWS),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            yield row[0], row[1], kind, row[2]
        last_at, last_id = rows[-1][0], rows[-1][1]
        time.sleep(BATCH_PAUSE_SEC)


def _sidecars(path: Path) -> List[Path]:
    return [path, Path(f"{path}-wal"), Path(f"{path}-shm")]


def _remove_sqlite(path: Path) -> None:
    for item in _sidecars(path):
        if item.exists():
            item.unlink()


def backfill(db: Path, output: Optional[Path] = None) -> Dict[str, Any]:
    from modules.brokers.models import Market
    from modules.brokers.toss_stream import MarketEvent
    from modules.extended_watch import EXTENDED_PHASES, ExtendedWatch
    from modules.market_data_store import MarketDataStore

    class ReplayWatch(ExtendedWatch):
        # 운영과 같은 집계 코드를 쓰되 세션 판정만 바꾸고 알림은 내지 않는다.
        def phase(self, at):
            return backfill_phase(at)

        def _check_alert(self, bar):
            return None

    output = output or db.with_name("market_data.backfill.sqlite")
    if output.exists():
        raise FileExistsError(f"이미 있습니다: {output.name}")
    if shutil.disk_usage(str(db.parent)).free < FREE_MARGIN_BYTES:
        raise RuntimeError("디스크 여유 1GB 미만 — backfill 중단")
    tmp = output.with_name(output.name + ".tmp")
    _remove_sqlite(tmp)
    source = connect_read_only(db)
    store = MarketDataStore(tmp)
    processed, malformed, last_at = 0, 0, 0.0
    try:
        watch = ReplayWatch(store, lambda: {}, lambda _message: None)
        for symbol in US_SYMBOLS:
            merged = heapq.merge(_iter_raw(source, symbol, "trade"),
                                 _iter_raw(source, symbol, "orderbook"),
                                 key=lambda row: (row[0], row[1]))
            for at, _row_id, kind, payload in merged:
                processed += 1
                last_at = max(last_at, at)
                if kind == "orderbook" and backfill_phase(at) not in EXTENDED_PHASES:
                    continue
                try:
                    data = json.loads(payload)
                except ValueError:
                    malformed += 1
                    continue
                watch.on_event(MarketEvent(kind, f"{kind}:us:{symbol}", symbol, Market.US,
                                           data.get("timestamp"), data, at))
            watch.tick(last_at + 120)
        phases = dict(store._conn.execute(
            "SELECT phase, COUNT(*) FROM extended_minutes GROUP BY phase").fetchall())
        days = store._conn.execute("SELECT COUNT(DISTINCT substr(minute, 1, 10)) FROM extended_minutes").fetchone()[0]
    except Exception:
        store.close()
        _remove_sqlite(tmp)
        raise
    finally:
        source.close()
    store.close()
    os.replace(str(tmp), str(output))
    return {"output": output.name, "output_bytes": output.stat().st_size, "events_read": processed,
            "malformed": malformed, "minutes_by_phase": phases, "kst_days": days}


# --- compact ------------------------------------------------------------------------------

def _collector_running() -> bool:
    # pgrep 이 없으면 FileNotFoundError — '안 돈다'로 추측하지 않는다.
    result = subprocess.run(["pgrep", "-f", COLLECTOR_PATTERN], stdout=subprocess.DEVNULL)
    return result.returncode == 0


def _require_idle(db: Path) -> None:
    if _collector_running():
        raise RuntimeError("수집기가 돌고 있습니다")
    if (db.parent / ".deploying").exists():
        raise RuntimeError("배포 진행 중입니다")


def _compact_path(db: Path) -> Path:
    return db.with_name("market_data.compact.sqlite")


def _stamp_path(db: Path) -> Path:
    return db.with_name("market_data.compact.verified.json")


def _old_path(db: Path) -> Path:
    return db.with_name(db.name + ".old")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(db: Path, backfill_path: Optional[Path] = None) -> Dict[str, Any]:
    from modules.market_data_store import MarketDataStore

    _require_idle(db)
    backfill_path = backfill_path or db.with_name("market_data.backfill.sqlite")
    if not backfill_path.exists():
        raise FileNotFoundError("backfill 결과가 없습니다 — 원본을 지우면 장외 데이터를 복원할 수 없습니다")
    source = connect_read_only(db)
    try:
        missing = [t for t in KEEP_TABLES if t not in _tables(source)]
        if missing:
            raise RuntimeError(f"유지 테이블 없음: {missing}")
        keep_bytes = sum(table_stats(source, t)["estimated_bytes"] for t in KEEP_TABLES)
    finally:
        source.close()
    needed = keep_bytes * 2 + backfill_path.stat().st_size + FREE_MARGIN_BYTES
    if shutil.disk_usage(str(db.parent)).free < needed:
        raise RuntimeError("복사할 디스크 공간이 부족합니다")
    dest = _compact_path(db)
    _remove_sqlite(dest)
    stamp = _stamp_path(db)
    if stamp.exists():
        stamp.unlink()
    MarketDataStore(dest).close()
    conn = sqlite3.connect(dest.resolve().as_uri(), uri=True, timeout=30)
    try:
        conn.execute("ATTACH DATABASE ? AS src", (f"{db.resolve().as_uri()}?mode=ro",))
        conn.execute("ATTACH DATABASE ? AS bf", (f"{backfill_path.resolve().as_uri()}?mode=ro",))
        with conn:
            for table in KEEP_TABLES:
                cols = ", ".join(f'"{c}"' for c in _columns(conn, table))
                conn.execute(f'INSERT INTO main."{table}" ({cols}) SELECT {cols} FROM src."{table}"')
            cols = ", ".join(f'"{c}"' for c in _columns(conn, "extended_minutes"))
            # 운영 중 저장된 행이 복원 행보다 우선이다.
            conn.execute(f"INSERT OR IGNORE INTO main.extended_minutes ({cols}) "
                         f"SELECT {cols} FROM bf.extended_minutes")
        conn.execute("DETACH DATABASE src")
        conn.execute("DETACH DATABASE bf")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return {"compact_bytes": dest.stat().st_size}


def verify(db: Path, backfill_path: Optional[Path] = None) -> Dict[str, Any]:
    from modules.market_data_store import MarketDataStore

    backfill_path = backfill_path or db.with_name("market_data.backfill.sqlite")
    dest = _compact_path(db)
    stamp = _stamp_path(db)
    if stamp.exists():
        stamp.unlink()
    failures: List[str] = []
    rows: Dict[str, int] = {}
    conn = connect_read_only(dest)
    try:
        conn.execute("ATTACH DATABASE ? AS src", (f"{db.resolve().as_uri()}?mode=ro",))
        conn.execute("ATTACH DATABASE ? AS bf", (f"{backfill_path.resolve().as_uri()}?mode=ro",))
        if conn.execute("PRAGMA main.integrity_check").fetchone()[0] != "ok":
            failures.append("integrity_check")
        for table in KEEP_TABLES:
            cols = ", ".join(f'"{c}"' for c in _columns(conn, table))
            lost = conn.execute(f'SELECT COUNT(*) FROM (SELECT {cols} FROM src."{table}" '
                                f'EXCEPT SELECT {cols} FROM main."{table}")').fetchone()[0]
            if lost:
                failures.append(f"{table}: 원본 행 {lost}건 누락")
            rows[table] = conn.execute(f'SELECT COUNT(*) FROM main."{table}"').fetchone()[0]
            if table != "extended_minutes":
                src_rows = conn.execute(f'SELECT COUNT(*) FROM src."{table}"').fetchone()[0]
                if src_rows != rows[table]:
                    failures.append(f"{table}: 행 수 불일치")
        lost = conn.execute(
            """SELECT COUNT(*) FROM bf.extended_minutes b WHERE NOT EXISTS (
               SELECT 1 FROM main.extended_minutes d WHERE d.symbol=b.symbol AND d.minute=b.minute)"""
        ).fetchone()[0]
        if lost:
            failures.append(f"backfill: {lost}건 누락")
        if conn.execute("SELECT EXISTS(SELECT 1 FROM main.market_events)").fetchone()[0]:
            failures.append("market_events 가 비어 있지 않습니다")
        src_sessions = conn.execute("SELECT COUNT(DISTINCT session_key) FROM src.shadow_signals").fetchone()[0]
    finally:
        conn.close()
    store = MarketDataStore(dest)
    try:
        if store.collected_sessions() != src_sessions:
            failures.append("collected_sessions 불일치")
    finally:
        store.close()
    if failures:
        return {"ok": False, "failures": failures, "rows": rows}
    stamp.write_text(json.dumps({"sha256": _sha256(dest), "bytes": dest.stat().st_size}), encoding="utf-8")
    return {"ok": True, "failures": [], "rows": rows}


def _move_all(moves: List[Tuple[Path, Path]]) -> None:
    done: List[Tuple[Path, Path]] = []
    try:
        for src, target in moves:
            if src.exists():
                os.replace(str(src), str(target))
                done.append((src, target))
    except Exception:
        for src, target in reversed(done):
            os.replace(str(target), str(src))
        raise


def swap(db: Path) -> Dict[str, Any]:
    _require_idle(db)
    dest, stamp, old = _compact_path(db), _stamp_path(db), _old_path(db)
    if not stamp.exists():
        raise RuntimeError("검증 기록이 없습니다")
    if any(item.exists() for item in _sidecars(old)):
        raise FileExistsError("이전 .old 파일이 남아 있습니다")
    if json.loads(stamp.read_text(encoding="utf-8"))["sha256"] != _sha256(dest):
        raise RuntimeError("검증 뒤 새 DB 가 바뀌었습니다")
    moves = list(zip(_sidecars(db), _sidecars(old)))
    _move_all(moves)
    try:
        os.replace(str(dest), str(db))
        conn = connect_read_only(db)
        try:
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("교체 후 quick_check 실패")
        finally:
            conn.close()
    except Exception:
        if db.exists():
            os.replace(str(db), str(dest))
        for src, target in reversed(moves):
            if target.exists():
                os.replace(str(target), str(src))
        raise
    stamp.unlink()
    return {"swapped": True, "db_bytes": db.stat().st_size, "old_kept": old.name}


def compact(db: Path) -> Dict[str, Any]:
    built = build(db)
    checked = verify(db)
    if not checked["ok"]:
        _remove_sqlite(_compact_path(db))
        return {"ok": False, "build": built, "verify": checked, "swapped": False}
    return {"ok": True, "build": built, "verify": checked, "swap": swap(db)}


def rollback(db: Path) -> Dict[str, Any]:
    _require_idle(db)
    old = _old_path(db)
    aside = db.with_name("market_data.compact.rollback.sqlite")
    if not old.exists():
        raise FileNotFoundError("되돌릴 .old 파일이 없습니다")
    if any(item.exists() for item in _sidecars(aside)):
        raise FileExistsError("이전 rollback 파일이 남아 있습니다")
    _move_all(list(zip(_sidecars(db), _sidecars(aside))))
    _move_all(list(zip(_sidecars(old), _sidecars(db))))
    return {"rolled_back": True, "db_bytes": db.stat().st_size, "compact_kept": aside.name}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inspect", "backfill", "compact", "rollback"])
    parser.add_argument("--db", default="database/market_data.sqlite")
    args = parser.parse_args(argv)
    handler = {"inspect": inspect, "backfill": backfill, "compact": compact, "rollback": rollback}
    report = handler[args.command](Path(args.db))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
