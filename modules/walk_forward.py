"""
Walk-Forward Exit-Rule Simulator (Phase 0)

목적: v2.2 파라미터 셋과 v2.3 파라미터 셋을 동일한 과거 OHLC 시퀀스 위에서
청산 규칙(Stop Loss / Trailing Stop / Time Cut) 만 갈아끼우며 시뮬레이션해
어느 쪽이 더 나은 성과를 보이는지 비교하기 위한 순수 함수.

설계 원칙:
 - **외부 의존성 없음** (pandas/yfinance 불필요) → unittest 가능
 - 진입 시그널은 단순화: "각 일자 시가 매수, 청산 규칙으로 빠짐"
   (목적은 진입 알고리즘이 아니라 청산 파라미터 비교)
 - 청산 규칙 우선순위: Stop Loss > Trailing Stop > Time Cut

PARAM_SETS:
 - v2.2_us, v2.3_us, v2.2_kr_stock, v2.3_kr_stock 4종
   (`run_bot.py` 의 상수와 일치)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence


# === 비교 대상 파라미터 셋 ===
# partial_tp_ratio: 0.0 = 단일 청산 (기존), 0.5 = trailing 활성가에서 50% 청산
PARAM_SETS: Dict[str, Dict[str, float]] = {
    "v2.2_us": {
        "stop_loss_pct": -3.0,
        "trailing_activation_pct": 3.0,
        "trailing_drop_pct": 1.5,
        "time_cut_days": 5,
        "partial_tp_ratio": 0.0,
    },
    "v2.3_us": {
        "stop_loss_pct": -5.0,
        "trailing_activation_pct": 5.0,
        "trailing_drop_pct": 3.0,
        "time_cut_days": 5,
        "partial_tp_ratio": 0.0,
    },
    "v2.4_us": {
        "stop_loss_pct": -5.0,
        "trailing_activation_pct": 5.0,
        "trailing_drop_pct": 3.0,
        "time_cut_days": 5,
        "partial_tp_ratio": 0.5,
    },
    "v2.2_kr_stock": {
        "stop_loss_pct": -5.0,
        "trailing_activation_pct": 5.0,
        "trailing_drop_pct": 2.5,
        "time_cut_days": 5,
        "partial_tp_ratio": 0.0,
    },
    "v2.3_kr_stock": {
        "stop_loss_pct": -7.0,
        "trailing_activation_pct": 7.0,
        "trailing_drop_pct": 4.0,
        "time_cut_days": 5,
        "partial_tp_ratio": 0.0,
    },
    "v2.4_kr_stock": {
        "stop_loss_pct": -7.0,
        "trailing_activation_pct": 7.0,
        "trailing_drop_pct": 4.0,
        "time_cut_days": 5,
        "partial_tp_ratio": 0.5,
    },
    # === v2.5: breakeven stop + slippage/fee 모델 ===
    # breakeven_trigger_pct: 이 % 도달 이후 하락시 매수가에서 청산(손실 전환 방지)
    # slippage_pct: 청산가 하향 조정(%), fee_pct: 양방향 수수료(%)
    "v2.5_us": {
        "stop_loss_pct": -5.0,
        "trailing_activation_pct": 5.0,
        "trailing_drop_pct": 3.0,
        "time_cut_days": 5,
        "partial_tp_ratio": 0.5,
        "breakeven_trigger_pct": 3.0,
        "breakeven_buffer_pct": 0.2,
        "slippage_pct": 0.05,
        "fee_pct": 0.015,
    },
    "v2.5_kr_stock": {
        "stop_loss_pct": -7.0,
        "trailing_activation_pct": 7.0,
        "trailing_drop_pct": 4.0,
        "time_cut_days": 5,
        "partial_tp_ratio": 0.5,
        "breakeven_trigger_pct": 4.0,
        "breakeven_buffer_pct": 0.2,
        "slippage_pct": 0.05,
        "fee_pct": 0.015,
    },
}


@dataclass
class Trade:
    entry_idx: int
    entry_price: float
    exit_idx: int
    exit_price: float
    pnl_pct: float
    trigger: str  # 'stop_loss' | 'trailing_stop' | 'time_cut' | 'end_of_series'


@dataclass
class SimResult:
    params_name: str
    trades: List[Trade] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.trades)

    def metrics(self) -> Dict[str, float]:
        if not self.trades:
            return {"n": 0, "win_rate": 0.0, "avg_pnl_pct": 0.0,
                    "total_pnl_pct": 0.0, "mdd_pct": 0.0,
                    "profit_factor": None, "sharpe_like": None}

        pnls = [t.pnl_pct for t in self.trades]
        wins = sum(1 for p in pnls if p > 0)
        gross_win = sum(p for p in pnls if p > 0)
        gross_loss = -sum(p for p in pnls if p < 0)
        pf = (gross_win / gross_loss) if gross_loss > 0 else None

        mean = sum(pnls) / len(pnls)
        var = sum((p - mean) ** 2 for p in pnls) / max(1, len(pnls) - 1)
        std = math.sqrt(var)
        sharpe_like = (mean / std) if std > 0 else None

        # 누적 손익 곡선의 MDD (단순 산술 합)
        cum = 0.0
        peak = 0.0
        mdd = 0.0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            dd = peak - cum
            if dd > mdd:
                mdd = dd

        return {
            "n": len(pnls),
            "win_rate": round(wins / len(pnls), 4),
            "avg_pnl_pct": round(mean, 4),
            "total_pnl_pct": round(sum(pnls), 4),
            "mdd_pct": round(mdd, 4),
            "profit_factor": round(pf, 4) if pf is not None else None,
            "sharpe_like": round(sharpe_like, 4) if sharpe_like is not None else None,
        }


def _simulate_position(
    ohlc: Sequence[Dict[str, float]],
    entry_idx: int,
    params: Dict[str, float],
) -> Optional[Trade]:
    """단일 포지션 시뮬레이션 (entry_idx 시점 매수 후 청산까지).

    partial_tp_ratio > 0 이면 trailing_activation 도달 시 해당 비율만큼 1차 청산한
    것으로 가정하고, 나머지는 trailing_stop 으로 계속 추적. 최종 pnl_pct 는
    두 청산의 가중 평균 변화율로 반환한다.
    """
    if entry_idx >= len(ohlc):
        return None

    entry_price = float(ohlc[entry_idx]["open"])
    if entry_price <= 0:
        return None

    stop_loss_pct = params["stop_loss_pct"]
    trail_act = params["trailing_activation_pct"]
    trail_drop = params["trailing_drop_pct"]
    time_cut_days = int(params["time_cut_days"])
    partial_tp_ratio = float(params.get("partial_tp_ratio", 0.0) or 0.0)
    # v2.5 삽입 파라미터 (미설정 시 0 → 원래 동작)
    be_trigger = float(params.get("breakeven_trigger_pct", 0.0) or 0.0)
    be_buffer = float(params.get("breakeven_buffer_pct", 0.0) or 0.0)
    slippage_pct = float(params.get("slippage_pct", 0.0) or 0.0)
    fee_pct = float(params.get("fee_pct", 0.0) or 0.0)

    highest = entry_price
    trail_active = False
    partial_done = False
    partial_exit_price = 0.0
    breakeven_armed = False
    breakeven_price = 0.0

    last_idx = min(len(ohlc) - 1, entry_idx + time_cut_days)

    def _apply_costs(raw_pnl_pct: float) -> float:
        # 수수료는 양방향(매수+매도) 2회 차감, 슬리피지는 청산에만 1회 적용
        return raw_pnl_pct - (2.0 * fee_pct) - slippage_pct

    def _finalize(exit_idx: int, exit_price: float, trigger: str) -> Trade:
        if partial_done and partial_tp_ratio > 0:
            r1 = (partial_exit_price - entry_price) / entry_price * 100.0
            r2 = (exit_price - entry_price) / entry_price * 100.0
            blended = partial_tp_ratio * r1 + (1.0 - partial_tp_ratio) * r2
            blended = _apply_costs(blended)
            return Trade(entry_idx, entry_price, exit_idx, exit_price,
                         round(blended, 4), trigger + "+partial")
        pnl = (exit_price - entry_price) / entry_price * 100.0
        pnl = _apply_costs(pnl)
        return Trade(entry_idx, entry_price, exit_idx, exit_price,
                     round(pnl, 4), trigger)

    for i in range(entry_idx, last_idx + 1):
        bar = ohlc[i]
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])

        # 1) Stop Loss — 일중 저가 기준
        low_pnl = (low - entry_price) / entry_price * 100.0
        if low_pnl <= stop_loss_pct:
            sl_price = entry_price * (1.0 + stop_loss_pct / 100.0)
            return _finalize(i, sl_price, "stop_loss")

        # 1-A) Breakeven Stop — v2.5
        if be_trigger > 0 and breakeven_armed and low <= breakeven_price:
            return _finalize(i, breakeven_price, "breakeven_stop")

        # 2) Trailing 활성화 조건 체크
        peak_pnl = (high - entry_price) / entry_price * 100.0

        # 1-B) Breakeven 아밍 (고점이 trigger 도달 시)
        if be_trigger > 0 and not breakeven_armed and peak_pnl >= be_trigger:
            breakeven_armed = True
            breakeven_price = entry_price * (1.0 + be_buffer / 100.0)

        if not trail_active and peak_pnl >= trail_act:
            trail_active = True
            highest = max(highest, high)
            if partial_tp_ratio > 0 and not partial_done:
                partial_done = True
                partial_exit_price = entry_price * (1.0 + trail_act / 100.0)

        if trail_active:
            highest = max(highest, high)
            trail_trigger_price = highest * (1.0 - trail_drop / 100.0)
            if low <= trail_trigger_price:
                return _finalize(i, trail_trigger_price, "trailing_stop")

    # 3) Time Cut — 마지막 바 종가 청산
    last_close = float(ohlc[last_idx]["close"])
    trig = "time_cut" if last_idx < len(ohlc) - 1 else "end_of_series"
    return _finalize(last_idx, last_close, trig)


def simulate_exit_rules(
    ohlc: Sequence[Dict[str, float]],
    params: Dict[str, float],
    params_name: str = "custom",
    entry_spacing: int = 1,
) -> SimResult:
    """OHLC 시계열에서 entry_spacing 일마다 매수, 청산 규칙으로 빠지는 시뮬.

    Args:
        ohlc: [{open, high, low, close}, ...] 시간 오름차순
        params: PARAM_SETS 의 한 항목
        entry_spacing: 진입 간격 (1이면 매일 새 포지션)
    """
    result = SimResult(params_name=params_name)
    i = 0
    while i < len(ohlc):
        trade = _simulate_position(ohlc, i, params)
        if trade is None:
            break
        result.trades.append(trade)
        # 청산 직후 다음 진입 후보로 이동 (중첩 포지션 방지)
        next_start = max(trade.exit_idx + 1, i + entry_spacing)
        i = next_start
    return result


def compare_param_sets(
    ohlc: Sequence[Dict[str, float]],
    set_names: Sequence[str] = ("v2.2_us", "v2.3_us"),
) -> Dict[str, Dict[str, float]]:
    """여러 파라미터 셋을 동일 OHLC 에 돌려 metric 비교."""
    out = {}
    for name in set_names:
        params = PARAM_SETS[name]
        res = simulate_exit_rules(ohlc, params, params_name=name)
        out[name] = res.metrics()
    return out


def format_comparison(comparison: Dict[str, Dict[str, float]]) -> str:
    """CLI/리포트 출력용 표 문자열."""
    lines = []
    header = f"{'set':<20} {'n':>5} {'win%':>7} {'avg%':>8} {'cum%':>8} {'mdd%':>8} {'pf':>6} {'sharpe':>8}"
    lines.append(header)
    lines.append("-" * len(header))
    for name, m in comparison.items():
        pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] is not None else "  -  "
        sh = f"{m['sharpe_like']:.2f}" if m['sharpe_like'] is not None else "  -  "
        lines.append(
            f"{name:<20} {m['n']:>5} {m['win_rate']*100:>6.1f} "
            f"{m['avg_pnl_pct']:>7.2f} {m['total_pnl_pct']:>7.2f} "
            f"{m['mdd_pct']:>7.2f} {pf:>6} {sh:>8}"
        )
    return "\n".join(lines)
