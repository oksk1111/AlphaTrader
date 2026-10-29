#!/usr/bin/env python3
"""Operator-approved one-share live order validation.

This command never runs from CI or the bot. It has an explicit two-step approval:
`authorize` writes a signed 15-minute cap, then `execute` consumes it before
sending exactly one order and verifies a confirmed fill plus account resync.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import json

from modules.brokers.models import BrokerError, Market, OrderRequest, OrderSide, OrderType
from modules.brokers.toss import TossBroker
from modules.live_gate import create_smoke_authorization, record_smoke


def _market(value: str) -> Market:
    try:
        return Market(value.upper())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("market must be KR or US") from exc


def authorize(args) -> dict:
    return create_smoke_authorization(args.market.value, args.symbol, args.max_notional)


def execute(args) -> dict:
    if args.confirm != "I_UNDERSTAND_THIS_SENDS_A_REAL_ORDER":
        raise BrokerError("--confirm 정확한 확인 문구가 필요합니다", code="live-smoke-confirmation-required")
    broker = TossBroker(execution_mode="live")
    quote = broker.quote(args.symbol, args.market)
    if quote.price <= 0:
        raise BrokerError("현재가가 유효하지 않습니다", code="live-smoke-quote")
    if args.market == Market.US:
        price = int(quote.price * 1.01 * (100 if quote.price >= 1 else 10000)) / (100 if quote.price >= 1 else 10000)
        order = OrderRequest(args.symbol, args.market, OrderSide.BUY, OrderType.LIMIT, quantity=1, price=price)
        notional = price
    else:
        order = OrderRequest(args.symbol, args.market, OrderSide.BUY, OrderType.MARKET, quantity=1)
        # The signed cap must conservatively cover a market order; price is the preflight quote.
        order = OrderRequest(args.symbol, args.market, OrderSide.BUY, OrderType.LIMIT, quantity=1, price=int(quote.price * 1.01))
        notional = order.price

    os.environ["LIVE_SMOKE_ACTIVE"] = "1"
    try:
        ack = broker.submit_and_wait(order, timeout_sec=args.timeout)
    finally:
        os.environ.pop("LIVE_SMOKE_ACTIVE", None)
    if ack.filled_quantity < 1:
        raise BrokerError(f"smoke 주문이 체결되지 않았습니다: {ack.state.value}", code="live-smoke-not-filled")
    snapshot = broker.account_snapshot()
    found = next((h for h in snapshot.holdings_for(args.market) if h.symbol == args.symbol), None)
    if not found or found.quantity < 1:
        raise BrokerError("smoke 체결 뒤 잔고 재동기화에 종목이 없습니다", code="live-smoke-resync-failed")
    details = {
        "symbol": args.symbol, "market": args.market.value, "order_id": ack.order_id,
        "filled_qty": ack.filled_quantity, "average_filled_price": ack.average_filled_price,
        "notional_cap_used": notional, "resynced_quantity": found.quantity,
    }
    record_smoke((args.market.value,), details)
    return details


def main() -> int:
    parser = argparse.ArgumentParser(description="explicit, one-share live Toss smoke validation")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("authorize", "execute"):
        item = sub.add_parser(name)
        item.add_argument("--market", type=_market, required=True)
        item.add_argument("--symbol", required=True, type=str.upper)
        item.add_argument("--max-notional", type=float, required=True)
        if name == "execute":
            item.add_argument("--timeout", type=float, default=15.0)
            item.add_argument("--confirm", default="")
    args = parser.parse_args()
    try:
        result = authorize(args) if args.command == "authorize" else execute(args)
        print(json.dumps({"status": "ok", "result": result}, ensure_ascii=False, indent=2))
        return 0
    except BrokerError as exc:
        print(json.dumps({"status": "failed", "code": exc.code, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
