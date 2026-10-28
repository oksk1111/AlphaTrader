"""웹소켓/1분봉 기반 장중 ETF 신호 — I/O 없는 순수 계산 계층.

실거래 활성화 전 최소 20거래일 shadow 데이터와 walk-forward 검증이 필요하다.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from modules.brokers.models import Bar, Market, Quote


@dataclass(frozen=True)
class IntradayContext:
    symbol: str
    market: Market
    bars: Sequence[Bar]
    quote: Quote
    daily_closes: Sequence[float]
    benchmark_return_pct: float = 0.0
    relative_volume: float = 1.0
    minutes_to_close: float = 240.0
    stream_age_sec: float = 0.0
    account_ready: bool = True
    daily_loss_halted: bool = False
    order_limit_reached: bool = False


@dataclass(frozen=True)
class IntradayDecision:
    action: str
    score: float
    size_multiplier: float
    components: Dict[str, float] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    release_condition: Optional[str] = None


@dataclass(frozen=True)
class OvernightDecision:
    keep: bool
    score: float
    reasons: List[str]


def _ema(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    value = float(values[-period])
    for current in values[-period + 1:]:
        value = alpha * float(current) + (1.0 - alpha) * value
    return value


def _sma(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(float(v) for v in values[-period:]) / period


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _vwap(bars: Sequence[Bar]) -> Optional[float]:
    volume = sum(max(0.0, bar.volume) for bar in bars)
    if volume <= 0:
        return None
    return sum(((bar.high + bar.low + bar.close) / 3.0) * max(0.0, bar.volume)
               for bar in bars) / volume


def _daily_trend_score(closes: Sequence[float]) -> float:
    if len(closes) < 61:
        return 0.35
    close = float(closes[-1])
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)
    ret20 = close / float(closes[-21]) - 1.0
    structure = 1.0 if close > ma20 > ma60 else 0.5 if close > ma20 else 0.1
    momentum = _clamp(0.5 + ret20 * 5.0)
    return 0.65 * structure + 0.35 * momentum


def evaluate_intraday(ctx: IntradayContext, min_score: float = 0.62,
                      max_spread_pct: float = 0.35) -> IntradayDecision:
    vetoes = [
        (ctx.stream_age_sec > 5.0, "실시간 시세 stale", "다음 정상 웹소켓 틱 수신 즉시"),
        (not ctx.account_ready, "잔고/현금 조회 실패", "다음 계좌 조회 성공 즉시"),
        (ctx.daily_loss_halted, "일일 손실 한도", "다음 거래일"),
        (ctx.order_limit_reached, "세션 주문 횟수 한도", "다음 거래일"),
        (ctx.minutes_to_close <= 20.0, "장 마감 임박", "다음 거래일 개장"),
    ]
    for blocked, reason, release in vetoes:
        if blocked:
            return IntradayDecision("veto", 0.0, 0.0, reasons=[reason], release_condition=release)

    spread = ctx.quote.spread_pct
    if spread is not None and spread > max_spread_pct:
        return IntradayDecision(
            "veto", 0.0, 0.0,
            reasons=[f"스프레드 {spread:.3f}% > {max_spread_pct:.3f}%"],
            release_condition=f"스프레드가 {max_spread_pct:.3f}% 이하로 정상화",
        )
    if len(ctx.bars) < 22:
        return IntradayDecision("defer", 0.0, 0.0, reasons=["완성 1분봉 22개 미만"])

    bars = list(ctx.bars)
    closes = [bar.close for bar in bars]
    price = ctx.quote.price
    vwap = _vwap(bars)
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    previous_high = max(bar.high for bar in bars[-6:-1])
    open_price = bars[0].open
    intraday_return = ((price / open_price) - 1.0) * 100.0 if open_price > 0 else 0.0
    relative_strength = intraday_return - ctx.benchmark_return_pct

    components = {
        "daily_trend": _daily_trend_score(ctx.daily_closes),
        "vwap": _clamp(0.5 + ((price / vwap - 1.0) * 100.0) * 1.5) if vwap else 0.4,
        "ema": 1.0 if price > ema9 > ema21 else 0.55 if price > ema21 else 0.15,
        "breakout": 1.0 if price > previous_high else _clamp(price / previous_high - 0.995, 0.0, 0.005) / 0.005,
        "relative_volume": _clamp((ctx.relative_volume - 0.7) / 1.3),
        "relative_strength": _clamp(0.5 + relative_strength / 3.0),
        "liquidity": 1.0 if spread is None else _clamp(1.0 - spread / max_spread_pct),
    }
    weights = {
        "daily_trend": 0.18, "vwap": 0.18, "ema": 0.16, "breakout": 0.15,
        "relative_volume": 0.13, "relative_strength": 0.12, "liquidity": 0.08,
    }
    score = sum(components[name] * weight for name, weight in weights.items())
    score = round(_clamp(score), 4)
    if score < min_score:
        return IntradayDecision(
            "defer", score, 0.0, components,
            reasons=[f"장중 점수 {score:.3f} < {min_score:.3f}"],
        )
    size = round(_clamp((score - min_score) / max(1e-9, 1.0 - min_score), 0.25, 1.0), 4)
    return IntradayDecision("buy", score, size, components, reasons=["장중 복합점수 충족"])


def evaluate_overnight(ctx: IntradayContext, min_score: float = 0.72) -> OvernightDecision:
    if len(ctx.bars) < 22 or len(ctx.daily_closes) < 61:
        return OvernightDecision(False, 0.0, ["오버나잇 판정 데이터 부족"])
    bars = list(ctx.bars)
    closes = [bar.close for bar in bars]
    vwap = _vwap(bars)
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    price = ctx.quote.price
    daily = _daily_trend_score(ctx.daily_closes)
    components = [
        daily,
        1.0 if vwap and price > vwap else 0.0,
        1.0 if ema9 and ema21 and price > ema9 > ema21 else 0.0,
        _clamp(ctx.relative_volume / 1.5),
        _clamp(0.5 + ctx.benchmark_return_pct / 4.0),
    ]
    score = round(sum(components) / len(components), 4)
    keep = score >= min_score and ctx.minutes_to_close <= 20.0
    reasons = [f"오버나잇 점수 {score:.3f} {'≥' if keep else '<'} {min_score:.3f}"]
    if ctx.minutes_to_close > 20.0:
        reasons.append("마감 판정 시간이 아님")
    return OvernightDecision(keep, score, reasons)
