"""증권사 원본 응답이 전략 계층으로 새지 않게 하는 정규 도메인 모델."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Market(str, Enum):
    KR = "KR"
    US = "US"


class ExecutionMode(str, Enum):
    SHADOW = "shadow"
    PAPER = "paper"
    LIVE = "live"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderState(str, Enum):
    ACCEPTED = "ACCEPTED"
    PENDING = "PENDING"
    PARTIAL_FILLED = "PARTIAL_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class BrokerError(RuntimeError):
    """조회 실패와 정상적인 0/빈 목록을 구분하는 브로커 오류."""

    def __init__(self, message: str, code: str = "broker-error",
                 request_id: Optional[str] = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.retryable = retryable


@dataclass(frozen=True)
class Holding:
    symbol: str
    name: str
    market: Market
    currency: str
    quantity: float
    sellable_quantity: float
    average_price: float
    last_price: float
    market_value: float
    profit_loss: float
    profit_rate_pct: float


@dataclass(frozen=True)
class AccountSnapshot:
    account_seq: str
    cash: Dict[str, float]
    holdings: List[Holding]
    market_value: Dict[str, float] = field(default_factory=dict)
    profit_loss: Dict[str, float] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def cash_for(self, market: Market) -> float:
        currency = "KRW" if market == Market.KR else "USD"
        return max(0.0, float(self.cash.get(currency, 0.0)))

    def holdings_for(self, market: Market) -> List[Holding]:
        return [holding for holding in self.holdings if holding.market == market]


@dataclass(frozen=True)
class Quote:
    symbol: str
    market: Market
    currency: str
    price: float
    timestamp: Optional[str] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None

    @property
    def spread_pct(self) -> Optional[float]:
        if not self.bid or not self.ask or self.bid <= 0 or self.ask < self.bid:
            return None
        mid = (self.bid + self.ask) / 2.0
        return (self.ask - self.bid) / mid * 100.0 if mid > 0 else None


@dataclass(frozen=True)
class Bar:
    symbol: str
    market: Market
    currency: str
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    interval: str


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    market: Market
    side: OrderSide
    order_type: OrderType
    quantity: Optional[float] = None
    amount: Optional[float] = None
    price: Optional[float] = None
    time_in_force: str = "DAY"
    client_order_id: Optional[str] = None

    def validate(self) -> None:
        if bool(self.quantity) == bool(self.amount):
            raise ValueError("quantity 또는 amount 중 정확히 하나가 필요합니다")
        if self.order_type == OrderType.LIMIT and not self.price:
            raise ValueError("지정가 주문에는 price가 필요합니다")
        if self.order_type == OrderType.MARKET and self.price is not None:
            raise ValueError("시장가 주문에는 price를 보낼 수 없습니다")
        if self.amount is not None and not (
            self.market == Market.US and self.side == OrderSide.BUY
            and self.order_type == OrderType.MARKET
        ):
            raise ValueError("금액 주문은 미국 시장가 매수만 지원합니다")


@dataclass(frozen=True)
class OrderAck:
    order_id: str
    client_order_id: Optional[str]
    state: OrderState
    symbol: Optional[str] = None
    filled_quantity: float = 0.0
    average_filled_price: Optional[float] = None
    message: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
