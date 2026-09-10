"""
scripts/verify_strategy_loop.py — v6.0 전략 검증 루프 (Loop Engineering)

[목적]

"몇 달째 거래가 안 된다"를 **배포 전에** 잡아내는 장치. 이 프로젝트의 실패는
전략이 손실을 낸 게 아니라 전략이 **아무것도 하지 않는데 아무도 몰랐다**는 것이다.
단위 테스트는 함수 하나의 옳고 그름은 보지만, "1년치 장을 돌렸을 때 실제로 체결이
일어나는가"는 보지 못한다. 그 간극을 메우는 게 이 스크립트다.

[무엇을 하는가]

여러 시장 국면(상승/횡보/조정/폭락/V자반등)을 합성 가격으로 생성하고, 각 국면에서
장중 재평가 주기까지 포함해 세션을 시뮬레이션한 뒤 다음을 검증한다:

  1. 체결률(fill rate)  — 국면별로 매수가 실제로 발생하는가
  2. 무매수 연속(dry streak) — 어떤 국면에서도 비정상적으로 길게 멈추지 않는가
  3. 사이즈 단조성       — 나쁜 국면일수록 작게 사는가
  4. 안전성             — 폭락장에서는 확실히 멈추는가

실행:
    python scripts/verify_strategy_loop.py
    python scripts/verify_strategy_loop.py --sessions 120 --seed 7

종료코드 0 = 합격. 1 = 실패(회귀). CI 에 그대로 물릴 수 있다.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from dataclasses import dataclass
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.decision_engine import (  # noqa: E402
    TickerContext, PortfolioContext, evaluate_buy, MIN_BUY_SCORE,
)

BARS_PER_SESSION = 13          # 장중 재평가 횟수 (6.5시간 / 30분)
WARMUP_DAYS = 60               # MA60 계산용


# ---------------------------------------------------------------------------
# 시장 국면 생성기
# ---------------------------------------------------------------------------
@dataclass
class Regime:
    name: str
    drift: float          # 일간 기대수익률
    vol: float            # 일간 변동성
    # 판정 기준은 '체결률'이 아니라 '평균 노출도(포지션/자산)' 다.
    # 상승장에서 체결이 뜸한 것은 정상일 수 있다 — 이미 풀투자 상태이기 때문이다.
    # 반대로 폭락장에서 체결이 잦은 것은 위험하다 — 손절→재진입을 반복한다는 뜻.
    # 따라서 "얼마나 자주 샀나"가 아니라 "결과적으로 얼마나 실려 있나"를 본다.
    min_exposure: float   # 이 국면에서 최소 이만큼은 투자돼 있어야 한다
    max_exposure: float   # 이 이상 실려 있으면 리스크 관리 실패
    max_dry_streak: int = 10  # 미실현 상태에서 허용되는 최대 연속 무매수 세션
    # [v6.1] 청산 규칙 품질 게이트. None 이면 그 국면에서는 검사하지 않는다.
    min_capture: float = None        # 같은 노출로 보유했을 때 대비 최소 수익 비율
    max_churn_drag_pp: float = None  # 그냥 보유 대비 허용 초과손실 (%p, 음수)


REGIMES = [
    # min_capture / max_churn_drag_pp 는 v6.1 에서 추가된 청산 품질 게이트다.
    # 상승 국면에서는 "번 것을 얼마나 지켰나"(capture), 하락 국면에서는
    # "그냥 들고 있는 것보다 얼마나 더 잃었나"(churn_drag) 를 본다.
    Regime("강한 상승장",   +0.0035, 0.011, min_exposure=0.10, max_exposure=1.00, max_dry_streak=8,
           min_capture=0.55, max_churn_drag_pp=-12.0),
    Regime("완만한 상승",   +0.0012, 0.013, min_exposure=0.08, max_exposure=1.00, max_dry_streak=10,
           min_capture=0.40, max_churn_drag_pp=-12.0),
    Regime("횡보장",        +0.0000, 0.014, min_exposure=0.05, max_exposure=0.80, max_dry_streak=12,
           max_churn_drag_pp=-12.0),
    Regime("조정장(-15%)",  -0.0025, 0.018, min_exposure=0.00, max_exposure=0.55, max_dry_streak=999,
           max_churn_drag_pp=-12.0),
    Regime("폭락장(-40%)",  -0.0090, 0.035, min_exposure=0.00, max_exposure=0.35, max_dry_streak=999,
           max_churn_drag_pp=-15.0),
]


def make_series(regime: Regime, n: int, rng: random.Random, start: float = 100.0) -> List[float]:
    prices, p = [], start
    for _ in range(n):
        p *= math.exp(regime.drift + rng.gauss(0, regime.vol))
        prices.append(round(p, 4))
    return prices


def ma(vals: List[float], w: int):
    return sum(vals[-w:]) / w if len(vals) >= w else None


# ---------------------------------------------------------------------------
# 포지션 / 청산 모델
#
# 매수만 시뮬레이션하면 포지션이 노출 한도까지 차오른 뒤 영원히 veto 가 나서
# "전략이 멈췄다"는 거짓 실패가 뜬다. 실제 봇은 손절·트레일링으로 포지션을
# 회전시키므로, 검증 루프도 청산을 함께 모델링해야 의미 있는 신호가 나온다.
# 값은 user_config.json 의 risk_management 와 동일하게 맞춘다.
# ---------------------------------------------------------------------------
# [v6.1] 값을 여기 하드코딩하지 않고 user_config.json 에서 읽는다.
# v6.0 은 이 세 값을 스크립트에 복사해 뒀는데, 그러면 운영 설정을 바꿔도 검증
# 루프는 옛날 값으로 통과해 버린다 — 검증이 검증하지 않는 상태가 된다.
def _load_risk_config() -> Dict:
    cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "user_config.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return (json.load(f).get("risk_management") or {})
    except Exception:
        return {}


_RISK = _load_risk_config()
STOP_LOSS_PCT = float(_RISK.get("stop_loss_pct", -7.0))
TRAIL_ACTIVATE_PCT = float(_RISK.get("trailing_stop_activation_pct", 5.0))
TRAIL_DROP_PCT = float(_RISK.get("trailing_stop_drop_pct", 3.5))


@dataclass
class Portfolio:
    cash: float = 100_000.0
    qty: int = 0
    avg_price: float = 0.0
    highest: float = 0.0
    max_position_value: float = 25_000.0

    def position_value(self, price: float) -> float:
        return self.qty * price

    def equity(self, price: float) -> float:
        return self.cash + self.position_value(price)

    def buy(self, price: float, spend: float) -> int:
        qty = int(min(spend, self.cash) / price)
        if qty <= 0:
            return 0
        new_qty = self.qty + qty
        self.avg_price = ((self.avg_price * self.qty) + price * qty) / new_qty
        self.qty = new_qty
        self.cash -= qty * price
        self.highest = max(self.highest, price)
        return qty

    def sell_all(self, price: float) -> float:
        proceeds = self.qty * price
        self.cash += proceeds
        self.qty = 0
        self.avg_price = 0.0
        self.highest = 0.0
        return proceeds

    def check_exit(self, price: float):
        """손절/트레일링 판정. (청산여부, 사유) 반환."""
        if self.qty <= 0 or self.avg_price <= 0:
            return False, ""
        self.highest = max(self.highest, price)
        pnl = (price - self.avg_price) / self.avg_price * 100.0
        if pnl <= STOP_LOSS_PCT:
            return True, "손절"
        peak_gain = (self.highest - self.avg_price) / self.avg_price * 100.0
        if peak_gain >= TRAIL_ACTIVATE_PCT:
            drop = (self.highest - price) / self.highest * 100.0
            if drop >= TRAIL_DROP_PCT:
                return True, "트레일링"
        return False, ""


# ---------------------------------------------------------------------------
# 세션 시뮬레이터
# ---------------------------------------------------------------------------
@dataclass
class SessionResult:
    filled: bool = False
    buys: int = 0
    sells: int = 0
    size_mults: List[float] = None
    actions: Dict[str, int] = None
    exposure: List[float] = None

    def __post_init__(self):
        if self.size_mults is None:
            self.size_mults = []
        if self.actions is None:
            self.actions = {"buy": 0, "defer": 0, "veto": 0}
        if self.exposure is None:
            self.exposure = []


def simulate_session(closes: List[float], day_open: float, rng: random.Random,
                     pf: Portfolio, is_leveraged: bool = False) -> SessionResult:
    """하루치 세션을 장중 재평가 주기 단위로 시뮬레이션한다.

    v5.1 이었다면 이 루프는 첫 bar 한 번만 돌고 끝났다 — 그게 바로 버그였다.
    v6.0 은 BARS_PER_SESSION 번 재평가하므로, 개장 때 조건이 나빴어도 장중에
    좋아지면 같은 세션 안에서 매수할 수 있다.
    """
    prev_close = closes[-1]
    intraday_vol = 0.006
    price = day_open
    day_low = day_high = day_open

    res = SessionResult()
    buys_this_session = 0
    stopped_out_today = False   # 실제 봇의 STOPPED_OUT_TODAY 휩쏘 가드

    for bar in range(BARS_PER_SESSION):
        price = max(0.01, price * math.exp(rng.gauss(0, intraday_vol)))
        day_low = min(day_low, price)
        day_high = max(day_high, price)

        # --- 1) 리스크 관리(청산) 먼저 ---
        exit_now, _reason = pf.check_exit(price)
        if exit_now:
            pf.sell_all(price)
            res.sells += 1
            stopped_out_today = True   # 당일 재매수 금지 (v5.1 휩쏘 가드)
            res.exposure.append(0.0)
            continue
        res.exposure.append(pf.position_value(price) / max(1e-9, pf.equity(price)))

        # --- 2) 매수 판단 ---
        hist = closes + [price]
        ctx = TickerContext(
            ticker="SIM", market="US", price=price,
            ma5=ma(hist, 5), ma10=ma(hist, 10), ma20=ma(hist, 20), ma60=ma(hist, 60),
            prev_close=prev_close, day_low=day_low, day_high=day_high,
            gap_pct=(day_open - prev_close) / prev_close * 100.0,
            consec_decline_pct=(max(0.0, (closes[-4] - price) / closes[-4] * 100.0)
                                if len(closes) >= 4 else 0.0),
            is_leveraged=is_leveraged,
            holding_qty=pf.qty, holding_avg_price=pf.avg_price,
            position_value=pf.position_value(price),
            max_position_value=pf.max_position_value,
            buys_this_session=buys_this_session, max_buys_per_session=10,
            stopped_out_today=stopped_out_today,
        )
        pctx = PortfolioContext(
            available_cash=pf.cash,
            minutes_since_open=bar * 30.0,
            minutes_to_close=(BARS_PER_SESSION - bar) * 30.0,
        )

        d = evaluate_buy(ctx, pctx)
        res.actions[d.action] += 1

        if d.action == "buy":
            spend = pf.cash * 0.05 * d.size_multiplier
            if pf.buy(price, spend) > 0:
                res.buys += 1
                buys_this_session += 1
                res.size_mults.append(d.size_multiplier)

    res.filled = res.buys > 0
    return res


def run_regime(regime: Regime, sessions: int, seed: int) -> Tuple[Dict, List[str]]:
    rng = random.Random(seed)
    closes = make_series(Regime("warmup", 0.0005, 0.012, 0, 1), WARMUP_DAYS, rng)

    pf = Portfolio()
    start_equity = pf.equity(closes[-1])

    filled_sessions = 0
    total_buys = total_sells = 0
    size_samples: List[float] = []
    exposure_samples: List[float] = []
    dry_streak = max_dry = 0
    action_totals = {"buy": 0, "defer": 0, "veto": 0}

    for _ in range(sessions):
        day_open = closes[-1] * math.exp(rng.gauss(0, regime.vol * 0.4))
        res = simulate_session(closes, day_open, rng, pf)

        for k, v in res.actions.items():
            action_totals[k] += v
        total_buys += res.buys
        total_sells += res.sells
        size_samples.extend(res.size_mults)
        exposure_samples.extend(res.exposure)

        # 무매수 연속은 '살 수 있었는데 안 산' 세션만 센다. 이미 노출 한도까지
        # 차 있어 못 산 세션을 무매수로 세면 상승장에서 거짓 경보가 난다.
        session_exposure = (sum(res.exposure) / len(res.exposure)) if res.exposure else 0.0
        saturated = session_exposure >= 0.9 * (pf.max_position_value / max(1e-9, pf.equity(closes[-1])))
        if res.filled:
            filled_sessions += 1
            dry_streak = 0
        elif saturated:
            dry_streak = 0   # 풀투자 상태 — 안 사는 게 정상
        else:
            dry_streak += 1
            max_dry = max(max_dry, dry_streak)

        closes.append(round(closes[-1] * math.exp(regime.drift + rng.gauss(0, regime.vol)), 4))

    final_price = closes[-1]
    stats = {
        "regime": regime.name,
        "fill_rate": filled_sessions / sessions,
        "avg_exposure": (sum(exposure_samples) / len(exposure_samples)) if exposure_samples else 0.0,
        "total_buys": total_buys,
        "total_sells": total_sells,
        "max_dry_streak": max_dry,
        "avg_size": (sum(size_samples) / len(size_samples)) if size_samples else 0.0,
        "actions": action_totals,
        "equity_change_pct": (pf.equity(final_price) / start_equity - 1.0) * 100.0,
        "buy_hold_pct": (final_price / closes[WARMUP_DAYS - 1] - 1.0) * 100.0,
    }

    # ------------------------------------------------------------------
    # [v6.1] 노출도만 보면 청산 규칙의 결함이 보이지 않는다.
    #
    # v6.0 의 판정은 "노출도가 국면별로 적절한가" 뿐이었다. 그런데 5개 시드를
    # 돌려보면 모든 국면에서 노출도가 22~25% 로 거의 같았고(폭락장만 10~16%),
    # 그런데도 수익률은 상승장 +5%(B&H +38%), 폭락장 -28% 였다. 노출도 13% 로
    # -28% 를 잃으려면 **같은 자리를 반복해서 손절당하고 있어야 한다.**
    #
    # 즉 문제는 매수 판단이 아니라 청산 규칙이었는데, 노출도 지표는 그걸
    # 구조적으로 볼 수 없다. 그래서 두 지표를 추가한다:
    #
    #   capture(캡처율)  = 실제수익 / (평균노출 × B&H수익)
    #       "같은 노출도로 그냥 들고 있었을 때 대비 몇 %를 챙겼나."
    #       트레일링 스탑이 승자를 조기에 털어내면 1.0 아래로 떨어진다.
    #
    #   churn_drag(회전손실, %p) = 실제수익 - (평균노출 × B&H수익)
    #       "그냥 들고 있는 것 대비 몇 %p 를 더 잃었나."
    #       손절 휩쏘(매수→-7%손절→재매수)가 반복되면 크게 음수가 된다.
    # ------------------------------------------------------------------
    passive = stats["avg_exposure"] * stats["buy_hold_pct"]
    stats["passive_pct"] = passive
    stats["churn_drag_pp"] = stats["equity_change_pct"] - passive
    stats["capture"] = (stats["equity_change_pct"] / passive) if abs(passive) > 1e-9 else None

    return stats, _judge(regime, stats)


# ---------------------------------------------------------------------------
# 판정
# ---------------------------------------------------------------------------
def _judge(regime: Regime, stats: Dict) -> List[str]:
    """한 국면의 통계에 대해 위반 목록을 만든다.

    run_regime(단일 시드)와 main(시드 평균) 양쪽에서 같은 규칙을 쓰기 위해
    분리했다. 단일 시드 통과는 운일 수 있으므로 CI 판정은 평균으로 한다.
    """
    failures: List[str] = []

    if stats["avg_exposure"] < regime.min_exposure:
        failures.append(
            f"[{regime.name}] 평균 노출도 {stats['avg_exposure']:.0%} < 최소 {regime.min_exposure:.0%} "
            f"— 상승 국면에서 자본이 놀고 있습니다 (v5.x 회귀)."
        )
    if stats["avg_exposure"] > regime.max_exposure:
        failures.append(
            f"[{regime.name}] 평균 노출도 {stats['avg_exposure']:.0%} > 최대 {regime.max_exposure:.0%} "
            f"— 위험 국면에서 과다 노출 (v4.0 회귀)."
        )
    if stats["total_buys"] == 0:
        failures.append(f"[{regime.name}] 매수 0건 — 전략이 완전히 멈췄습니다.")
    if stats["max_dry_streak"] > regime.max_dry_streak:
        failures.append(
            f"[{regime.name}] 연속 무매수 {stats['max_dry_streak']}세션 > 허용 "
            f"{regime.max_dry_streak}세션 — 조용히 멈추는 구간이 있습니다."
        )

    # [v6.1] 청산 규칙 품질 게이트 -----------------------------------------
    # 노출도만 보면 "얼마나 실려 있나"는 알아도 "실려 있는 동안 얼마나 지켰나"는
    # 모른다. v6.0 이 전 국면에서 노출도 22~25% 로 통과하면서도 상승장 수익이
    # B&H 의 1/7 에 그쳤던 이유가 정확히 이 사각지대다.
    if (regime.min_capture is not None and stats["capture"] is not None
            and stats["passive_pct"] > 1.0 and stats["capture"] < regime.min_capture):
        failures.append(
            f"[{regime.name}] 캡처율 {stats['capture']:.0%} < 최소 {regime.min_capture:.0%} "
            f"(실제 {stats['equity_change_pct']:+.1f}% vs 동일노출 보유 {stats['passive_pct']:+.1f}%) "
            f"— 트레일링/손절이 승자를 조기에 털어내고 있습니다."
        )
    if (regime.max_churn_drag_pp is not None
            and stats["churn_drag_pp"] < regime.max_churn_drag_pp):
        failures.append(
            f"[{regime.name}] 회전손실 {stats['churn_drag_pp']:+.1f}%p < 허용 "
            f"{regime.max_churn_drag_pp:+.1f}%p (매도 {stats['total_sells']}회) "
            f"— 손절 휩쏘로 그냥 보유하는 것보다 크게 잃고 있습니다."
        )
    return failures


# ---------------------------------------------------------------------------
# [v6.1] 소액 계좌 사이징 검증
#
# 2026-09-10 실계좌 상태: US 주문가능금액 $330.64 / 후보 12종목,
# KR 예수금 2,715,678원 / 후보 21종목, 양쪽 모두 보유 0.
#
# 이 조합에서 v6.0 은 구조적으로 단 한 주도 살 수 없었다:
#     per_ticker_cash = 330.64 / 12 = $27.55   (어떤 종목도 1주 미만)
#     그리고 호출부가 int(qty) 를 구한 뒤 배율을 곱했다 -> int(1 * 0.5) = 0
#
# 국면 시뮬레이션은 이 결함을 원리적으로 잡을 수 없다. 자금 '규모'를 모델링하지
# 않기 때문이다. 국면 검증만 있었기 때문에 v6.0 은 전 국면 통과 상태로 배포됐고,
# 실계좌에서는 한 주도 사지 못했다. 그래서 실계좌 수치를 그대로 넣는 층을 둔다.
# ---------------------------------------------------------------------------

SMALL_ACCOUNT_CASES = [
    # (이름, 현금, 후보수, 시장, 살 수 있어야 하는 가격들, 못 사는 게 정상인 가격들)
    ("US $330.64 / 12종목", 330.64, 12, "US",
     [25.0, 90.0, 180.0, 300.0], [400.0, 500.0]),
    ("KR 2,715,678원 / 21종목", 2_715_678, 21, "KR",
     [70_000, 250_000, 1_000_000], [3_000_000]),
]
SIZE_MULTIPLIERS = [0.25, 0.3, 0.5, 0.7, 1.0]


def check_small_account() -> List[str]:
    """실계좌 잔고에서 매수 수량이 0으로 죽지 않는지 검사한다."""
    failures: List[str] = []
    try:
        import run_bot
    except Exception as e:
        return [f"[소액계좌] run_bot 임포트 실패로 검사 불가: {e}"]

    for name, cash, n_targets, market, affordable, unaffordable in SMALL_ACCOUNT_CASES:
        for price in affordable:
            for mult in SIZE_MULTIPLIERS:
                qty = run_bot.calculate_dca_quantity(
                    cash, price, n_targets, run_bot.DCA_SETTINGS, market,
                    weight=1.0, size_multiplier=mult,
                )
                if qty < 1:
                    failures.append(
                        f"[소액계좌] {name}: 가격 {price:,.0f} · 사이즈배율 {mult:.0%} -> {qty}주. "
                        f"현금이 1주를 감당하는데 0주가 나옵니다 (2026-09-10 사고 재발)."
                    )
        for price in unaffordable:
            qty = run_bot.calculate_dca_quantity(
                cash, price, n_targets, run_bot.DCA_SETTINGS, market,
                weight=1.0, size_multiplier=1.0,
            )
            if qty > 0:
                failures.append(
                    f"[소액계좌] {name}: 현금 {cash:,.0f} 로 1주 {price:,.0f} 를 "
                    f"{qty}주 샀습니다 — 미수/주문거부가 납니다."
                )
    return failures


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=60)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--stop", type=float, default=None,
                    help="손절 %% 오버라이드 (스윕용, 음수)")
    ap.add_argument("--trail-act", type=float, default=None,
                    help="트레일링 활성화 %% 오버라이드 (스윕용)")
    ap.add_argument("--trail-drop", type=float, default=None,
                    help="트레일링 하락폭 %% 오버라이드 (스윕용)")
    ap.add_argument("--quiet", action="store_true",
                    help="소액계좌 검사의 사이징 로그를 숨긴다")
    ap.add_argument("--seeds", type=int, default=1,
                    help="이 개수만큼 연속 시드를 돌려 '평균'으로 판정한다. "
                         "단일 시드 통과는 운일 수 있다.")
    args = ap.parse_args()

    global STOP_LOSS_PCT, TRAIL_ACTIVATE_PCT, TRAIL_DROP_PCT
    if args.stop is not None:
        STOP_LOSS_PCT = args.stop
    if args.trail_act is not None:
        TRAIL_ACTIVATE_PCT = args.trail_act
    if args.trail_drop is not None:
        TRAIL_DROP_PCT = args.trail_drop

    seeds = [args.seed + i for i in range(max(1, args.seeds))]

    print("=" * 104)
    print(f" v6.1 전략 검증 루프 — {args.sessions}세션 x {len(REGIMES)}국면 x {len(seeds)}시드 "
          f"(seed={seeds[0]}..{seeds[-1]}, 최소점수={MIN_BUY_SCORE})")
    print(f" 청산 규칙: 손절 {STOP_LOSS_PCT:+.1f}% · 트레일링 활성 {TRAIL_ACTIVATE_PCT:+.1f}% "
          f"· 하락폭 {TRAIL_DROP_PCT:.1f}%")
    print("=" * 104)

    all_failures: List[str] = []

    # --- [0] 소액 계좌 사이징: 국면 시뮬레이션이 못 보는 층 ---
    if args.quiet:
        import logging
        logging.getLogger("Alphatrader").setLevel(logging.WARNING)
    small_failures = check_small_account()
    all_failures.extend(small_failures)
    print(f"[0] 소액 계좌 사이징 ({len(SMALL_ACCOUNT_CASES)}계좌 x "
          f"{len(SIZE_MULTIPLIERS)}배율): "
          f"{'FAIL' if small_failures else 'PASS'}")
    print()

    print(f"{'국면':<15}{'노출도':>8}{'체결률':>8}{'매수':>6}{'매도':>6}"
          f"{'최대무매수':>11}{'사이즈':>8}{'수익률':>9}{'동일노출':>10}"
          f"{'캡처율':>8}{'회전손실':>11}{'B&H':>9}")
    print("-" * 104)

    rows: List[Dict] = []
    for regime in REGIMES:
        stats_list = [run_regime(regime, args.sessions, sd)[0] for sd in seeds]

        agg = {
            "regime": regime.name,
            "fill_rate": _mean([s["fill_rate"] for s in stats_list]),
            "avg_exposure": _mean([s["avg_exposure"] for s in stats_list]),
            "total_buys": int(_mean([s["total_buys"] for s in stats_list])),
            "total_sells": int(_mean([s["total_sells"] for s in stats_list])),
            "max_dry_streak": int(max(s["max_dry_streak"] for s in stats_list)),
            "avg_size": _mean([s["avg_size"] for s in stats_list]),
            "equity_change_pct": _mean([s["equity_change_pct"] for s in stats_list]),
            "buy_hold_pct": _mean([s["buy_hold_pct"] for s in stats_list]),
            "passive_pct": _mean([s["passive_pct"] for s in stats_list]),
            "churn_drag_pp": _mean([s["churn_drag_pp"] for s in stats_list]),
        }
        agg["capture"] = ((agg["equity_change_pct"] / agg["passive_pct"])
                          if abs(agg["passive_pct"]) > 1e-9 else None)

        all_failures.extend(_judge(regime, agg))
        rows.append(agg)

        # 기준선(동일노출 보유)이 음수면 '캡처율'은 의미가 없다 (부호가 뒤집힌다).
        cap = (f"{agg['capture']:>7.0%}"
               if (agg["capture"] is not None and agg["passive_pct"] > 1.0)
               else "      -")
        print(f"{agg['regime']:<15}{agg['avg_exposure']:>7.0%}{agg['fill_rate']:>8.0%}"
              f"{agg['total_buys']:>6}{agg['total_sells']:>6}"
              f"{agg['max_dry_streak']:>11}{agg['avg_size']:>8.0%}"
              f"{agg['equity_change_pct']:>8.1f}%{agg['passive_pct']:>9.1f}%"
              f"{cap}{agg['churn_drag_pp']:>10.1f}%p{agg['buy_hold_pct']:>8.1f}%")

    print("-" * 104)
    print("  동일노출 = 평균 노출도로 그냥 보유했을 때의 수익 (기준선)")
    print("  캡처율   = 수익률 / 동일노출.  100% 미만이면 청산 규칙이 수익을 깎고 있다.")
    print("  회전손실 = 수익률 - 동일노출.  크게 음수면 손절 휩쏘로 갈아먹고 있다.")

    # 사이즈 단조성: 상승장 평균 사이즈 > 조정장 평균 사이즈
    bull = next(r for r in rows if r["regime"].startswith("강한 상승"))
    corr = next(r for r in rows if r["regime"].startswith("조정장"))
    if corr["avg_size"] > 0 and bull["avg_size"] <= corr["avg_size"]:
        all_failures.append(
            f"사이즈 단조성 위반: 상승장 {bull['avg_size']:.0%} <= 조정장 {corr['avg_size']:.0%} "
            f"— 나쁜 국면일수록 작게 사야 합니다."
        )

    if all_failures:
        print()
        print("검증 실패:")
        for f in all_failures:
            print(f"   - {f}")
        return 1

    print()
    print("전 국면 통과 — 소액 계좌에서도 체결되고, 상승장에서 번 것을 지키며, "
          "하락장에서 휩쏘로 갈아먹지 않습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
