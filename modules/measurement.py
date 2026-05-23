"""
Measurement - Trade Journal 집계 (Phase 0)

trade_journal.json 의 거래 기록을 다음 단위로 집계한다:

 - overall       : 전체 통산 (n, win_rate, avg_pnl_pct, total_pnl, sharpe_like, profit_factor)
 - by_trigger    : stop_loss / trailing_stop / dca_stop_loss / time_cut / ...
 - by_ticker     : 종목별 알파 분리
 - by_market     : KR / US
 - by_version    : v2.2 / v2.3 (파라미터 개정 전후 비교)
 - recent        : 최근 N건의 거래 리스트 (대시보드 표시용)

모든 metric 은 0 거래에도 안전하게 동작 (NaN 대신 0 또는 None 반환).
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

from modules.trade_journal import load_journal


def _agg(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(records)
    if n == 0:
        return {
            "n": 0, "wins": 0, "losses": 0, "flats": 0,
            "win_rate": 0.0, "avg_pnl_pct": 0.0, "total_pnl": 0.0,
            "best_pnl_pct": 0.0, "worst_pnl_pct": 0.0,
            "profit_factor": None, "sharpe_like": None,
        }

    pnl_pcts = [float(r.get("pnl_pct") or 0.0) for r in records]
    pnl_totals = [float(r.get("pnl_total") or 0.0) for r in records]

    wins = sum(1 for p in pnl_pcts if p > 0)
    losses = sum(1 for p in pnl_pcts if p < 0)
    flats = n - wins - losses

    gross_win = sum(p for p in pnl_totals if p > 0)
    gross_loss = -sum(p for p in pnl_totals if p < 0)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None

    mean = sum(pnl_pcts) / n
    if n > 1:
        var = sum((p - mean) ** 2 for p in pnl_pcts) / (n - 1)
        std = math.sqrt(var)
        sharpe_like = (mean / std) if std > 0 else None
    else:
        sharpe_like = None

    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate": round(wins / n, 4),
        "avg_pnl_pct": round(mean, 4),
        "total_pnl": round(sum(pnl_totals), 2),
        "best_pnl_pct": round(max(pnl_pcts), 4),
        "worst_pnl_pct": round(min(pnl_pcts), 4),
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "sharpe_like": round(sharpe_like, 4) if sharpe_like is not None else None,
    }


def _group(records: Iterable[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        buckets[str(r.get(key) or "unknown")].append(r)
    return {k: _agg(v) for k, v in buckets.items()}


def build_metrics(records: Optional[List[Dict[str, Any]]] = None,
                  recent_limit: int = 20) -> Dict[str, Any]:
    """전체 측정 metric 을 한 번에 계산해 반환."""
    if records is None:
        records = load_journal()

    recent_sorted = sorted(
        records,
        key=lambda r: str(r.get("exit_time") or ""),
        reverse=True,
    )

    return {
        "total_trades": len(records),
        "overall": _agg(records),
        "by_trigger": _group(records, "trigger"),
        "by_ticker": _group(records, "ticker"),
        "by_market": _group(records, "market"),
        "by_version": _group(records, "version"),
        "recent": recent_sorted[:recent_limit],
    }


def format_brief(metrics: Dict[str, Any]) -> str:
    """대시보드 상단 1줄 요약용."""
    ov = metrics.get("overall", {})
    n = ov.get("n", 0)
    if n == 0:
        return "측정 데이터 없음 (거래 누적 대기 중)"
    return (
        f"누적 {n}건 | 승률 {ov['win_rate']*100:.1f}% | "
        f"평균 PnL {ov['avg_pnl_pct']:.2f}% | 누적 손익 {ov['total_pnl']:,.0f}"
    )
