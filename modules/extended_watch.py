"""[v8.6] 미국 장외(데이·프리·애프터) 관찰. 주문 경로가 없다.

수집기 안에서 돈다. 장외 1분 집계(가격·거래량·스프레드·호가 잔량)와 뉴스를 처음 본 시각을
저장하고, 직전 정규장 마지막 체결가 대비 큰 움직임을 텔레그램으로 알린다.
SOXL·SOXS 는 관찰 전용이다 — 장중 신호 엔진과 원본 틱 저장에 넣지 않는다 (v8.0 규칙 12).
"""

import hashlib
import html
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import requests

from modules import market_clock
from modules.brokers.models import Market
from modules.logger import logger


WATCH_ONLY_SYMBOLS = ("SOXL", "SOXS")
EXTENDED_PHASES = ("day", "pre", "after")
PHASE_LABELS = {"day": "데이마켓", "pre": "프리마켓", "after": "애프터마켓"}

MOVE_ALERT_PCT = 3.0
LEVERAGED_MOVE_ALERT_PCT = 9.0        # 3배 ETF 는 기초지수 3% 와 같은 크기
ALERT_CONFIRM_MINUTES = 2             # 체결 한 건짜리 튀는 가격으로 알리지 않는다
ALERT_COOLDOWN_SEC = 30 * 60
BOOK_MAX_AGE_SEC = 60
CALENDAR_TTL_SEC = 60 * 60
CALENDAR_RETRY_SEC = 5 * 60
NEWS_POLL_SEC = 180
NEWS_MAX_BYTES = 2 * 1024 * 1024
NEWS_LOOKBACK_SEC = 30 * 60

