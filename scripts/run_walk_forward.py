"""
Walk-Forward Backtest Driver

KIS API 또는 로컬 캐시에서 OHLC 를 로드해 v2.2 vs v2.3 파라미터 셋을 비교.

사용법:
    python -m scripts.run_walk_forward --tickers 005930,000660 --market KR --days 120
    python -m scripts.run_walk_forward --tickers TQQQ,SOXL --market US --days 120

KIS 접속이 불가하거나 OHLC 가 비어있으면 해당 ticker 는 skip 후 안내만 출력한다.
결과는 `database/walk_forward_<timestamp>.json` 에 저장된다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from modules.walk_forward import (  # noqa: E402
    PARAM_SETS,
    compare_param_sets,
    format_comparison,
    simulate_exit_rules,
)

REPORT_DIR = BASE_DIR / "database" / "walk_forward_reports"


def _normalize_ohlc(raw):
    """KIS 응답을 [{open,high,low,close}, ...] 시간 오름차순으로 정규화."""
    out = []
    if not raw:
        return out
    for row in raw:
        try:
            o = float(row.get("open") or row.get("stck_oprc") or 0)
            h = float(row.get("high") or row.get("stck_hgpr") or 0)
            l = float(row.get("low") or row.get("stck_lwpr") or 0)
            c = float(row.get("close") or row.get("stck_clpr") or 0)
            if o > 0 and h > 0 and l > 0 and c > 0:
                out.append({"open": o, "high": h, "low": l, "close": c})
        except Exception:
            continue
    # KIS daily 는 보통 최신 → 과거. 오름차순으로 뒤집기.
    if len(out) >= 2:
        out = out[::-1] if out[0].get("close") and out[-1].get("close") else out
    return out


def _fetch_ohlc_kr(ticker, days):
    try:
        from modules.kis_domestic import KisDomestic
        kis = KisDomestic()
        raw = kis.get_daily_ohlc(ticker, days=days)
        return _normalize_ohlc(raw)
    except Exception as e:
        print(f"  [WARN] KR OHLC fetch 실패 ({ticker}): {e}")
        return []


def _fetch_ohlc_us(ticker, days, exchange="NAS"):
    try:
        from modules.kis_api import KisOverseas
        kis = KisOverseas()
        raw = kis.get_daily_ohlc(ticker, exchange, days=days)
        return _normalize_ohlc(raw)
    except Exception as e:
        print(f"  [WARN] US OHLC fetch 실패 ({ticker}): {e}")
        return []


def run(tickers, market, days, exchange):
    market = market.upper()
    if market == "KR":
        sets = ("v2.2_kr_stock", "v2.3_kr_stock")
    else:
        sets = ("v2.2_us", "v2.3_us")

    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market": market,
        "days": days,
        "param_sets": {k: PARAM_SETS[k] for k in sets},
        "results": {},
    }

    for ticker in tickers:
        print(f"\n=== {ticker} ({market}, last {days}d) ===")
        if market == "KR":
            ohlc = _fetch_ohlc_kr(ticker, days)
        else:
            ohlc = _fetch_ohlc_us(ticker, days, exchange)

        if len(ohlc) < 10:
            print(f"  [SKIP] OHLC 부족 (n={len(ohlc)})")
            report["results"][ticker] = {"status": "no_data", "bars": len(ohlc)}
            continue

        comparison = compare_param_sets(ohlc, set_names=sets)
        print(format_comparison(comparison))
        report["results"][ticker] = {
            "status": "ok",
            "bars": len(ohlc),
            "metrics": comparison,
        }

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = REPORT_DIR / f"walk_forward_{market}_{stamp}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n[Saved] {out_path}")

    # 한 줄 결론 출력
    _print_verdict(report)


def _print_verdict(report):
    wins_v23 = 0
    total = 0
    for tick, info in report.get("results", {}).items():
        if info.get("status") != "ok":
            continue
        m = info.get("metrics", {})
        v22 = next((v for k, v in m.items() if k.startswith("v2.2")), None)
        v23 = next((v for k, v in m.items() if k.startswith("v2.3")), None)
        if v22 and v23 and v23.get("n") and v22.get("n"):
            total += 1
            if (v23.get("total_pnl_pct") or 0) > (v22.get("total_pnl_pct") or 0):
                wins_v23 += 1
    if total == 0:
        print("\n[Verdict] 비교 가능한 종목 없음.")
    else:
        print(f"\n[Verdict] v2.3 우세: {wins_v23}/{total} 종목")
        if wins_v23 * 2 < total:
            print("  ⚠️ v2.2 보다 v2.3 가 열위 → 파라미터 롤백 검토 권장")
        elif wins_v23 == total:
            print("  ✅ 전 종목 v2.3 우세")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", required=True, help="comma-separated tickers")
    p.add_argument("--market", default="KR", choices=["KR", "US"])
    p.add_argument("--days", type=int, default=120)
    p.add_argument("--exchange", default="NAS")
    args = p.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    try:
        run(tickers, args.market, args.days, args.exchange)
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
