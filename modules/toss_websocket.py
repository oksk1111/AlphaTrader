"""
[v8.0] 토스증권 실시간 웹소켓 — 체결 틱 + 본인 주문 이벤트.

`KisWebSocket(tickers, callback)` 과 같은 사용법 (`start_background()`, `stop()`,
`connected`). 추가로:
  - `personal:order` 를 함께 구독해 주문 접수·체결·거부를 **푸시로** 받는다.
    `toss_api.record_order_event()` 에 넣으므로 `wait_order()` 가 REST 폴링 없이
    체결을 확인한다 — 주문→체결 확인 지연이 폴링 주기(1초)가 아니라 이벤트 도착 시간이 된다.
  - `update_tickers()` 로 구독을 바꿔도 재연결하지 않는다 (선언형 full-replace).

토스 웹소켓 규칙 (overview.md):
  - 계정당 동시 연결 2개. 초과 시 **가장 오래된 연결이 끊긴다** → 봇은 연결 1개만 유지.
  - 클라이언트 수신이 180초 없으면 서버가 끊는다 → 60초마다 텍스트 `PING`.
  - 연결당 구독 100건. 끊긴 동안의 주문 이벤트는 재전송되지 않는다 →
    `wait_order()` 는 REST 조회로 보완한다.

KisWebSocket 과 달리 `stop()` 은 백그라운드 스레드 모드에서도 실제로 연결을 닫는다
(예전 구현은 스레드 모드에서 task 가 없어 세션마다 연결이 쌓였다).
"""

import asyncio
import json
import logging
import threading
import time

import websockets

from config import TOSS_WS_URL
from modules import toss_api

PING_INTERVAL_SEC = 60
MAX_CODES = 100


def _status_of(exc):
    """핸드셰이크 실패의 HTTP 상태 (websockets 버전별로 위치가 다르다)."""
    for path in (("status_code",), ("response", "status_code"), ("response", "status")):
        obj = exc
        for attr in path:
            obj = getattr(obj, attr, None)
        if isinstance(obj, int):
            return obj
    return None


def _connect(url, token):
    headers = {"Authorization": f"Bearer {token}"}
    try:
        return websockets.connect(url, additional_headers=headers, ping_interval=30,
                                  open_timeout=10, max_queue=1024)
    except TypeError:   # websockets < 14
        return websockets.connect(url, extra_headers=headers, ping_interval=30,
                                  open_timeout=10, max_queue=1024)


