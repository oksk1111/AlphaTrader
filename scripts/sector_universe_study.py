#!/usr/bin/env python3
"""KR 업종 커버리지 유니버스 확장 검증 (2026-10-30).

질문: 현재 KR 로테이션 유니버스(17종)에는 화장품·소비재·금융·산업재 ETF 가 없어서, 그 업종에
추세가 생겨도 원리적으로 편입할 수 없다. KRX 업종별 대표 ETF 를 넣으면 나아지는가?

AGENTS v8.0 규칙 9 — 후보와 채택 규칙을 **결과를 보기 전에** 정한다. 이 docstring 이 그 기록이다.

후보 선정 규칙 (수익률을 보지 않고 정함)
  - 현 유니버스가 덮지 않는 KRX 업종마다 ETF 1개.
  - 같은 업종이면 상장일이 가장 이른 국내 업종지수 ETF (레버리지·인버스 제외) — 검증 이력 최대화.
  - 140950 은 보험이 아니라 POWER K100 이라 제외. 바이오(244580)는 헬스케어(143860)와 겹쳐 제외.

채택 규칙 (전부 충족해야 채택, 아니면 기각)
  1. test 2022- Sharpe: 확장 ≥ 현행 + 0.05   (+0.02 는 잡음 — AGENTS v8.0 규칙 9)
  2. 시작연도 2017~2022 6개 중 최저 Sharpe: 확장 ≥ 현행
  3. test 2022- MDD: 현행보다 3%p 넘게 나빠지지 않음
  4. 연 회전율(test 2022-): 현행의 1.5배 이하

공통 조건: 운영설정(user_config.json momentum_rotation.kr — 슬롯·버퍼·리밸런싱·시장필터·종가손절·
방어자산)을 그대로 읽는다. 모든 시계열은 2026-09-30 에서 자른다(기존 캐시 종료일). 데이터는 yfinance
(개발 PC 에서 토스 API 를 부르면 서버 봇 토큰이 끊긴다 — AGENTS v8.0 규칙 2).

    python scripts/sector_universe_study.py
    python scripts/sector_universe_study.py --point-in-time

--point-in-time (2차, 2026-10-30, 1차 결과를 본 뒤 사전 등록)
  1차는 기각됐지만 비교가 현행에 유리하게 기울어 있다: 현행 17종 중 14종이 2018~2026 상장이고
  2022 이후 오른 테마(AI·방산 등)를 2026 년에 고른 목록이다. 그래서 양쪽 모두 "2017-01-01 이전에
  상장돼 있던 ETF" 로만 제한해 사후 선택 편향을 같게 맞춘다 (판정은 첫 거래일만 본다, 수익률 아님).
    A = (현행 유니버스 ∪ backtest_real.KR_ETFS) 중 2017 이전 상장  → 069500·091160·133690
    B = A + SECTOR_ETFS 중 2017 이전 상장 (266410·266390 은 2017-03 상장이라 제외) → 15종
  채택 규칙은 1차와 같은 4개. 판정의 의미도 미리 정한다:
    B 통과 → 업종 커버리지가 구조적으로 낫다 → 운영 유니버스를 현행 17 + SECTOR_ETFS 14 로 확장 제안
    B 실패 → 현행 유지
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import backtest_real as bt  # noqa: E402  (scripts/ 는 패키지가 아니다)

SECTOR_ETFS = {
    "228790": "TIGER 화장품",
    "266410": "KODEX 필수소비재",
    "266390": "KODEX 경기소비재",
    "091180": "KODEX 자동차",
    "143860": "TIGER 헬스케어",
    "091170": "KODEX 은행",
    "102970": "KODEX 증권",
    "117700": "KODEX 건설",
    "117680": "KODEX 철강",
    "117460": "KODEX 에너지화학",
    "102960": "KODEX 기계장비",
    "140710": "KODEX 운송",
    "139230": "TIGER 200 중공업",
    "228810": "TIGER 미디어컨텐츠",
}
END = "2026-10-01"          # 2026-09-30 까지 포함
CAPITAL = 2_700_000.0
WINDOWS = [("train 2017-21", "2017-01-01", "2022-01-01"), ("test 2022-", "2022-01-01", END)]
WINDOWS += [(f"start {y}", f"{y}-01-01", END) for y in range(2017, 2023)]


def _holdings_share(data, universe, code, start, end):
    """확장 유니버스에서 code 를 보유한 시점의 비율(%, 근사) — 정보용, 채택 판단에 쓰지 않는다."""
    seen = {"days": 0, "held": 0}
    orig = bt.Book.equity

    def spy(self, px):
        seen["days"] += 1
        if code in self.pos:
            seen["held"] += 1
        return orig(self, px)

    bt.Book.equity = spy
    try:
        bt.run_operational(data, "KR", start, CAPITAL, universe, end=end)
    finally:
        bt.Book.equity = orig
    return 100.0 * seen["held"] / max(1, seen["days"])


def _listed_before(data, codes, cutoff="2017-01-01"):
    import pandas as pd
    return [c for c in codes if c in data and data[c].index[0] < pd.Timestamp(cutoff)]


def main() -> int:
    point_in_time = "--point-in-time" in sys.argv[1:]
    base = list(dict.fromkeys(bt._operational_universe("KR") + list(bt.KR_ETFS)))
    rc = bt._load_rotation("KR")
    data = bt.load_prices(base + list(SECTOR_ETFS) + rc.get("defensive", []), "KR", "2014-01-01",
                          source="yfinance")
    base = [c for c in base if c in data]
    expanded = base + [c for c in SECTOR_ETFS if c in data and c not in base]
    if point_in_time:
        base = _listed_before(data, base)
        expanded = base + [c for c in _listed_before(data, list(SECTOR_ETFS)) if c not in base]
        print(f"[point-in-time] A={base}")
    missing = [c for c in SECTOR_ETFS if c not in data]
    print(f"{'A' if point_in_time else '현행'} {len(base)}종 / {'B' if point_in_time else '확장'} {len(expanded)}종" + (f" (데이터 없음: {missing})" if missing else ""))

    rows = {}
    print(f"\n{'구간':<16}{'현행 CAGR':>10}{'MDD':>8}{'Sharpe':>8}{'회전':>6}   "
          f"{'확장 CAGR':>10}{'MDD':>8}{'Sharpe':>8}{'회전':>6}")
    for name, s, e in WINDOWS:
        a = bt.run_operational(data, "KR", s, CAPITAL, base, end=e)
        b = bt.run_operational(data, "KR", s, CAPITAL, expanded, end=e)
        rows[name] = (a, b)
        print(f"{name:<16}{a['cagr_pct']:>9.1f}%{a['mdd_pct']:>7.1f}%{a['sharpe']:>8.2f}"
              f"{a['turnover_x_per_year']:>6.1f}   "
              f"{b['cagr_pct']:>9.1f}%{b['mdd_pct']:>7.1f}%{b['sharpe']:>8.2f}{b['turnover_x_per_year']:>6.1f}")

    ta, tb = rows["test 2022-"]
    starts = [n for n, _, _ in WINDOWS if n.startswith("start")]
    min_a = min(rows[n][0]["sharpe"] for n in starts)
    min_b = min(rows[n][1]["sharpe"] for n in starts)
    checks = [
        ("1. test Sharpe 확장 ≥ 현행+0.05", tb["sharpe"] >= ta["sharpe"] + 0.05,
         f"{tb['sharpe']:.2f} vs {ta['sharpe']:.2f}+0.05"),
        ("2. 시작연도 최저 Sharpe 확장 ≥ 현행", min_b >= min_a, f"{min_b:.2f} vs {min_a:.2f}"),
        ("3. test MDD 악화 ≤ 3%p", tb["mdd_pct"] >= ta["mdd_pct"] - 3.0,
         f"{tb['mdd_pct']:.1f}% vs {ta['mdd_pct']:.1f}%"),
        ("4. test 회전 ≤ 1.5배", tb["turnover_x_per_year"] <= ta["turnover_x_per_year"] * 1.5,
         f"{tb['turnover_x_per_year']:.1f} vs {ta['turnover_x_per_year']:.1f}×1.5"),
    ]
    print("\n채택 규칙 (사전 등록)")
    for label, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}  ({detail})")
    adopted = all(ok for _, ok, _ in checks)
    print(f"\n판정: {'채택' if adopted else '기각'}")

    share = _holdings_share(data, expanded, "228790", "2017-01-01", END)
    print(f"(정보) 확장 유니버스 2017-2026 중 TIGER 화장품 보유일 비율: {share:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
