"""자동매매 엔진이 사용하는 증권사 공통 계약."""

from abc import ABC, abstractmethod
from typing import List, Optional

from modules.brokers.models import (
    AccountSnapshot, Bar, Market, OrderAck, OrderRequest, Quote,
)


class Broker(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def account_snapshot(self) -> AccountSnapshot:
        raise NotImplementedError

    @abstractmethod
    def quote(self, symbol: str, market: Market) -> Quote:
        raise NotImplementedError

    @abstractmethod
    def bars(self, symbol: str, market: Market, interval: str = "1d",
             count: int = 200, before: Optional[str] = None) -> List[Bar]:
        raise NotImplementedError

    @abstractmethod
    def submit_order(self, order: OrderRequest) -> OrderAck:
        raise NotImplementedError

    @abstractmethod
    def order(self, order_id: str) -> OrderAck:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str) -> OrderAck:
        raise NotImplementedError

    @abstractmethod
    def sellable_quantity(self, symbol: str) -> float:
        raise NotImplementedError
