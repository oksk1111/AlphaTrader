"""
Trade Journal - 체결 단위 거래 기록 (Phase 0 측정 인프라)

매 청산(매도 성공) 시점에 한 건의 trade record 를 append-only 로 저장한다.
이후 measurement / walk-forward 모듈이 이 데이터를 집계해 신호별 / 종목별 / 트리거별
성과를 계산한다.

설계 원칙:
 - 외부 API 호출 없이 호출 가능 (즉시 로컬 파일에 기록)
 - 실패해도 매매 흐름을 깨지 않음 (try/except 로 호출자가 무시 가능)
 - 스키마 변경에 대비해 `schema_version` 필드를 포함

스키마 (`database/trade_journal.json` 내 list of dicts):
{
    "schema_version": 1,
    "trade_id": "<ticker>_<epoch_ms>",
    "ticker": "005930",
    "market": "KR",
    "exchange": null,
    "trigger": "stop_loss" | "trailing_stop" | "dca_stop_loss" | "trend_break"
               | "time_cut" | "rule0_retry" | "manual" | "unknown",
    "entry_price": 71500.0,
    "entry_time": "2026-05-20 10:31:02",
    "exit_price": 68210.0,
    "exit_time": "2026-05-23 14:55:11",
    "sold_qty": 10,
    "entry_qty": 10,
    "pnl_per_share": -3290.0,
    "pnl_total": -32900.0,
    "pnl_pct": -4.6,
    "duration_min": 4344,
    "phase": "market" | "limit" | "already_flat",
    "version": "v2.3",
    "extra": { ... }
}
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
JOURNAL_FILE = BASE_DIR / "database" / "trade_journal.json"
SCHEMA_VERSION = 1
DEFAULT_VERSION_TAG = "v2.3"


def _load(path: Path = JOURNAL_FILE):
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(records, path: Path = JOURNAL_FILE) -> None:
    os.makedirs(path.parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _duration_minutes(entry_time: Optional[str], exit_time: str) -> Optional[float]:
    if not entry_time:
        return None
    try:
        et = datetime.strptime(entry_time, "%Y-%m-%d %H:%M:%S")
        xt = datetime.strptime(exit_time, "%Y-%m-%d %H:%M:%S")
        return round((xt - et).total_seconds() / 60.0, 1)
    except Exception:
        return None


def record_close(
    ticker: str,
    market: str,
    trigger: str,
    exit_price: float,
    sold_qty: int,
    monitor_data: Optional[Dict[str, Any]] = None,
    phase: str = "market",
    version: str = DEFAULT_VERSION_TAG,
    extra: Optional[Dict[str, Any]] = None,
    path: Path = JOURNAL_FILE,
) -> Optional[Dict[str, Any]]:
    """청산 기록을 trade_journal 에 append.

    실패 시 None 반환 (호출자는 매매 흐름 중단 금지).
    """
    try:
        if not ticker or sold_qty is None:
            return None

        monitor_data = monitor_data or {}
        entry_price = _safe_float(monitor_data.get("buy_price"))
        entry_qty = int(monitor_data.get("buys") or sold_qty or 0)
        exit_price = _safe_float(exit_price)
        sold_qty = int(sold_qty or 0)

        # entry_time 은 monitor 에 시각 정보가 다양함 → 가장 안정적인 키만 사용
        raw_entry_time = monitor_data.get("entry_time")
        if isinstance(raw_entry_time, datetime):
            entry_time = raw_entry_time.strftime("%Y-%m-%d %H:%M:%S")
        else:
            entry_time = raw_entry_time if isinstance(raw_entry_time, str) else None

        exit_time = _now_str()

        pnl_per_share = exit_price - entry_price if entry_price > 0 else 0.0
        pnl_total = pnl_per_share * sold_qty
        pnl_pct = ((exit_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0

        record = {
            "schema_version": SCHEMA_VERSION,
            "trade_id": f"{ticker}_{int(time.time() * 1000)}",
            "ticker": str(ticker),
            "market": market,
            "exchange": monitor_data.get("exchange"),
            "trigger": trigger or "unknown",
            "entry_price": round(entry_price, 4),
            "entry_time": entry_time,
            "exit_price": round(exit_price, 4),
            "exit_time": exit_time,
            "sold_qty": sold_qty,
            "entry_qty": entry_qty,
            "pnl_per_share": round(pnl_per_share, 4),
            "pnl_total": round(pnl_total, 4),
            "pnl_pct": round(pnl_pct, 4),
            "duration_min": _duration_minutes(entry_time, exit_time),
            "phase": phase,
            "version": version,
            "extra": extra or {},
        }

        records = _load(path)
        records.append(record)
        _save(records, path)
        return record
    except Exception:
        # 절대로 매매 흐름을 막지 않는다.
        return None


def load_journal(path: Path = JOURNAL_FILE):
    return _load(path)
