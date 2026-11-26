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
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo
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
    # [2026-11-26 사전 등록] docs/preregistration/2026-11-26-intraday-gate.md
    verdict: str = ""                       # PASS | REJECT | INVALID
    source_tz: str = ""
    in_hours_share_pct: float = 0.0
    complete_sessions: int = 0
    stress_expectancy_pct: float = 0.0      # 비용 1.5배
    expectancy_ex_best_pct: float = 0.0
    baseline_trades: int = 0
    baseline_expectancy_pct: float = 0.0
    first_half_expectancy_pct: float = 0.0
    second_half_expectancy_pct: float = 0.0
    adoption_reasons: List[str] = None


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


# 정규장 (거래소 현지). 분봉 저장 시각은 토스 timestamp 앞 16자리라 시간대 표기가 잘려 있다
# (US 는 KST "+09:00" 로 온다 — tests/test_toss_stream.py). 그래서 시각대를 추정하고 거래소 현지
# 날짜로 세션을 나눈다. 문자열 앞 10자리로 나누면 미국장 하루가 KST 자정에서 두 세션으로 쪼개진다.
REGULAR = {"KR": ("Asia/Seoul", dtime(9, 0), dtime(15, 30)),
           "US": ("America/New_York", dtime(9, 30), dtime(16, 0))}
CANDIDATE_TZ = {"KR": ("Asia/Seoul", "UTC"), "US": ("Asia/Seoul", "America/New_York", "UTC")}
REGULAR_MINUTES = {"KR": 390, "US": 390}


def _parse_minute(text: str) -> Optional[datetime]:
    try:
        return datetime.strptime(text[:16].replace(" ", "T"), "%Y-%m-%dT%H:%M")
    except ValueError:
        return None


def _to_local(naive: datetime, source_tz: str, market: str) -> datetime:
    return naive.replace(tzinfo=ZoneInfo(source_tz)).astimezone(ZoneInfo(REGULAR[market][0]))


def _in_regular(local: datetime, market: str) -> bool:
    _, open_t, close_t = REGULAR[market]
    return local.weekday() < 5 and open_t <= local.time().replace(second=0, microsecond=0) < close_t


def infer_source_tz(minutes: Sequence[str], market: str) -> Tuple[str, float]:
    """정규장 안에 드는 분봉이 가장 많은 저장 시각대와 그 비율(%)."""
    parsed = [m for m in (_parse_minute(x) for x in minutes) if m]
    if not parsed:
        return CANDIDATE_TZ[market][0], 0.0
    best = max(CANDIDATE_TZ[market],
               key=lambda tz: sum(_in_regular(_to_local(m, tz, market), market) for m in parsed))
    share = sum(_in_regular(_to_local(m, best, market), market) for m in parsed) / len(parsed)
    return best, share * 100.0


def _minute_rows(conn, market: str, symbol: str, source_tz: Optional[str] = None) -> Dict[str, List[Bar]]:
    rows = conn.execute(
        """SELECT minute, currency, open, high, low, close, volume
           FROM minute_bars WHERE market=? AND symbol=? ORDER BY minute""",
        (market, symbol),
    ).fetchall()
    sessions: Dict[str, List[Bar]] = {}
    for row in rows:
        naive = _parse_minute(row[0])
        if naive is None:
            continue
        if source_tz:
            local = _to_local(naive, source_tz, market)
            if not _in_regular(local, market):
                continue          # 장외·잘못 들어온 분봉은 장중 판정에 쓰지 않는다
        else:
            local = naive
        session = local.strftime("%Y-%m-%d")
        sessions.setdefault(session, []).append(Bar(
            symbol=symbol, market=Market(market), timestamp=local.strftime("%Y-%m-%dT%H:%M"),
            currency=row[1] or ("KRW" if market == "KR" else "USD"),
            open=row[2], high=row[3], low=row[4], close=row[5], volume=row[6],
            interval="1m",
        ))
    for bars in sessions.values():
        bars.sort(key=lambda bar: bar.timestamp)
    return sessions


def _minutes_to_close(bar: Bar, market: str) -> float:
    local = datetime.strptime(bar.timestamp[:16], "%Y-%m-%dT%H:%M")
    close = datetime.combine(local.date(), REGULAR[market][2])
    return max(0.0, (close - local).total_seconds() / 60.0 - 1.0)   # 이 봉이 끝난 시점 기준


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


def _day_key(text: str) -> str:
    return str(text)[:10].replace("-", "")[:8]      # "2026-11-26" 과 "20261126" 을 같게 비교


def _daily_closes_before(daily: Sequence[Bar], session: str) -> List[float]:
    key = _day_key(session)
    return [bar.close for bar in daily if _day_key(bar.timestamp) < key]


def _drawdown(equity: Sequence[float]) -> float:
    peak = 1.0
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst * 100.0


