"""
decision_engine.py — v6.0 점수 기반 매수 판단 엔진

[왜 이 모듈이 생겼는가 — v4.0/v5.0 실패의 공통 원인]

v4.0 은 "무조건 사라"(게이트 전부 OFF) 였다 → 반도체 붕괴장에서 계속 물타기, 대손실.
v5.0/v5.1 은 "게이트를 전부 복원"했다 → 몇 달째 한 주도 못 샀다.

두 버전은 정반대로 보이지만 똑같은 버그를 공유한다:
매수 판단이 이진(binary) 통과/차단이고, 그 판정을 하루에 딱 한 번, 장 시작
5분 안에 내린다는 것. 임계값을 어느 쪽으로 밀든 "전부 사거나 / 전혀 안 사거나"
두 극단밖에 나오지 않는다. 튜닝으로 고칠 수 있는 문제가 아니라 구조의 문제다.

특히 v5.0 의 추세 게이트(현재가 > MA20, KR 은 AND 현재가 > MA5)는 DCA 의 정의와
정면으로 모순된다. DCA(분할매수)의 존재 이유는 하락 구간에서 평단을 낮추는 것인데,
"MA20 위에서만 산다"는 조건은 하락 구간 매수를 전부 금지한다. 게다가 그 판정을
KR 은 09:00 개장 동시호가 체결가 한 틱으로, US 는 23:30 한 틱으로 내린다.
조정장이 몇 주 이어지면 매수 건수는 정확히 0이 된다.

[v6.0 의 원칙]

  1. 게이트는 허가가 아니라 사이즈를 정한다. 대부분의 신호는 0~1 점수로 환산해
     매수 '수량'에 곱한다. 나쁜 신호 = 적게 산다. (나쁜 신호 = 아예 안 산다 → 폐기)
  2. 진짜 veto 는 소수만. 그리고 모든 veto 는 반드시 (a) 자동 해제 조건과
     (b) 알림을 갖는다. 조용히 영구히 막히는 경로를 남기지 않는다.
  3. 하락은 감점이 아니라 가점이 될 수 있다. 단, '떨어지는 칼날'과 '눌림목'을
     구분한다 — 장기 추세(MA60) 위에 있으면서 단기 조정이면 가점, 장기 추세까지
     깨졌으면 감점.
  4. 판단은 장중 내내 반복된다. 이 함수는 종목당 하루 1회가 아니라 감시 주기마다
     호출되는 것을 전제로 설계됐다 (재평가 쿨다운은 호출부가 관리).
  5. 누적 노출 상한으로 물타기를 통제한다. "사지 마"가 아니라 "여기까지만 사".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 매수 실행 최소 점수. 이 아래는 'defer'(이번 주기 보류, 다음 주기 재평가).
MIN_BUY_SCORE = 0.35
# 점수 → 사이즈 매핑 시 최소 배율 (점수가 낮아도 아주 소액은 산다 = DCA 지속성)
MIN_SIZE_MULT = 0.25


@dataclass
class TickerContext:
    """한 종목의 매수 판단에 필요한 모든 입력."""
    ticker: str
    market: str                      # 'US' | 'KR'
    price: float
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None
    ma60: Optional[float] = None
    atr_pct: Optional[float] = None
    prev_close: Optional[float] = None
    day_low: Optional[float] = None
    day_high: Optional[float] = None
    gap_pct: float = 0.0             # 전일종가 대비 (음수=갭다운)
    consec_decline_pct: float = 0.0  # 최근 N일 누적 하락률 (양수=하락폭)
    is_leveraged: bool = False

    # 포지션/예산
    holding_qty: int = 0
    holding_avg_price: float = 0.0
    position_value: float = 0.0      # 현재 이 종목 평가금액
    max_position_value: float = 0.0  # 이 종목 허용 최대 노출 (0=무제한)
    buys_this_session: int = 0
    max_buys_per_session: int = 10

    # 외부 판정
    stopped_out_today: bool = False
    market_sentiment: Optional[Dict[str, Any]] = None
    sector_sentiment: Optional[Dict[str, Any]] = None
    correlation_capped: bool = False


@dataclass
class PortfolioContext:
    """계좌 전체 상태."""
    available_cash: float = 0.0
    risk_size_multiplier: float = 1.0   # risk_state 낙폭 사다리
    risk_halted: bool = False
    risk_reason: str = ""
    losing_streak_paused: bool = False
    minutes_to_close: float = 999.0
    minutes_since_open: float = 999.0


@dataclass
class BuyDecision:
    action: str                      # 'buy' | 'defer' | 'veto'
    score: float = 0.0
    size_multiplier: float = 0.0
    reasons: List[str] = field(default_factory=list)
    veto_reason: str = ""

    @property
    def should_buy(self) -> bool:
        return self.action == "buy"

    def summary(self) -> str:
        if self.action == "veto":
            return f"VETO — {self.veto_reason}"
        head = f"{self.action.upper()} score={self.score:.2f} size={self.size_multiplier:.0%}"
        return head + (f" | {', '.join(self.reasons)}" if self.reasons else "")


# ---------------------------------------------------------------------------
# 개별 점수 요소. 각 함수는 (기여도 -1.0~+1.0, 설명) 을 반환한다.
# ---------------------------------------------------------------------------

def _score_trend(c: TickerContext) -> Tuple[float, str]:
    """추세 점수.

    v5.0 은 여기서 '통과/차단'을 결정했다. v6.0 은 점수만 매긴다.
    핵심: 장기 추세(MA60)가 살아 있으면 단기 하락은 기회로 본다.
    """
    price = c.price
    long_ma = c.ma60 or c.ma20
    mid_ma = c.ma20
    short_ma = c.ma5

    if not mid_ma:
        return 0.0, "MA20 없음(데이터 부족) - 중립"

    above_mid = price > mid_ma
    above_long = (price > long_ma) if long_ma else above_mid
    above_short = (price > short_ma) if short_ma else True

    if above_mid and above_short:
        return 1.0, "정배열 (MA5·MA20 상회)"
    if above_mid and not above_short:
        return 0.55, "MA20 상회 · MA5 하회 (단기 눌림)"
    if above_long and not above_mid:
        # 장기추세 유지 + 중기 조정 = DCA 가 가장 잘 먹히는 구간
        return 0.7, "MA60 상회 · MA20 하회 (눌림목 매수 구간)"
    if above_short and not above_long:
        return 0.3, "장기추세 이탈 · 단기 반등 중 (소액만)"
    return -0.4, "장·중·단기 모두 하회 (하락추세 - 감점)"


def _score_regime(c: TickerContext) -> Tuple[float, str]:
    """추세 '구조' 점수 — MA20 과 MA60 의 상대 위치.

    이 항목이 v6.0 검증 루프에서 추가됐다. 초기 v6.0 은 국면별 체결률이
    **역전**돼 있었다 (강한 상승장 42% vs 폭락장 85%). 폭락장에서 손절로 현금이
    계속 풀리고, 높은 변동성 덕에 당일 저점 대비 반등이 수시로 발생해 낙하칼날
    가드를 빠져나갔기 때문이다. 즉 v4.0 의 "하락장에서 매일 물타기" 실패 모드가
    형태만 바꿔 재발한 것이다.

    핵심 구분은 '가격이 MA20 아래인가'가 아니라 **'MA20 자체가 MA60 아래인가'** 다:
      - MA20 > MA60 (정배열 구조) + 가격이 MA20 아래  →  상승추세 속 눌림목. 산다.
      - MA20 < MA60 (역배열 구조)                     →  추세가 꺾인 하락장. 줄인다.
    전자는 DCA 가 가장 잘 먹히는 자리이고, 후자는 DCA 가 계좌를 갈아먹는 자리다.
    """
    if not c.ma20 or not c.ma60 or c.ma60 <= 0:
        return 0.0, ""
    spread = (c.ma20 - c.ma60) / c.ma60 * 100.0
    if spread >= 3.0:
        return 0.8, f"정배열 구조 (MA20 > MA60 +{spread:.1f}%)"
    if spread >= 0.0:
        return 0.35, f"완만한 정배열 (MA20 > MA60 +{spread:.1f}%)"
    if spread >= -3.0:
        return -0.4, f"추세 약화 (MA20 < MA60 {spread:.1f}%)"
    if spread >= -8.0:
        return -0.8, f"역배열 구조 (MA20 < MA60 {spread:.1f}%) - 하락추세"
    return -1.0, f"급격한 역배열 (MA20 < MA60 {spread:.1f}%) - 하락 가속"


def _score_pullback(c: TickerContext) -> Tuple[float, str]:
    """당일 저점 대비 반등 여부. 떨어지는 칼날 회피용."""
    if not c.day_low or c.day_low <= 0:
        return 0.0, ""
    bounce = (c.price - c.day_low) / c.day_low * 100.0
    if bounce >= 1.5:
        return 0.5, f"당일 저점 대비 +{bounce:.1f}% 반등"
    if bounce >= 0.8:
        return 0.1, f"당일 저점 대비 +{bounce:.1f}%"
    # 저점에서 0.8% 도 못 올라온 상태 = 아직 하락이 진행 중. 낙하칼날 가드의 입력이
    # 되므로 임계를 넉넉히 잡는다 (0.1% 로 두면 사실상 아무것도 걸러내지 못했다).
    return -0.35, f"당일 저점 부근 +{bounce:.1f}% (하락 지속 - 감점)"


def _score_gap(c: TickerContext) -> Tuple[float, str]:
    """갭. 완만한 갭다운은 DCA 기회, 패닉 갭다운은 별도 veto 에서 처리."""
    g = c.gap_pct
    if g >= 3.0:
        return -0.2, f"갭업 +{g:.1f}% (추격매수 억제)"
    if -4.0 <= g <= -1.0:
        return 0.35, f"완만한 갭다운 {g:.1f}% (분할매수 기회)"
    if g < -4.0:
        return -0.3, f"큰 갭다운 {g:.1f}% (사이즈 축소)"
    return 0.0, ""


def _score_decline(c: TickerContext) -> Tuple[float, str]:
    """연속 하락. v5.0 은 무조건 차단했지만, DCA 관점에서는 축적 구간이다.
    다만 폭이 크면 사이즈를 줄인다."""
    d = c.consec_decline_pct
    if d <= 0:
        return 0.0, ""
    if d < 5.0:
        return 0.25, f"연속 하락 {d:.1f}% (분할매수 구간)"
    if d < 10.0:
        return -0.15, f"연속 하락 {d:.1f}% (사이즈 축소)"
    return -0.45, f"연속 하락 {d:.1f}% (급락 - 대폭 축소)"


def _score_sentiment(c: TickerContext) -> Tuple[float, str]:
    """AI 시장/섹터 감성. CRASH 는 veto 에서 잡고, 여기서는 HIGH/LOW 만 가중."""
    total = 0.0
    notes: List[str] = []
    for label, s in (("시장", c.market_sentiment), ("섹터", c.sector_sentiment)):
        if not s:
            continue
        risk = str(s.get("risk_level", "")).upper()
        if risk == "HIGH":
            total -= 0.35
            notes.append(f"{label} 위험 HIGH")
        elif risk == "LOW":
            total += 0.2
            notes.append(f"{label} 위험 LOW")
    return total, " · ".join(notes)


def _score_position(c: TickerContext) -> Tuple[float, str]:
    """이미 많이 담았으면 감점. '사지 마'가 아니라 '천천히 담아'."""
    if c.max_position_value <= 0 or c.position_value <= 0:
        return 0.0, ""
    used = c.position_value / c.max_position_value
    if used >= 1.0:
        return -1.0, f"종목 노출 한도 도달 ({used:.0%})"
    if used >= 0.75:
        return -0.35, f"종목 노출 {used:.0%} (한도 근접)"
    if used >= 0.5:
        return -0.15, f"종목 노출 {used:.0%}"
    return 0.1, ""


def _score_avg_down(c: TickerContext) -> Tuple[float, str]:
    """평단 대비 위치. 평단보다 충분히 낮으면 물타기 가점."""
    if c.holding_qty <= 0 or c.holding_avg_price <= 0:
        return 0.0, ""
    pnl = (c.price - c.holding_avg_price) / c.holding_avg_price * 100.0
    if pnl <= -8.0:
        return -0.2, f"평단 대비 {pnl:.1f}% (손실 과다 - 신중)"
    if pnl <= -3.0:
        return 0.3, f"평단 대비 {pnl:.1f}% (물타기 구간)"
    if pnl >= 10.0:
        return -0.25, f"평단 대비 +{pnl:.1f}% (고점 추격 억제)"
    return 0.0, ""


# ---------------------------------------------------------------------------
# 하드 veto — 반드시 자동 해제 조건이 있어야 한다.
# ---------------------------------------------------------------------------

def _hard_vetoes(c: TickerContext, p: PortfolioContext) -> Optional[str]:
    if c.price <= 0:
        return "현재가 조회 실패"
    if p.available_cash < c.price:
        # 해제조건: 입금/매도로 현금이 생기면 즉시 해제 (매 주기 재평가)
        return f"주문가능금액 부족 (현금 {p.available_cash:,.0f} < 1주 {c.price:,.0f})"
    if c.stopped_out_today:
        # 해제조건: 다음 영업일
        return "금일 손절 종목 - 당일 재매수 금지 (휩쏘 방지)"
    if c.buys_this_session >= c.max_buys_per_session:
        # 해제조건: 다음 세션
        return f"세션 매수 횟수 한도 ({c.max_buys_per_session}회)"
    if p.minutes_to_close <= 15:
        # 해제조건: 다음 세션
        return "폐장 15분 전 - 신규 진입 금지"
    if p.risk_halted:
        # 해제조건: risk_state 의 cool-off / 낙폭 회복
        return f"계좌 낙폭 서킷브레이커: {p.risk_reason}"
    if p.losing_streak_paused:
        # 해제조건: 익일 자정
        return "일일 손실 한도 초과 - 당일 신규 매수 중단"
    if c.gap_pct <= -8.0:
        # 해제조건: 익일 (또는 당일 반등으로 gap_pct 개선 시 즉시)
        return f"패닉 갭다운 {c.gap_pct:.1f}%"
    for label, s in (("시장", c.market_sentiment), ("섹터", c.sector_sentiment)):
        if s and str(s.get("market_condition", "")).upper() == "CRASH":
            # 해제조건: 다음 감성 재조회 (캐시 5분)
            return f"AI {label} CRASH 판정: {str(s.get('reason', 'N/A'))[:80]}"
    if c.correlation_capped and c.holding_qty <= 0:
        # 해제조건: 상관그룹 내 포지션 청산 시. 기보유 종목의 추가매수는 막지 않는다.
        return "상관그룹 신규 진입 한도 초과"
    if c.max_position_value > 0 and c.position_value >= c.max_position_value:
        # 종목 노출 상한은 점수 감점이 아니라 하드 한도여야 한다. 감점(가중치 0.10)으로
        # 두면 "한도 100% 도달"인데도 다른 지표가 좋으면 계속 사버린다.
        # 해제조건: 가격 하락/일부 청산으로 평가금액이 한도 아래로 내려가면 즉시.
        return (f"종목 노출 한도 도달 "
                f"({c.position_value:,.0f}/{c.max_position_value:,.0f})")
    return None


# ---------------------------------------------------------------------------
# 메인 진입점
# ---------------------------------------------------------------------------

_WEIGHTS = {
    "trend": 0.22,
    "regime": 0.26,   # 추세 '구조' — 국면 역전(하락장 과매수)을 막는 핵심 가중치
    "pullback": 0.12,
    "gap": 0.08,
    "decline": 0.10,
    "sentiment": 0.12,
    "position": 0.06,
    "avg_down": 0.04,
}


def evaluate_buy(c: TickerContext, p: PortfolioContext,
                 min_score: float = MIN_BUY_SCORE) -> BuyDecision:
    """한 종목의 매수 여부·사이즈를 판단한다.

    반환 action:
      'veto'  — 이번 주기 매수 불가 (사유 있음, 자동 해제 조건 있음)
      'defer' — 점수 미달. 다음 주기에 다시 본다. 차단이 아니다.
      'buy'   — size_multiplier 만큼 매수.
    """
    veto = _hard_vetoes(c, p)
    if veto:
        return BuyDecision(action="veto", veto_reason=veto)

    parts = {
        "trend": _score_trend(c),
        "regime": _score_regime(c),
        "pullback": _score_pullback(c),
        "gap": _score_gap(c),
        "decline": _score_decline(c),
        "sentiment": _score_sentiment(c),
        "position": _score_position(c),
        "avg_down": _score_avg_down(c),
    }

    raw = 0.0
    reasons: List[str] = []
    for key, (val, note) in parts.items():
        raw += _WEIGHTS[key] * val
        if note:
            reasons.append(note)

    # --- 낙하칼날 가드 ---------------------------------------------------
    # 장·중·단기 MA 를 전부 하회(추세 점수 음수)하면서 당일 저점에서 반등도 없는
    # 종목은, 다른 지표가 아무리 좋아도 이번 주기에는 사지 않는다.
    # v4.0 이 SOXL 을 매일 물타기하다 대손실을 낸 구간이 정확히 이 상태였다.
    # 단, veto 가 아니라 defer 다 — 장중에 저점 대비 반등이 나오면 같은 세션 안에서
    # 다시 매수 후보가 된다. "오늘은 끝"이 아니라 "지금은 아니다".
    if parts["trend"][0] < 0 and parts["pullback"][0] < 0:
        return BuyDecision(
            action="defer", score=0.0, size_multiplier=0.0,
            reasons=reasons + ["낙하칼날 가드: 전 이평 하회 + 반등 미확인 - 반등 시 재평가"],
        )

    # raw 범위는 대략 -1.0 ~ +1.0. 0~1 로 정규화.
    score = round(max(0.0, min(1.0, (raw + 1.0) / 2.0)), 3)

    # 레버리지 ETF 는 변동성 decay 때문에 같은 점수라도 더 작게 산다.
    lev_adj = 0.7 if c.is_leveraged else 1.0
    # 개장 직후 30분은 변동성이 극단적 - 절반만.
    open_adj = 0.5 if p.minutes_since_open < 30 else 1.0

    if score < min_score:
        return BuyDecision(
            action="defer", score=score, size_multiplier=0.0,
            reasons=reasons + [f"점수 {score:.2f} < 기준 {min_score:.2f} - 다음 주기 재평가"],
        )

    # 점수를 사이즈로: min_score→MIN_SIZE_MULT, 1.0→1.0 선형 보간
    span = max(1e-6, 1.0 - min_score)
    size = MIN_SIZE_MULT + (1.0 - MIN_SIZE_MULT) * ((score - min_score) / span)
    size *= lev_adj * open_adj * max(0.0, p.risk_size_multiplier)
    size = round(max(0.0, min(1.0, size)), 3)

    if size <= 0.0:
        return BuyDecision(action="defer", score=score, size_multiplier=0.0,
                           reasons=reasons + ["사이즈 배율 0 (낙폭 사다리)"])

    return BuyDecision(action="buy", score=score, size_multiplier=size, reasons=reasons)