NEWS_FEEDS = (
    ("cnbc-finance", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
    ("google-market", "https://news.google.com/rss/search?q=stock+market+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("google-semis", "https://news.google.com/rss/search?q=semiconductor+stocks+when:1d&hl=en-US&gl=US&ceid=US:en"),
)


def is_watch_only(event) -> bool:
    return event.market == Market.US and event.symbol in WATCH_ONLY_SYMBOLS


def _utc(at: float) -> datetime:
    return datetime.fromtimestamp(at, timezone.utc)


def _minute(at: float) -> str:
    return _utc(at).astimezone(market_clock.KST).strftime("%Y-%m-%dT%H:%M")


class ExtendedWatch:
    def __init__(self, store, calendar_provider: Callable[[], Dict[str, Any]],
                 notify: Callable[[str], Any]):
        self.store = store
        self.calendar_provider = calendar_provider
        self.notify = notify
        self._calendar: Dict[str, Any] = {}
        self._calendar_at = 0.0
        self._refs: Dict[str, float] = store.extended_refs()
        self._dirty_refs: Dict[str, float] = {}
        self._books: Dict[str, Dict[str, float]] = {}
        self._bars: Dict[str, Dict[str, Any]] = {}
        self._streaks: Dict[str, List[int]] = {}
        self._alerted: Dict[str, Dict[str, float]] = {}
        self.minutes_saved = 0
        self.alerts_sent = 0
        self.unclassified_events = 0

    def phase(self, at: float) -> str:
        ttl = CALENDAR_TTL_SEC if self._calendar else CALENDAR_RETRY_SEC
        if at - self._calendar_at >= ttl:
            self._calendar_at = at
            try:
                self._calendar = self.calendar_provider() or self._calendar
            except Exception as exc:
                logger.warning("[ExtendedWatch] 미국 장 캘린더 조회 실패: %s", exc)
        return market_clock.us_session_phase(self._calendar, _utc(at))

    def current_phase(self) -> str:
        """캘린더를 새로 조회하지 않는다 (뉴스 스레드용)."""
        return market_clock.us_session_phase(self._calendar)

    def on_event(self, event) -> None:
        if event.market != Market.US or not event.symbol:
            return
        if event.kind == "orderbook":
            self._on_book(event)
        elif event.kind == "trade":
            self._on_trade(event)

    def _on_book(self, event) -> None:
        if self.phase(event.received_at) not in EXTENDED_PHASES:
            return
        try:
            bid = event.data["bids"][0]
            ask = event.data["asks"][0]
            bid_price, ask_price = float(bid["price"]), float(ask["price"])
            bid_volume, ask_volume = float(bid["volume"]), float(ask["volume"])
        except (KeyError, IndexError, TypeError, ValueError):
            return                            # 한쪽 호가가 빈 장외 구간은 흔하다
        if bid_price <= 0 or ask_price < bid_price:
            return
        spread = (ask_price - bid_price) / ((ask_price + bid_price) / 2) * 10000
        self._books[event.symbol] = {"spread": spread, "bid_volume": bid_volume,
                                     "ask_volume": ask_volume, "at": event.received_at}
        bar = self._bars.get(event.symbol)
        if bar:
            self._apply_book(bar, self._books[event.symbol])

    @staticmethod
    def _apply_book(bar: Dict[str, Any], book: Dict[str, float]) -> None:
        bar["_spread_sum"] += book["spread"]
        bar["_spread_count"] += 1
        bar["spread_bps_max"] = max(bar["spread_bps_max"] or 0.0, book["spread"])
        bar["bid_volume"], bar["ask_volume"] = book["bid_volume"], book["ask_volume"]

    def _on_trade(self, event) -> None:
        try:
            price = float(event.data.get("price") or 0)
            volume = float(event.data.get("volume") or 0)
        except (TypeError, ValueError):
            return
        if price <= 0 or volume < 0:
            return
        at, symbol = event.received_at, event.symbol
        phase = self.phase(at)
        if phase == "regular":
            self._close_bar(symbol)
            self._refs[symbol] = price
            self._dirty_refs[symbol] = at
            self._streaks.pop(symbol, None)
            self._alerted.pop(symbol, None)
            return
        if phase not in EXTENDED_PHASES:
            self.unclassified_events += 1
            return
        minute = _minute(at)
        bar = self._bars.get(symbol)
        if bar and (bar["minute"] != minute or bar["phase"] != phase):
            self._close_bar(symbol)
            bar = None
        if not bar:
            bar = {"symbol": symbol, "minute": minute, "phase": phase, "open": price,
                   "high": price, "low": price, "close": price, "volume": 0.0, "trades": 0,
                   "spread_bps_max": None, "bid_volume": None, "ask_volume": None,
                   "_spread_sum": 0.0, "_spread_count": 0, "_last_at": at}
            book = self._books.get(symbol)
            if book and at - book["at"] <= BOOK_MAX_AGE_SEC:
                self._apply_book(bar, book)
            self._bars[symbol] = bar
        bar["high"], bar["low"] = max(bar["high"], price), min(bar["low"], price)
        bar["close"] = price
        bar["volume"] += volume
        bar["trades"] += 1
        bar["_last_at"] = at

    def tick(self, at: Optional[float] = None) -> None:
        """이벤트가 끊겨도 지난 분을 저장하고 정규장 기준가를 남긴다."""
        at = time.time() if at is None else at
        for symbol, updated_at in list(self._dirty_refs.items()):
            self.store.set_extended_ref(symbol, self._refs[symbol], updated_at)
            self._dirty_refs.pop(symbol, None)
        minute = _minute(at)
        for symbol, bar in list(self._bars.items()):
            if bar["minute"] < minute:
                self._close_bar(symbol)

    def _close_bar(self, symbol: str) -> None:
        bar = self._bars.pop(symbol, None)
        if not bar:
            return
        count = bar["_spread_count"]
        bar["spread_bps_avg"] = bar["_spread_sum"] / count if count else None
        bar["ref_price"] = self._refs.get(symbol)
        self.store.upsert_extended_minute(bar)
        self.minutes_saved += 1
        self._check_alert(bar)

    def _check_alert(self, bar: Dict[str, Any]) -> None:
        symbol, ref = bar["symbol"], bar["ref_price"]
        if not ref:
            return
        move = (bar["close"] / ref - 1) * 100
        threshold = LEVERAGED_MOVE_ALERT_PCT if symbol in WATCH_ONLY_SYMBOLS else MOVE_ALERT_PCT
        if abs(move) < threshold:
            self._streaks.pop(symbol, None)
            return
        sign = 1 if move > 0 else -1
        streak = self._streaks.get(symbol)
        streak = [sign, streak[1] + 1] if streak and streak[0] == sign else [sign, 1]
        self._streaks[symbol] = streak
        if streak[1] < ALERT_CONFIRM_MINUTES:
            return
        last = self._alerted.get(symbol)
        if last:
            if bar["_last_at"] - last["at"] < ALERT_COOLDOWN_SEC:
                return
            if last["sign"] == sign and abs(move) < last["move"] + threshold:
                return
        self._alerted[symbol] = {"sign": sign, "move": abs(move), "at": bar["_last_at"]}
        self.alerts_sent += 1
        self.notify(self._alert_text(bar, ref, move))

    def _alert_text(self, bar: Dict[str, Any], ref: float, move: float) -> str:
        spread = bar["spread_bps_avg"]
        lines = [
            f"{'📈' if move > 0 else '📉'} <b>[{PHASE_LABELS[bar['phase']]}] {bar['symbol']} {move:+.1f}%</b>",
            f"직전 정규장 ${ref:,.2f} → ${bar['close']:,.2f}",
            f"스프레드 {spread:.0f}bps · 1분 거래량 {bar['volume']:,.0f}" if spread is not None
            else f"호가 없음 · 1분 거래량 {bar['volume']:,.0f}",
        ]
        news = self.store.recent_news(bar["_last_at"] - NEWS_LOOKBACK_SEC)
        if news:
            lines.append("최근 뉴스:")
            lines.extend(f"· {html.escape(title[:100])}" for title in news)
        lines.append("관찰 전용 — 봇은 주문하지 않습니다")
        return "\n".join(lines)

    def status(self) -> Dict[str, Any]:
        return {
            "phase": self.current_phase(),
            "minutes_saved": self.minutes_saved,
            "alerts_sent": self.alerts_sent,
            "reference_prices": len(self._refs),
            "unclassified_events": self.unclassified_events,
        }


def parse_feed(source: str, content: bytes) -> List[Dict[str, Any]]:
    root = ET.fromstring(content)
    items = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        link = (item.findtext("link") or "").strip()
        items.append({
            "id": hashlib.sha1(f"{source}|{link or title}".encode("utf-8")).hexdigest(),
            "source": source, "title": title[:300], "link": link[:500],
            "published_at": (item.findtext("pubDate") or "").strip()[:64] or None,
        })
    return items


class NewsPoller:
    """장외 시간에만 RSS 를 돈다. 기사 시각이 아니라 '우리가 처음 본 시각'을 기록한다."""

    def __init__(self, store, watch: ExtendedWatch, fetch=requests.get):
        self.store = store
        self.watch = watch
        self.fetch = fetch
        self.new_items = 0
        self.failed_feeds: Dict[str, str] = {}
        self._baselined = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def poll_once(self, phase: str, at: Optional[float] = None) -> None:
        at = time.time() if at is None else at
        for source, url in NEWS_FEEDS:
            try:
                response = self.fetch(url, timeout=10)
                response.raise_for_status()
                if len(response.content) > NEWS_MAX_BYTES:
                    raise ValueError("응답이 너무 큽니다")
                items = parse_feed(source, response.content)
            except Exception as exc:
                if source not in self.failed_feeds:
                    logger.warning("[ExtendedWatch] 뉴스 수집 실패 (%s): %s", source, exc)
                self.failed_feeds[source] = type(exc).__name__
                continue
            self.failed_feeds.pop(source, None)
            added = self.store.record_news(items, at, phase, source not in self._baselined)
            if source in self._baselined:
                self.new_items += added
            self._baselined.add(source)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="ExtendedNews")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            phase = self.watch.current_phase()
            if phase in EXTENDED_PHASES:
                try:
                    self.poll_once(phase)
                except Exception as exc:
                    logger.warning("[ExtendedWatch] 뉴스 저장 실패: %s", exc)
            self._stop.wait(NEWS_POLL_SEC)

    def status(self) -> Dict[str, Any]:
        return {"news_new_items": self.new_items, "news_failed_feeds": dict(self.failed_feeds)}