def _expectancy(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def simulate_market(conn, market: str, symbols: Sequence[str], cfg: Dict) -> GateResult:
    minutes = [row[0] for row in conn.execute(
        "SELECT minute FROM minute_bars WHERE market=?", (market,)).fetchall()]
    source_tz, in_hours_pct = infer_source_tz(minutes, market)
    sessions_of = {symbol: _minute_rows(conn, market, symbol, source_tz) for symbol in symbols}
    daily_of = {symbol: _daily_rows(conn, market, symbol) for symbol in symbols}
    all_sessions = sorted({day for sessions in sessions_of.values() for day in sessions})
    half_full = REGULAR_MINUTES[market] * 0.5
    complete = sorted({day for sessions in sessions_of.values()
                       for day, bars in sessions.items() if len(bars) >= half_full})
    trades: List[Trade] = []
    baseline: List[float] = []
    equity = 1.0
    curve = [equity]
    min_score = float(cfg.get("min_buy_score", 0.62))
    overnight_score = float(cfg.get("min_overnight_score", 0.72))
    spread = float(cfg.get("max_spread_pct", 0.35))
    cost = COST_PCT[market]

    for session in all_sessions:
        for symbol in symbols:
            bars = sessions_of[symbol].get(session) or []
            daily_closes = _daily_closes_before(daily_of[symbol], session)
            if len(bars) < 31 or len(daily_closes) < 61:
                continue
            # 기준선: 같은 종목·세션에서 개장 30분 뒤 시가에 사서 종가에 판다 (사전 등록 기준 4).
            baseline.append((bars[-1].close / bars[30].open - 1.0) * 100.0 - cost)
            # 첫 30분 이후부터 한 분씩 평가하고 그 다음 봉 시가에 체결한다.
            entry_i = None
            for i in range(29, len(bars) - 1):
                current = bars[i]
                quote = Quote(symbol, Market(market), current.currency, current.close)
                decision = evaluate_intraday(IntradayContext(
                    symbol=symbol, market=Market(market), bars=bars[:i + 1], quote=quote,
                    daily_closes=daily_closes, relative_volume=1.0,
                    minutes_to_close=_minutes_to_close(current, market), stream_age_sec=0,
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
                daily_closes=daily_closes, relative_volume=1.0,
                minutes_to_close=_minutes_to_close(final, market),
            ), min_score=overnight_score).keep
            # 오버나잇은 다음 거래일 첫 봉 시가, 없으면 당일 마지막 종가로 보수적 청산.
            exit_price = final.close
            if overnight:
                future = sorted(day for day in sessions_of[symbol] if day > session)
                if future and sessions_of[symbol][future[0]]:
                    exit_price = sessions_of[symbol][future[0]][0].open
            gross = (exit_price / entry - 1.0) * 100.0
            net = gross - cost
            trades.append(Trade(market, symbol, session, entry, exit_price, gross, net, overnight))
            equity *= 1.0 + net / 100.0
            curve.append(equity)

    nets = [trade.net_return_pct for trade in trades]
    wins = [x for x in nets if x > 0]
    losses = [-x for x in nets if x < 0]
    profit_factor = sum(wins) / sum(losses) if losses else (999.0 if wins else 0.0)
    expectancy = _expectancy(nets)
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

    # --- 사전 등록 추가 기준 (docs/preregistration/2026-11-26-intraday-gate.md) ---
    stress = _expectancy([x - cost * 0.5 for x in nets])
    ex_best = _expectancy(sorted(nets)[:-1]) if len(nets) > 1 else 0.0
    base_exp = _expectancy(baseline)
    mid = all_sessions[len(all_sessions) // 2] if all_sessions else ""
    first = _expectancy([t.net_return_pct for t in trades if t.session < mid])
    second = _expectancy([t.net_return_pct for t in trades if t.session >= mid])
    adoption = list(reasons)
    if stress <= 0:
        adoption.append(f"비용 1.5배 기대값 {stress:.3f}% ≤ 0")
    if ex_best <= 0:
        adoption.append(f"최고 거래 1건 제외 기대값 {ex_best:.3f}% ≤ 0")
    if expectancy <= base_exp:
        adoption.append(f"기대값 {expectancy:.3f}% ≤ 기준선(30분 뒤 매수·종가 매도) {base_exp:.3f}%")
    if first <= 0 or second <= 0:
        adoption.append(f"앞/뒤 절반 기대값 {first:.3f}% / {second:.3f}% 중 ≤ 0")
    invalid = []
    if in_hours_pct < 80.0:
        invalid.append(f"정규장 안 분봉 {in_hours_pct:.0f}% < 80% (저장 시각대 {source_tz} 추정)")
    if len(complete) < 20:
        invalid.append(f"분봉 50% 이상 찬 세션 {len(complete)} < 20")
    verdict = "INVALID" if invalid else ("REJECT" if adoption else "PASS")
    return GateResult(
        passed=not reasons, reasons=reasons, sessions=len(all_sessions), trades=len(trades),
        total_return_pct=(equity - 1.0) * 100.0, max_drawdown_pct=max_dd,
        profit_factor=profit_factor, expectancy_pct=expectancy,
        win_rate_pct=(len(wins) / len(trades) * 100.0) if trades else 0.0,
        turnover=len(trades) * 2, overnight_trades=sum(1 for trade in trades if trade.overnight),
        verdict=verdict, source_tz=source_tz, in_hours_share_pct=in_hours_pct,
        complete_sessions=len(complete), stress_expectancy_pct=stress,
        expectancy_ex_best_pct=ex_best, baseline_trades=len(baseline),
        baseline_expectancy_pct=base_exp, first_half_expectancy_pct=first,
        second_half_expectancy_pct=second, adoption_reasons=invalid + adoption,
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
    # 수집기가 쓰는 중인 DB 다 — 읽기 전용으로 연다.
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only=ON")
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
            print(f"  사전등록 판정: {result.verdict} (저장 시각대 {result.source_tz}, 정규장 분봉 "
                  f"{result.in_hours_share_pct:.0f}%, 완전 세션 {result.complete_sessions})")
            print(f"  비용1.5배 {result.stress_expectancy_pct:.3f}% / 최고1건 제외 {result.expectancy_ex_best_pct:.3f}% / "
                  f"기준선 {result.baseline_expectancy_pct:.3f}% (n={result.baseline_trades}) / "
                  f"앞·뒤 절반 {result.first_half_expectancy_pct:.3f}% · {result.second_half_expectancy_pct:.3f}% / "
                  f"승률 {result.win_rate_pct:.0f}% / 오버나잇 {result.overnight_trades}")
            for reason in result.adoption_reasons:
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
