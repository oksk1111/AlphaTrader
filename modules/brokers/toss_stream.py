"""토스증권 실시간 시세·호가·주문 이벤트 스트림.

수신 콜백에서는 주문하지 않는다. 이벤트를 bounded queue에 넣고 전략 루프 한 곳에서
직렬 처리해 같은 신호의 중복 주문을 막는다.
"""

import asyncio
import json
import logging
import queue
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import websockets

from modules.brokers.models import Market, OrderState


WS_URL = "wss://openapi-ws.tossinvest.com/ws/v1"
_ORDER_EVENT_STATE = {
    "PENDING": OrderState.PENDING,
    "PARTIAL_FILL": OrderState.PARTIAL_FILLED,
    "FILL": OrderState.FILLED,
    "CANCELED": OrderState.CANCELED,
    "REJECTED": OrderState.REJECTED,
    "CANCEL_REJECTED": OrderState.REJECTED,
    "REPLACE_REJECTED": OrderState.REJECTED,
}


@dataclass(frozen=True)
class MarketEvent:
    kind: str
    topic: str
    symbol: Optional[str]
    market: Optional[Market]
    timestamp: Optional[str]
    data: Dict[str, Any]
    received_at: float


class TossStreamProtocolError(ValueError):
    pass


def subscription_declaration(symbols: Iterable[Tuple[Market, str]],
                             account_seq: Optional[str] = None,
                             request_id: str = "subscriptions-1") -> List[Dict[str, Any]]:
    grouped: Dict[str, List[str]] = {}
    for market, symbol in symbols:
        market_code = market.value.lower()
        grouped.setdefault(f"trade:{market_code}", []).append(symbol)
        grouped.setdefault(f"orderbook:{market_code}", []).append(symbol)
    declarations: List[Dict[str, Any]] = [{"id": request_id}]
    for kind, codes in sorted(grouped.items()):
        declarations.append({"type": kind, "codes": sorted(set(codes))})
    if account_seq:
        declarations.append({"type": "personal:order", "codes": [str(account_seq)]})
    topic_count = sum(len(item.get("codes", [])) for item in declarations)
    if topic_count > 100:
        raise ValueError(f"토스 웹소켓 구독은 연결당 최대 100건입니다: {topic_count}")
    return declarations


def parse_frame(raw: str, received_at: Optional[float] = None) -> Optional[MarketEvent]:
    if raw == "PING":
        return MarketEvent("ping", "", None, None, None, {}, received_at or time.time())
    try:
        frame = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise TossStreamProtocolError("웹소켓 JSON 형식이 아닙니다") from exc
    frame_type = frame.get("type")
    now = received_at or time.time()
    if frame_type == "pong":
        return MarketEvent("pong", "", None, None, None, {}, now)
    if frame_type == "subscriptions":
        return MarketEvent("subscriptions", "", None, None, None, frame, now)
    if frame_type == "error":
        return MarketEvent("error", "", None, None, None, frame.get("error") or {}, now)
    if frame_type != "message":
        raise TossStreamProtocolError(f"알 수 없는 웹소켓 프레임: {frame_type}")
    topic = frame.get("topic", "")
    data = frame.get("data") or {}
    parts = topic.split(":")
    if len(parts) < 3:
        raise TossStreamProtocolError(f"잘못된 topic: {topic}")
    if parts[0] in ("trade", "orderbook"):
        try:
            market = Market(parts[1].upper())
        except ValueError as exc:
            raise TossStreamProtocolError(f"지원하지 않는 시장 topic: {topic}") from exc
        symbol = ":".join(parts[2:])
        return MarketEvent(parts[0], topic, symbol, market, data.get("timestamp"), data, now)
    if parts[0] == "personal" and parts[1] == "order":
        order = data.get("order") or {}
        currency = order.get("currency")
        market = Market.KR if currency == "KRW" else Market.US if currency == "USD" else None
        normalized = dict(data)
        normalized["state"] = _ORDER_EVENT_STATE.get(data.get("event"), OrderState.UNKNOWN).value
        return MarketEvent("order", topic, order.get("symbol"), market,
                           order.get("orderedAt"), normalized, now)
    raise TossStreamProtocolError(f"지원하지 않는 topic: {topic}")


class TossStream:
    def __init__(self, token_provider, symbols: Iterable[Tuple[Market, str]],
                 account_seq: Optional[str] = None, max_queue: int = 10000):
        self.token_provider = token_provider
        self.symbols = list(symbols)
        self.account_seq = str(account_seq) if account_seq else None
        self.events: "queue.Queue[MarketEvent]" = queue.Queue(maxsize=max_queue)
        self.connected = False
        self.last_message_at = 0.0
        self.last_pong_at = 0.0
        self.subscribed: List[str] = []
        self.rejected: List[Dict[str, Any]] = []
        self.dropped_market_events = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def message_age_sec(self) -> Optional[float]:
        return max(0.0, time.time() - self.last_message_at) if self.last_message_at else None

    def status(self) -> Dict[str, Any]:
        return {
            "connected": self.connected,
            "message_age_sec": self.message_age_sec,
            "last_pong_age_sec": (max(0.0, time.time() - self.last_pong_at)
                                   if self.last_pong_at else None),
            "subscriptions": len(self.subscribed),
            "rejected": list(self.rejected),
            "queue_size": self.events.qsize(),
            "dropped_market_events": self.dropped_market_events,
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="TossStream")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        asyncio.run(self._connect_forever())

    def _put(self, event: MarketEvent) -> None:
        try:
            self.events.put_nowait(event)
        except queue.Full:
            if event.kind == "order":
                # 주문 이벤트는 lossless가 전제다. 소비 지연을 드러내고 연결을 재동기화한다.
                raise RuntimeError("주문 이벤트 큐가 가득 찼습니다")
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            self.dropped_market_events += 1
            self.events.put_nowait(event)

    async def _connect_forever(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                token = self.token_provider()
                async with websockets.connect(
                    WS_URL, extra_headers={"Authorization": f"Bearer {token}"},
                    ping_interval=None, close_timeout=5,
                ) as websocket:
                    self.connected = True
                    attempt = 0
                    declaration = subscription_declaration(self.symbols, self.account_seq)
                    await websocket.send(json.dumps(declaration))
                    next_ping = time.monotonic() + 60
                    while not self._stop.is_set():
                        timeout = max(0.1, next_ping - time.monotonic())
                        try:
                            raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
                        except asyncio.TimeoutError:
                            await websocket.send("PING")
                            next_ping = time.monotonic() + 60
                            continue
                        self.last_message_at = time.time()
                        event = parse_frame(raw, self.last_message_at)
                        if not event:
                            continue
                        if event.kind == "pong":
                            self.last_pong_at = self.last_message_at
                        elif event.kind == "subscriptions":
                            self.subscribed = list(event.data.get("subscribed") or [])
                            self.rejected = list(event.data.get("rejected") or [])
                        elif event.kind == "error":
                            logging.error("Toss WS error: %s", event.data)
                            if event.data.get("code") == "server-shutdown":
                                break
                        else:
                            self._put(event)
            except Exception as exc:
                logging.error("Toss WS disconnected: %s", exc)
            finally:
                self.connected = False
            if self._stop.is_set():
                break
            attempt += 1
            await asyncio.sleep(min(30.0, (2 ** min(attempt, 5)) + random.random()))
