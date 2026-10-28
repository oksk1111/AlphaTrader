"""Toss shadow 데이터로 장중 전략을 walk-forward 검증한다.

실행:
    python scripts/walk_forward_intraday.py
    python scripts/walk_forward_intraday.py --market US --json database/intraday_gate.json

이 스크립트의 gate가 통과해도 자동으로 live를 켜지 않는다. 데이터 수와 비용 반영 여부를
운영자가 확인한 뒤 별도 승인해야 한다.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules.brokers.models import Bar, Market, Quote  # noqa: E402
from modules.intraday_strategy import (  # noqa: E402
    IntradayContext, evaluate_intraday, evaluate_overnight,
)


@dataclass
class Trade:
    market: str
    symbol: str
    session: str
    entry: float
    exit: float
    gross_return_pct: float
    net_return_pct: float
    overnight: bool


@dataclass
class GateResult:
    passed: bool
    reasons: List[str]
    sessions: int
    trades: int
    total_return_pct: float
    max_drawdown_pct: float
    profit_factor: float
    expectancy_pct: float
    win_rate_pct: float
    turnover: int
    overnight_trades: int


COST_PCT = {"KR": 0.23, "US": 0.60}  # 왕복 수수료+세금+보수적 슬리피지


def _load_config() -> Dict:
    with open(ROOT / "user_config.json", encoding="utf-8") as file:
        return json.load(file)


def _symbols(config: Dict, market: str) -> List[str]:
    raw = ((config.get("momentum_rotation") or {}).get(market.lower()) or {}).get("universe") or []
    out = []
    for item in raw:
        symbol = (item.get("symbol") or item.get("code")) if isinstance(item, dict) else str(item)
        if symbol:
            out.append(symbol)
    return out


def _minute_rows(conn, market: str, symbol: str) -> Dict[str, List[Bar]]:
    rows = conn.execute(
        """SELECT minute, currency, open, high, low, close, volume
           FROM minute_bars WHERE market=? AND symbol=? ORDER BY minute""",
        (market, symbol),
    ).fetchall()
    sessions: Dict[str, List[Bar]] = {}
    for row in rows:
        session = row[0][:10]
        sessions.setdefault(session, []).append(Bar(
            symbol=symbol, market=Market(market), timestamp=row[0],
            currency=row[1] or ("KRW" if market == "KR" else "USD"),
            open=row[2], high=row[3], low=row[4], close=row[5], volume=row[6],
            interval="1m",
        ))
    return sessions


def _daily_rows(conn, market: str, symbol: str) -> List[Bar]:
    rows = conn.execute(
        """SELECT trading_day, currency, open, high, low, close, volume
           FROM daily_bars WHERE market=? AND symbol=? ORDER BY trading_day""",
        (market, symbol),
    ).fetchall()
    return [Bar(
        symbol=symbol, market=Market(market), timestamp=row[0],
        currency=row[1] or ("KRW" if market == "KR" else "USD"),
        open=row[2], high=row[3], low=row[4], close=row[5], volume=row[6], interval="1d",
    ) for row in rows]


def _daily_closes_before(daily: Sequence[Bar], session: str) -> List[float]:
    return [bar.close for bar in daily if bar.timestamp[:10] < session]


def _drawdown(equity: Sequence[float]) -> float:
    peak = 1.0
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst * 100.0


def simulate_market(conn, market: str, symbols: Sequence[str], cfg: Dict) -> GateResult:
    sessions_of = {symbol: _minute_rows(conn, market, symbol) for symbol in symbols}
    daily_of = {symbol: _daily_rows(conn, market, symbol) for symbol in symbols}
    all_sessions = sorted({day for sessions in sessions_of.values() for day in sessions})
    trades: List[Trade] = []
    equity = 1.0
    curve = [equity]
    min_score = float(cfg.get("min_buy_score", 0.62))
    overnight_score = float(cfg.get("min_overnight_score", 0.72))
    spread = float(cfg.get("max_spread_pct", 0.35))

    for session in all_sessions:
        for symbol in symbols:
            bars = sessions_of[symbol].get(session) or []
            daily_closes = _daily_closes_before(daily_of[symbol], session)
            if len(bars) < 30 or len(daily_closes) < 61:
                continue
            # 첫 30분 이후부터 한 분씩 평가하고 그 다음 봉 시가에 체결한다.
            entry_i = None
            for i in range(29, len(bars) - 1):
                current = bars[i]
                quote = Quote(symbol, Market(market), current.currency, current.close)
                decision = evaluate_intraday(IntradayContext(
                    symbol=symbol, market=Market(market), bars=bars[:i + 1], quote=quote,
                    daily_closes=daily_closes, relative_volume=1.0,
                    minutes_to_close=max(0, len(bars) - i - 1), stream_age_sec=0,
                ), min_score=min_score, max_spread_pct=spread)
                if decision.action == "buy":
                    entry_i = i + 1
                    break
            if entry_i is None:
                continue
            entry = bars[entry_i].open
            final = bars[-1]
            overnight = evaluate_overnight(IntradayContext(
                symbol=symbol, market=Market(market), bars=bars,
                quote=Quote(symbol, Market(market), final.currency, final.close),
                daily_closes=daily_closes, relative_volume=1.0, minutes_to_close=0,
            ), min_score=overnight_score).keep
            # 오버나잇은 다음 거래일 첫 봉 시가, 없으면 당일 마지막 종가로 보수적 청산.
            exit_price = final.close
            if overnight:
                future = sorted(day for day in sessions_of[symbol] if day > session)
                if future and sessions_of[symbol][future[0]]:
                    exit_price = sessions_of[symbol][future[0]][0].open
            gross = (exit_price / entry - 1.0) * 100.0
            net = gross - COST_PCT[market]
            trades.append(Trade(market, symbol, session, entry, exit_price, gross, net, overnight))
            equity *= 1.0 + net / 100.0
            curve.append(equity)

    wins = [trade.net_return_pct for trade in trades if trade.net_return_pct > 0]
    losses = [-trade.net_return_pct for trade in trades if trade.net_return_pct < 0]
    profit_factor = sum(wins) / sum(losses) if losses else (999.0 if wins else 0.0)
    expectancy = sum(trade.net_return_pct for trade in trades) / len(trades) if trades else 0.0
    reasons = []
    min_sessions = int(cfg.get("min_shadow_sessions", 20))
    if len(all_sessions) < min_sessions:
        reasons.append(f"수집 세션 {len(all_sessions)} < {min_sessions}")
    if len(trades) < 20:
        reasons.append(f"검증 거래 {len(trades)} < 20")
    if expectancy <= 0:
        reasons.append(f"비용 후 기대값 {expectancy:.3f}% ≤ 0")
    if profit_factor <= 1.0:
        reasons.append(f"profit factor {profit_factor:.2f} ≤ 1")
    max_dd = _drawdown(curve)
    if max_dd < -10.0:
        reasons.append(f"MDD {max_dd:.1f}% < -10%")
    return GateResult(
        passed=not reasons, reasons=reasons, sessions=len(all_sessions), trades=len(trades),
        total_return_pct=(equity - 1.0) * 100.0, max_drawdown_pct=max_dd,
        profit_factor=profit_factor, expectancy_pct=expectancy,
        win_rate_pct=(len(wins) / len(trades) * 100.0) if trades else 0.0,
        turnover=len(trades) * 2, overnight_trades=sum(1 for trade in trades if trade.overnight),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "database" / "market_data.sqlite"))
    parser.add_argument("--market", choices=["KR", "US", "ALL"], default="ALL")
    parser.add_argument("--json", default=str(ROOT / "database" / "intraday_gate.json"))
    args = parser.parse_args()
    db = Path(args.db)
    if not db.exists():
        print(f"shadow DB 없음: {db}")
        return 2
    config = _load_config()
    intraday = config.get("intraday_strategy") or {}
    conn = sqlite3.connect(str(db))
    output = {"generated_at": datetime.now().isoformat(timespec="seconds"), "markets": {}}
    try:
        markets = ["KR", "US"] if args.market == "ALL" else [args.market]
        for market in markets:
            result = simulate_market(conn, market, _symbols(config, market), intraday)
            output["markets"][market] = asdict(result)
            print(f"[{market}] {'PASS' if result.passed else 'HOLD'} sessions={result.sessions} "
                  f"trades={result.trades} return={result.total_return_pct:.2f}% "
                  f"MDD={result.max_drawdown_pct:.2f}% PF={result.profit_factor:.2f} "
                  f"expectancy={result.expectancy_pct:.3f}%")
            for reason in result.reasons:
                print(f"  - {reason}")
    finally:
        conn.close()
    output["passed"] = bool(output["markets"]) and all(
        row["passed"] for row in output["markets"].values())
    path = Path(args.json)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved: {path}")
    return 0 if output["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
