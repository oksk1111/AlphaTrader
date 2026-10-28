"""Toss shadow 검증용 틱·1분봉·신호 저장소 (SQLite WAL)."""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from modules.brokers.models import Bar, Market
from modules.brokers.toss_stream import MarketEvent


DEFAULT_DB = Path("database/market_data.sqlite")


class MarketDataStore:
    def __init__(self, path: Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS market_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at REAL NOT NULL,
                    kind TEXT NOT NULL,
                    market TEXT,
                    symbol TEXT,
                    event_timestamp TEXT,
                    topic TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_market_events_lookup
                    ON market_events(market, symbol, kind, received_at);

                CREATE TABLE IF NOT EXISTS minute_bars (
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    minute TEXT NOT NULL,
                    currency TEXT,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    PRIMARY KEY (market, symbol, minute)
                );

                CREATE TABLE IF NOT EXISTS shadow_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    session_key TEXT NOT NULL,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    score REAL NOT NULL,
                    size_multiplier REAL NOT NULL,
                    reasons TEXT NOT NULL,
                    components TEXT NOT NULL,
                    price REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_shadow_signals_session
                    ON shadow_signals(session_key, market, symbol);
            """)

    def record_event(self, event: MarketEvent) -> None:
        payload = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO market_events
                   (received_at, kind, market, symbol, event_timestamp, topic, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (event.received_at, event.kind,
                 event.market.value if event.market else None,
                 event.symbol, event.timestamp, event.topic, payload),
            )

    def upsert_trade(self, event: MarketEvent) -> None:
        if event.kind != "trade" or not event.market or not event.symbol:
            return
        price = float(event.data.get("price") or 0)
        volume = float(event.data.get("volume") or 0)
        timestamp = event.data.get("timestamp")
        if price <= 0 or volume < 0 or not timestamp:
            return
        # ISO 8601 입력은 초/밀리초를 제거하여 같은 분의 체결을 한 봉으로 합친다.
        minute = timestamp[:16]
        currency = event.data.get("currency")
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT open, high, low, close, volume FROM minute_bars WHERE market=? AND symbol=? AND minute=?",
                (event.market.value, event.symbol, minute),
            ).fetchone()
            if row:
                self._conn.execute(
                    """UPDATE minute_bars SET high=?, low=?, close=?, volume=?
                       WHERE market=? AND symbol=? AND minute=?""",
                    (max(row[1], price), min(row[2], price), price, row[4] + volume,
                     event.market.value, event.symbol, minute),
                )
            else:
                self._conn.execute(
                    """INSERT INTO minute_bars
                       (market, symbol, minute, currency, open, high, low, close, volume)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (event.market.value, event.symbol, minute, currency,
                     price, price, price, price, volume),
                )

    def record_signal(self, session_key: str, market: str, symbol: str,
                      action: str, score: float, size_multiplier: float,
                      reasons: Iterable[str], components: Dict[str, float], price: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO shadow_signals
                   (created_at, session_key, market, symbol, action, score,
                    size_multiplier, reasons, components, price)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (datetime.now(timezone.utc).timestamp(), session_key, market, symbol,
                 action, score, size_multiplier,
                 json.dumps(list(reasons), ensure_ascii=False),
                 json.dumps(components, ensure_ascii=False), price),
            )

    def minute_bars(self, market: str, symbol: str, limit: int = 240) -> list:
        rows = self._conn.execute(
            """SELECT minute, currency, open, high, low, close, volume
               FROM minute_bars WHERE market=? AND symbol=?
               ORDER BY minute DESC LIMIT ?""",
            (market, symbol, max(1, int(limit))),
        ).fetchall()
        rows.reverse()
        return [Bar(
            symbol=symbol, market=Market(market), timestamp=row[0],
            currency=row[1] or ("KRW" if market == "KR" else "USD"),
            open=row[2], high=row[3], low=row[4], close=row[5], volume=row[6],
            interval="1m",
        ) for row in rows]

    def collected_sessions(self, market: Optional[str] = None) -> int:
        query = "SELECT COUNT(DISTINCT session_key) FROM shadow_signals"
        params = ()
        if market:
            query += " WHERE market=?"
            params = (market,)
        row = self._conn.execute(query, params).fetchone()
        return int(row[0] or 0)

    def close(self) -> None:
        self._conn.close()