class TossWebSocket:
    def __init__(self, tickers, callback, market="US", on_order=None, subscribe_orders=True):
        self.market = market.upper()
        self.tickers = list(dict.fromkeys(t for t in (tickers or []) if t))
        self.callback = callback
        self.on_order = on_order
        self.subscribe_orders = subscribe_orders
        self.connected = False
        self.last_message_at = 0.0
        self.last_trade_at = {}
        self._stop = False
        self._loop = None
        self._ws = None
        self._thread = None
        self._decl_seq = 0

    # ---- 구독 선언 -----------------------------------------------------------
    def _declaration(self):
        self._decl_seq += 1
        decl = [{"id": f"sniper-{self._decl_seq}"}]
        mk = "us" if self.market == "US" else "kr"
        budget = MAX_CODES - (1 if self.subscribe_orders else 0)
        codes = self.tickers[:budget]
        if len(self.tickers) > budget:
            logging.warning(f"[TOSS-WS] 구독 한도 {MAX_CODES} 초과 — {len(self.tickers) - budget}종목 제외")
        if codes:
            decl.append({"type": f"trade:{mk}", "codes": codes})
        if self.subscribe_orders:
            try:
                decl.append({"type": "personal:order", "codes": [toss_api.session().account_seq()]})
            except Exception as e:
                logging.error(f"[TOSS-WS] 계좌 식별 실패 — 주문 이벤트 구독 생략: {e}")
        return decl

    async def _declare(self, ws):
        decl = self._declaration()
        if len(decl) > 1:
            await ws.send(json.dumps(decl))

    def update_tickers(self, tickers):
        """구독 종목 교체. 연결 중이면 즉시 재선언한다."""
        self.tickers = list(dict.fromkeys(t for t in (tickers or []) if t))
        if self._loop and self._ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._declare(self._ws), self._loop)
            except Exception as e:
                logging.warning(f"[TOSS-WS] 재선언 실패: {e}")

    # ---- 수신 ----------------------------------------------------------------
    async def _dispatch(self, raw):
        if not isinstance(raw, str):
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        mtype = msg.get("type")
        if mtype == "message":
            topic = msg.get("topic") or ""
            data = msg.get("data") or {}
            if topic.startswith("trade:"):
                ticker = topic.split(":", 2)[-1]
                try:
                    price = float(data.get("price") or 0)
                except (TypeError, ValueError):
                    return
                if price <= 0:
                    return
                self.last_trade_at[ticker] = time.time()
                toss_api.record_trade(ticker, price)
                try:
                    if asyncio.iscoroutinefunction(self.callback):
                        await self.callback(ticker, price)
                    else:
                        self.callback(ticker, price)
                except Exception as e:
                    logging.debug(f"[TOSS-WS] price callback error {ticker}: {e}")
            elif topic.startswith("personal:order"):
                order = data.get("order") or {}
                toss_api.record_order_event(order)
                logging.info(f"[TOSS-WS] 주문 이벤트 {data.get('event')} {order.get('side')} "
                             f"{order.get('symbol')} 체결 {((order.get('execution') or {}).get('filledQuantity'))}"
                             f"/{order.get('quantity')} @ {((order.get('execution') or {}).get('averageFilledPrice'))}")
                if self.on_order:
                    try:
                        self.on_order(data)
                    except Exception as e:
                        logging.warning(f"[TOSS-WS] on_order callback error: {e}")
        elif mtype == "subscriptions":
            rejected = msg.get("rejected") or []
            logging.info(f"[TOSS-WS] 구독 확정 {len(msg.get('subscribed') or [])}건"
                         + (f", 거부 {rejected}" if rejected else ""))
        elif mtype == "error":
            err = msg.get("error") or {}
            logging.error(f"[TOSS-WS] 오류 프레임: {err.get('code')} {err.get('message')}")
            if err.get("code") == "server-shutdown":
                raise ConnectionError("server-shutdown")
            if err.get("code") == "rate-limit-exceeded":
                await asyncio.sleep(1.0)

    async def _pinger(self, ws):
        while True:
            await asyncio.sleep(PING_INTERVAL_SEC)
            await ws.send("PING")

    async def connect_loop(self):
        backoff = 1.0
        while not self._stop:
            token = None
            try:
                token = toss_api.session().token()
                logging.info(f"[TOSS-WS] Connecting {TOSS_WS_URL} ({self.market}, {len(self.tickers)}종목)")
                async with _connect(TOSS_WS_URL, token) as ws:
                    self._ws = ws
                    self.connected = True
                    backoff = 1.0
                    await self._declare(ws)
                    ping = asyncio.ensure_future(self._pinger(ws))
                    try:
                        async for raw in ws:
                            self.last_message_at = time.time()
                            await self._dispatch(raw)
                            if self._stop:
                                break
                    finally:
                        ping.cancel()
            except asyncio.CancelledError:
                break
            except Exception as e:
                status = _status_of(e)
                if status == 401 and token:
                    logging.warning("[TOSS-WS] 401 — 토큰 갱신 후 재연결")
                    try:
                        toss_api.session()._recover_auth(token)
                    except Exception as te:
                        logging.error(f"[TOSS-WS] 토큰 갱신 실패: {te}")
                elif status == 403:
                    logging.error("[TOSS-WS] 403 — 허용 IP 미등록 (WTS 설정 > Open API > 허용 IP 관리)")
                    backoff = 60.0
                elif not self._stop:
                    logging.error(f"[TOSS-WS] 연결 끊김: {e}")
            finally:
                self._ws = None
                self.connected = False
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 2)
        logging.info("[TOSS-WS] 연결 루프 종료")

    # ---- 수명 ----------------------------------------------------------------
    def start_background(self):
        """백그라운드 데몬 스레드에서 연결 루프를 돌린다 (run_bot 은 동기 코드)."""
        if self._thread and self._thread.is_alive():
            return

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                loop.run_until_complete(self.connect_loop())
            finally:
                loop.close()
                self._loop = None

        self._stop = False
        self._thread = threading.Thread(target=run, daemon=True, name=f"TossWS-{self.market}")
        self._thread.start()
        logging.info(f"[TOSS-WS] 백그라운드 스레드 시작 ({self.market})")

    def wait_connected(self, timeout_sec=5.0):
        deadline = time.time() + timeout_sec
        while time.time() < deadline and not self.connected:
            time.sleep(0.05)
        return self.connected

    def stop(self, join_timeout=5.0):
        self._stop = True
        loop, ws = self._loop, self._ws
        if loop and ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=join_timeout)
        logging.info("[TOSS-WS] stopped")
