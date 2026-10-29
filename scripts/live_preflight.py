#!/usr/bin/env python3
"""Read-only Toss live readiness preflight. Never submits an order."""

from __future__ import annotations

import argparse
import json
import sys
import time

from modules.brokers.models import BrokerError, Market
from modules.brokers.toss import TossBroker
from modules.brokers.toss_stream import TossStream
from modules.live_gate import record_preflight, readiness

PROBES = {Market.KR: "069500", Market.US: "XLK"}


def _wait_for_stream(stream: TossStream, timeout_sec: float) -> dict:
    deadline = time.monotonic() + max(1.0, timeout_sec)
    latest = stream.status()
    while time.monotonic() < deadline:
        latest = stream.status()
        if latest["connected"] and latest["subscriptions"] and not latest["rejected"]:
            return latest
        time.sleep(0.2)
    raise BrokerError(f"웹소켓 구독 확인 실패: {latest}", code="live-preflight-websocket")


def run(timeout_sec: float = 15.0) -> dict:
    # Shadow mode deliberately ensures this command cannot transmit an order even
    # if someone imports it from an interactive production shell.
    broker = TossBroker(execution_mode="shadow")
    broker.authenticate()
    if not broker.account_seq:
        broker.account_seq = broker._resolve_account_seq()
    snapshot = broker.account_snapshot()
    quotes = {}
    for market, symbol in PROBES.items():
        quote = broker.quote(symbol, market)
        if quote.price <= 0:
            raise BrokerError(f"{market.value} {symbol} 현재가가 유효하지 않습니다", code="live-preflight-quote")
        quotes[market.value] = {"symbol": symbol, "price": quote.price}

    stream = TossStream(broker.authenticate, list(PROBES.items()), broker.account_seq)
    stream.start()
    try:
        stream_status = _wait_for_stream(stream, timeout_sec)
    finally:
        stream.stop()

    details = {
        "account_seq_configured": bool(broker.account_seq),
        "cash": {"KRW": snapshot.cash_for(Market.KR), "USD": snapshot.cash_for(Market.US)},
        "holdings": {"KR": len(snapshot.holdings_for(Market.KR)), "US": len(snapshot.holdings_for(Market.US))},
        "quotes": quotes,
        "websocket": stream_status,
    }
    record_preflight(("KR", "US"), details)
    return {"status": "ok", "markets": ["KR", "US"], "details": details}


def main() -> int:
    parser = argparse.ArgumentParser(description="Toss read-only live preflight (never orders)")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.timeout), ensure_ascii=False, indent=2))
        return 0
    except BrokerError as exc:
        print(json.dumps({"status": "failed", "code": exc.code, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
