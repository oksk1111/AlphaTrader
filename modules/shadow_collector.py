"""Toss 실시간 데이터를 수집하는 shadow 런타임.

봇과 별도 프로세스로 실행한다. 실제 주문은 절대 제출하지 않는다.
"""

import json
import os
import queue
import shutil
import signal
import threading
import time
from pathlib import Path
from typing import Dict, List, Tuple

from modules import market_clock
from modules.brokers.models import Market, Quote
from modules.brokers.toss import TossBroker
from modules.brokers.toss_stream import TossStream
from modules.extended_watch import WATCH_ONLY_SYMBOLS, ExtendedWatch, NewsPoller, is_watch_only
from modules.intraday_strategy import IntradayContext, evaluate_intraday
from modules.logger import logger
from modules.market_data_store import MarketDataStore
from modules.telegram_notifier import get_notifier


STATUS_FILE = Path("database/toss_stream_status.json")
WATCH_ERROR_LOG_SEC = 60


def _storage_status(store: MarketDataStore) -> Dict:
    """원본 틱은 삭제 없이 쌓인다. 디스크가 차면 봇도 멈추므로 크기를 화면에 드러낸다."""
    size = sum(p.stat().st_size for p in (store.path, Path(f"{store.path}-wal")) if p.exists())
    return {"market_db_bytes": size, "disk_free_bytes": shutil.disk_usage(store.path.parent).free}


def _notify_async(message: str) -> None:
    # 텔레그램 재시도가 수신 큐 소비를 붙잡지 않게 한다.
    threading.Thread(target=get_notifier().send_message, args=(message,), daemon=True).start()


def load_symbols() -> List[Tuple[Market, str]]:
    with open("user_config.json", encoding="utf-8") as file:
        config = json.load(file)
    rotation = config.get("momentum_rotation") or {}
    symbols: List[Tuple[Market, str]] = []
    for item in (rotation.get("kr") or {}).get("universe") or []:
        code = item.get("symbol") if isinstance(item, dict) else str(item)
        if code:
            symbols.append((Market.KR, code))
    for item in (rotation.get("us") or {}).get("universe") or []:
        code = item.get("symbol") if isinstance(item, dict) else str(item)
        if code:
            symbols.append((Market.US, code))
    # 구독 full key는 체결+호가 각각 1개이므로 현재 유니버스는 100건 이내여야 한다.
    return list(dict.fromkeys(symbols))


def _write_status(status: Dict) -> None:
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({**status, "updated_at": time.time()}, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, STATUS_FILE)


def run() -> None:
    broker = TossBroker(execution_mode="shadow")
    broker.authenticate()
    if not broker.account_seq:
        broker.account_seq = broker._resolve_account_seq()
    symbols = load_symbols()
    watch_symbols = [(Market.US, code) for code in WATCH_ONLY_SYMBOLS]
    stream = TossStream(broker.authenticate, list(dict.fromkeys(symbols + watch_symbols)),
                        broker.account_seq)
    store = MarketDataStore()
    watch = ExtendedWatch(store, broker.us_market_calendar, _notify_async)
    news = NewsPoller(store, watch)
    last_watch_error = 0.0
    stopping = False

    def stop(*_args):
        nonlocal stopping
        stopping = True
        stream.stop()
        news.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logger.info("[TossShadow] 수집 시작: %d종목, 주문 전송 비활성", len(symbols))
    stream.start()
    news.start()
    last_status = 0.0
    last_signal_minute = {}
    daily_cache = {}
    try:
        while not stopping:
            try:
                event = stream.events.get(timeout=1.0)
            except queue.Empty:
                event = None
            if event:
                # 장외 관찰 실패가 기존 수집을 멈추면 안 된다.
                try:
                    watch.on_event(event)
                except Exception as exc:
                    if time.time() - last_watch_error >= WATCH_ERROR_LOG_SEC:
                        last_watch_error = time.time()
                        logger.error("[ExtendedWatch] 이벤트 처리 실패: %s", exc)
            if event and not is_watch_only(event):
                store.record_event(event)
                store.upsert_trade(event)
                if event.kind == "trade" and event.market and event.symbol:
                    minute = str(event.timestamp or "")[:16]
                    key = (event.market.value, event.symbol)
                    if minute and last_signal_minute.get(key) != minute:
                        last_signal_minute[key] = minute
                        minute_bars = store.minute_bars(event.market.value, event.symbol, 240)
                        if len(minute_bars) >= 22:
                            if key not in daily_cache:
                                try:
                                    daily_cache[key] = list(reversed(broker.bars(
                                        event.symbol, event.market, interval="1d", count=80)))
                                    store.upsert_daily_bars(daily_cache[key])
                                except Exception as exc:
                                    logger.warning("[TossShadow] %s 일봉 조회 실패: %s", event.symbol, exc)
                                    daily_cache[key] = []
                            daily_closes = [bar.close for bar in daily_cache[key]]
                            price = float(event.data.get("price") or 0)
                            decision = evaluate_intraday(IntradayContext(
                                symbol=event.symbol, market=event.market,
                                bars=minute_bars,
                                quote=Quote(event.symbol, event.market,
                                            event.data.get("currency", ""), price),
                                daily_closes=daily_closes,
                                stream_age_sec=stream.message_age_sec or 0,
                                minutes_to_close=market_clock.session_info(event.market.value).minutes_to_close,
                            ))
                            store.record_signal(
                                market_clock.session_key(event.market.value), event.market.value,
                                event.symbol, decision.action, decision.score,
                                decision.size_multiplier, decision.reasons,
                                decision.components, price,
                            )
            if time.time() - last_status >= 15:
                try:
                    watch.tick()
                except Exception as exc:
                    logger.error("[ExtendedWatch] 주기 저장 실패: %s", exc)
                status = stream.status()
                status.update({
                    "broker": "toss",
                    "execution_mode": "shadow",
                    "symbols": len(symbols),
                    "kr_sessions_collected": store.collected_sessions("KR"),
                    "us_sessions_collected": store.collected_sessions("US"),
                    "extended": {**watch.status(), **news.status(),
                                 "watch_only": list(WATCH_ONLY_SYMBOLS)},
                    "storage": _storage_status(store),
                })
                _write_status(status)
                last_status = time.time()
    finally:
        stream.stop()
        news.stop()
        try:
            watch.tick(time.time() + 60)      # 진행 중인 분까지 저장
        except Exception as exc:
            logger.error("[ExtendedWatch] 종료 저장 실패: %s", exc)
        store.close()
        logger.info("[TossShadow] 수집 종료")


if __name__ == "__main__":
    run()
