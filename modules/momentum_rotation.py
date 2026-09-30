"""
momentum_rotation.py — v7.0 "무엇을 들고 있을 것인가"를 정하는 유일한 곳

[왜 이 모듈이 생겼는가]

v3 → v6.1 의 모든 개편은 **언제 살까**(매수 타이밍 게이트)와 **언제 팔까**(손절폭)만
고쳤다. 그런데 2026-10-01 에 실데이터(2016~2026, yfinance 일봉)로 현행 구조를
돌려보니 문제는 그 둘이 아니라 **무엇을 들고 있느냐**였다 (docs/주식 자동 매매 기획.md 문서 1-C):

  · KR 유니버스가 ~21종목(하드코딩 17 + 동적 + 스캐너 급등주)이고,
    `portfolio_selection.max_kr_stock: 0` 설정과 달리 개별주·단일종목 레버리지까지
    섞여 있었다. 270만원을 21종목에 쪼개면 종목당 13만원 — 분산이 아니라 희석이다.
  · 매일 장중 60초마다 손절/트레일링을 보니, 일봉 기준으로는 노이즈인 장중 꼬리에
    반복해서 털렸다.

두 참고 저장소는 정반대 구조로 실제 OOS 를 추적하고 있다:

  · tachikoma/kr-quant-engine — ETF 듀얼 모멘텀 로테이션.
    점수 = 0.55·z(R60) + 0.45·z(R120), 추세조건 close > MA20 > MA60,
    상위 2개 보유, 순위가 버퍼(3위) 밖으로 밀려야만 매도(히스테리시스),
    시장필터 = 지수 close ≥ MA120 이고 MA120 기울기(20일) ≥ 0, 아니면 현금.
  · dragon1086/prism-insight — O'Neil RS 레이팅(2·R63+R126+R189+R252),
    Market Pulse 국면 판정, **종가 기준** 손절(장중 꼬리는 매도 사유가 아님).

이 모듈은 그 규칙들을 I/O 없는 순수 함수로 구현한다. 봇(run_bot)과 실데이터
백테스트(scripts/backtest_real.py)가 **같은 함수**를 호출한다 — 검증한 코드와
운영하는 코드가 다르면 검증은 아무것도 보장하지 않는다.

[AGENTS.md 규칙과의 관계]

  · 이 모듈은 매수 '허가'를 내리지 않는다. **보유 후보(universe)** 를 정한다.
    후보 안에서의 매수 타이밍/사이즈는 여전히 decision_engine 이 점수로 정한다.
  · 시장필터(risk-off)는 veto 다. 자동 해제 조건: 지수가 MA120 위로 복귀하고
    MA120 기울기가 양(+)이 되는 즉시. 해제 조건 없는 차단이 아니다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# --- 기본 파라미터 (kr-quant-engine strategy_freeze.json v2 와 동일) ---------
MOM_SHORT = 60           # 단기 모멘텀 룩백 (거래일)
MOM_LONG = 120           # 장기 모멘텀 룩백
MOM_SHORT_WEIGHT = 0.55  # 0.55·z(R60) + 0.45·z(R120)
MARKET_MA = 120          # 시장필터 이동평균
MARKET_SLOPE = 20        # 시장필터 기울기 비교 간격
DEFAULT_SLOTS = 2        # 동시 보유 종목 수
DEFAULT_SELL_BUFFER = 3  # 순위가 이 밖으로 밀려야 매도 (보유 종목 한정)


@dataclass
class Features:
    ticker: str
    close: float
    ret_short: Optional[float]
    ret_long: Optional[float]
    ma20: Optional[float]
    ma60: Optional[float]
    rs_raw: Optional[float] = None   # O'Neil 가중수익률 (참고/동점 처리용)

    @property
    def trend_ok(self) -> bool:
        """close > MA20 > MA60 — kr-quant-engine 의 추세조건."""
        if self.ma20 is None or self.ma60 is None:
            return False
        return self.close > self.ma20 > self.ma60

    @property
    def eligible(self) -> bool:
        return self.ret_short is not None and self.ret_long is not None and self.trend_ok


@dataclass
class RotationPlan:
    """한 시점의 로테이션 결정."""
    risk_on: bool
    slots: int
    targets: List[str]                       # 이번에 보유해야 할 종목 (순위순)
    keep: List[str]                          # 보유 중 + 버퍼 안이라 유지
    sell: List[Tuple[str, str]]              # (종목, 사유) — 보유 중이지만 내보낼 것
    ranking: List[Tuple[str, float]] = field(default_factory=list)

    def summary(self) -> str:
        top = ", ".join(f"{t}({s:+.2f})" for t, s in self.ranking[:5]) or "없음"
        head = "RISK-ON" if self.risk_on else "RISK-OFF(현금)"
        sells = ", ".join(f"{t}:{r}" for t, r in self.sell) or "-"
        return (f"{head} slots={self.slots} targets={self.targets} "
                f"keep={self.keep} sell=[{sells}] | 순위 {top}")


# ---------------------------------------------------------------------------
# 특징량
# ---------------------------------------------------------------------------
def _ret(closes: Sequence[float], n: int) -> Optional[float]:
    if len(closes) <= n:
        return None
    base = closes[-1 - n]
    if not base or base <= 0:
        return None
    return closes[-1] / base - 1.0


def _sma(closes: Sequence[float], n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def oneil_rs_raw(closes: Sequence[float]) -> Optional[float]:
    """prism-insight cores/rs_rating.py 와 동일: 2·R63 + R126 + R189 + R252."""
    if len(closes) <= 252:
        return None
    return 2 * _ret(closes, 63) + _ret(closes, 126) + _ret(closes, 189) + _ret(closes, 252)


def compute_features(ticker: str, closes: Sequence[float]) -> Features:
    """closes: 오래된 것 → 최신 순서의 일봉 종가."""
    closes = [float(c) for c in closes if c is not None and float(c) > 0]
    if not closes:
        return Features(ticker, 0.0, None, None, None, None)
    return Features(
        ticker=ticker,
        close=closes[-1],
        ret_short=_ret(closes, MOM_SHORT),
        ret_long=_ret(closes, MOM_LONG),
        ma20=_sma(closes, 20),
        ma60=_sma(closes, 60),
        rs_raw=oneil_rs_raw(closes),
    )


def _zscores(values: List[float]) -> List[float]:
    n = len(values)
    if n == 0:
        return []
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    sd = math.sqrt(var)
    if sd < 1e-12:
        return [0.0] * n
    return [(v - mean) / sd for v in values]


def rank_universe(features: Iterable[Features],
                  short_weight: float = MOM_SHORT_WEIGHT) -> List[Tuple[str, float]]:
    """추세조건을 통과한 종목만 듀얼 모멘텀 점수로 정렬해 반환.

    z-score 는 **추세조건 통과 종목끼리** 계산한다 (kr-quant-engine 과 동일).
    통과 종목이 1개면 점수 0 으로 그대로 1위.
    """
    elig = [f for f in features if f.eligible]
    if not elig:
        return []
    zs = _zscores([f.ret_short for f in elig])
    zl = _zscores([f.ret_long for f in elig])
    scored = [
        (f.ticker, short_weight * a + (1 - short_weight) * b, f.rs_raw or 0.0)
        for f, a, b in zip(elig, zs, zl)
    ]
    # 동점이면 O'Neil RS 가 높은 쪽
    scored.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return [(t, round(s, 4)) for t, s, _ in scored]


# ---------------------------------------------------------------------------
# 시장 필터
# ---------------------------------------------------------------------------
def market_risk_on(index_closes: Sequence[float], ma: int = MARKET_MA,
                   slope: int = MARKET_SLOPE) -> bool:
    """지수 close ≥ MA(ma) 이고 MA 가 slope 거래일 전보다 높거나 같으면 risk-on.

    데이터가 부족하면 **risk-on (fail-open)**. 데이터 조회 실패가 곧 전면 매수
    중단이 되면 v5.x 의 "조용한 영구 정지"가 재현된다.
    """
    closes = [float(c) for c in index_closes if c is not None and float(c) > 0]
    if len(closes) < ma + slope:
        return True
    ma_now = sum(closes[-ma:]) / ma
    ma_prev = sum(closes[-ma - slope:-slope]) / ma
    return closes[-1] >= ma_now and ma_now >= ma_prev


# ---------------------------------------------------------------------------
# 슬롯 수 — "채울 수 있는 슬롯 수로 나눈다" (AGENTS v6.1 규칙 7)
# ---------------------------------------------------------------------------
def fillable_slots(equity: float, min_slot_value: float,
                   max_slots: int = DEFAULT_SLOTS) -> int:
    """총자산으로 채울 수 있는 슬롯 수. 최소 1.

    최소 주문금액보다 잘게 쪼개면 분산이 아니라 전면 정지다.
    """
    if max_slots <= 1 or min_slot_value <= 0:
        return max(1, max_slots)
    return max(1, min(max_slots, int(equity // min_slot_value)))


# ---------------------------------------------------------------------------
# 로테이션 결정
# ---------------------------------------------------------------------------
def plan_rotation(features: Iterable[Features], holdings: Iterable[str],
                  risk_on: bool, slots: int = DEFAULT_SLOTS,
                  sell_buffer: int = DEFAULT_SELL_BUFFER,
                  liquidate_on_risk_off: bool = True) -> RotationPlan:
    """보유 종목과 순위로 targets/keep/sell 을 정한다.

    - risk-off: 신규 편입 없음. liquidate_on_risk_off 면 보유 전량 매도 사유 부여.
    - 보유 종목은 순위가 sell_buffer 안이면 유지 (히스테리시스 — 회전율 억제).
    - 보유 종목이 추세조건을 잃어 순위에서 빠지면 매도.
    - 남는 슬롯은 순위 상위 비보유 종목으로 채운다.
    """
    feats = list(features)
    holdings = [h for h in holdings]
    ranking = rank_universe(feats)
    rank_of = {t: i + 1 for i, (t, _) in enumerate(ranking)}
    slots = max(1, int(slots))

    if not risk_on:
        sells = [(h, "시장필터 risk-off") for h in holdings] if liquidate_on_risk_off else []
        keep = [] if liquidate_on_risk_off else list(holdings)
        return RotationPlan(False, slots, keep[:], keep, sells, ranking)

    keep: List[str] = []
    sell: List[Tuple[str, str]] = []
    for h in holdings:
        r = rank_of.get(h)
        if r is None:
            sell.append((h, "추세이탈(close≤MA20 또는 MA20≤MA60)"))
        elif r > max(sell_buffer, slots):
            sell.append((h, f"순위 {r}위 > 버퍼 {max(sell_buffer, slots)}위"))
        else:
            keep.append(h)

    # 버퍼 안에 있어도 슬롯보다 많이 들고 있으면 순위 낮은 것부터 내보낸다
    keep.sort(key=lambda t: rank_of[t])
    while len(keep) > slots:
        t = keep.pop()
        sell.append((t, f"슬롯 초과 ({slots})"))

    targets = list(keep)
    for t, _ in ranking:
        if len(targets) >= slots:
            break
        if t not in targets:
            targets.append(t)
    return RotationPlan(True, slots, targets, keep, sell, ranking)
