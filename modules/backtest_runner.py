"""
백테스트 리포트 생성기

로컬 히스토리 데이터(database/*.json)를 기반으로 전략 성과를 주기적으로 집계합니다.
외부 API 호출 없이 동작하며 운영 서버에서 정기 실행하기 위한 용도입니다.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DB_DIR = BASE_DIR / "database"
ASSET_SNAPSHOTS_FILE = DB_DIR / "asset_snapshots.json"
PROFIT_HISTORY_FILE = DB_DIR / "profit_history.json"
REPORT_DIR = DB_DIR / "backtest_reports"
LATEST_SUMMARY_FILE = DB_DIR / "backtest_latest.json"


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _max_drawdown(equity_curve):
    peak = -math.inf
    max_dd = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak <= 0:
            continue
        dd = (peak - value) / peak
        if dd > max_dd:
            max_dd = dd
    return max_dd


def build_backtest_summary():
    snapshots = _load_json(ASSET_SNAPSHOTS_FILE, {})
    profit_history = _load_json(PROFIT_HISTORY_FILE, {})

    points = []
    for date_key in sorted(snapshots.keys()):
        total_krw = _safe_float((snapshots.get(date_key) or {}).get("total_krw"), 0.0)
        if total_krw > 0:
            points.append((date_key, total_krw))

    if len(points) < 2:
        return {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "insufficient-data",
            "message": "백테스트를 위한 스냅샷 데이터가 부족합니다(최소 2일 필요).",
            "days": len(points),
        }

    equity = [p[1] for p in points]
    returns = []
    for idx in range(1, len(points)):
        prev = points[idx - 1][1]
        curr = points[idx][1]
        if prev > 0:
            returns.append((curr - prev) / prev)

    start_value = equity[0]
    end_value = equity[-1]
    cumulative_return = (end_value - start_value) / start_value
    win_days = sum(1 for r in returns if r > 0)
    lose_days = sum(1 for r in returns if r < 0)
    flat_days = sum(1 for r in returns if r == 0)
    mean_daily = (sum(returns) / len(returns)) if returns else 0.0
    volatility = 0.0
    if len(returns) > 1:
        variance = sum((r - mean_daily) ** 2 for r in returns) / (len(returns) - 1)
        volatility = math.sqrt(variance)

    sharpe = 0.0
    if volatility > 0:
        sharpe = (mean_daily / volatility) * math.sqrt(252)

    mdd = _max_drawdown(equity)

    rolling_30 = None
    if len(points) >= 31:
        rolling_30 = (equity[-1] - equity[-31]) / equity[-31] if equity[-31] > 0 else None

    rolling_90 = None
    if len(points) >= 91:
        rolling_90 = (equity[-1] - equity[-91]) / equity[-91] if equity[-91] > 0 else None

    realized_total = 0.0
    total_trade_count = 0
    for _, row in profit_history.items():
        realized_total += _safe_float((row or {}).get("realized_profit"), 0.0)
        total_trade_count += len((row or {}).get("trades", []))

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "ok",
        "period": {
            "start": points[0][0],
            "end": points[-1][0],
            "days": len(points),
        },
        "performance": {
            "start_value_krw": round(start_value, 2),
            "end_value_krw": round(end_value, 2),
            "cumulative_return": cumulative_return,
            "max_drawdown": mdd,
            "mean_daily_return": mean_daily,
            "daily_volatility": volatility,
            "annualized_sharpe": sharpe,
            "rolling_30d_return": rolling_30,
            "rolling_90d_return": rolling_90,
        },
        "distribution": {
            "win_days": win_days,
            "lose_days": lose_days,
            "flat_days": flat_days,
            "win_rate": (win_days / len(returns)) if returns else 0.0,
        },
        "trades": {
            "realized_profit_total": round(realized_total, 2),
            "trade_count": total_trade_count,
        },
    }


def _to_pct(value):
    if value is None:
        return "N/A"
    return f"{value * 100:.2f}%"


def _build_markdown(summary):
    if summary.get("status") != "ok":
        return (
            "# US ETF Sniper Backtest Report\n\n"
            f"- Generated: {summary.get('generated_at')}\n"
            f"- Status: {summary.get('status')}\n"
            f"- Message: {summary.get('message')}\n"
        )

    perf = summary["performance"]
    dist = summary["distribution"]
    period = summary["period"]
    trades = summary["trades"]

    return f"""# US ETF Sniper Backtest Report

- Generated: {summary['generated_at']}
- Period: {period['start']} ~ {period['end']} ({period['days']} days)

## Performance

| Metric | Value |
|------|------:|
| Start Value (KRW) | {perf['start_value_krw']:,.0f} |
| End Value (KRW) | {perf['end_value_krw']:,.0f} |
| Cumulative Return | {_to_pct(perf['cumulative_return'])} |
| Max Drawdown | {_to_pct(perf['max_drawdown'])} |
| Mean Daily Return | {_to_pct(perf['mean_daily_return'])} |
| Daily Volatility | {_to_pct(perf['daily_volatility'])} |
| Annualized Sharpe | {perf['annualized_sharpe']:.2f} |
| 30D Return | {_to_pct(perf['rolling_30d_return'])} |
| 90D Return | {_to_pct(perf['rolling_90d_return'])} |

## Return Distribution

| Metric | Value |
|------|------:|
| Win Days | {dist['win_days']} |
| Lose Days | {dist['lose_days']} |
| Flat Days | {dist['flat_days']} |
| Win Rate | {_to_pct(dist['win_rate'])} |

## Trade Summary

| Metric | Value |
|------|------:|
| Total Realized Profit | {trades['realized_profit_total']:,.0f} |
| Trade Count | {trades['trade_count']} |

## Notes

- This report is generated from local history files in `database/`.
- No external market data API is called during backtest reporting.
"""


def main():
    summary = build_backtest_summary()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_DIR / f"backtest_{stamp}.md"

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(_build_markdown(summary))

    with open(LATEST_SUMMARY_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"[Backtest] status={summary.get('status')} generated={summary.get('generated_at')}")
    print(f"[Backtest] report={report_path}")
    print(f"[Backtest] summary={LATEST_SUMMARY_FILE}")


if __name__ == "__main__":
    main()
