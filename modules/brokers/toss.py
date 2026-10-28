"""토스증권 Open API REST 어댑터.

공식 사양:
- https://openapi.tossinvest.com/openapi-docs/latest/openapi.json
- https://openapi.tossinvest.com/openapi-docs/overview.md
"""

import json
import os
import threading
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from modules.brokers.base import Broker
from modules.brokers.models import (
    AccountSnapshot, Bar, BrokerError, ExecutionMode, Holding, Market,
    OrderAck, OrderRequest, OrderSide, OrderState, OrderType, Quote,
)


BASE_URL = "https://openapi.tossinvest.com"
TOKEN_FILE = Path("database/toss_token_cache.json")
_ORDER_STATE = {
    "PENDING": OrderState.PENDING,
    "PENDING_CANCEL": OrderState.PENDING,
    "PENDING_REPLACE": OrderState.PENDING,
    "PARTIAL_FILLED": OrderState.PARTIAL_FILLED,
    "FILLED": OrderState.FILLED,
    "CANCELED": OrderState.CANCELED,
    "REJECTED": OrderState.REJECTED,
    "CANCEL_REJECTED": OrderState.REJECTED,
    "REPLACE_REJECTED": OrderState.REJECTED,
    "REPLACED": OrderState.CANCELED,
}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(Decimal(str(value))) if value not in (None, "") else default
    except Exception:
        return default


def _currency_map(value: Any) -> Dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {key.upper(): _float(amount) for key, amount in value.items() if amount is not None}


class TossBroker(Broker):
    def __init__(self, client_id: str = None, client_secret: str = None,
                 account_seq: str = None, execution_mode: str = None,
                 session: requests.Session = None):
        self.client_id = client_id or os.getenv("TOSS_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("TOSS_CLIENT_SECRET", "")
        self.account_seq = str(account_seq or os.getenv("TOSS_ACCOUNT_SEQ", "")).strip()
        mode = (execution_mode or os.getenv("EXECUTION_MODE", "shadow")).strip().lower()
        try:
            self.execution_mode = ExecutionMode(mode)
        except ValueError as exc:
            raise ValueError(f"잘못된 EXECUTION_MODE: {mode}") from exc
        if self.execution_mode == ExecutionMode.PAPER:
            raise ValueError("Toss 공식 문서에는 모의투자 서버가 없습니다. shadow 또는 live를 사용하세요")
        self.base_url = os.getenv("TOSS_BASE_URL", BASE_URL).rstrip("/")
        if self.execution_mode == ExecutionMode.LIVE and self.base_url != BASE_URL:
            raise ValueError("live 주문은 공식 Toss API URL에서만 허용합니다")
        self.session = session or requests.Session()
        self.access_token: Optional[str] = None
        self.token_expiry = 0.0
        self._token_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "TOSS"

    def _load_token(self) -> bool:
        try:
            data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            if time.time() < float(data.get("expiry", 0)) - 300:
                self.access_token = data["access_token"]
                self.token_expiry = float(data["expiry"])
                return True
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
        return False

    def _save_token(self) -> None:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = TOKEN_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "access_token": self.access_token,
            "expiry": self.token_expiry,
        }), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, TOKEN_FILE)

    def authenticate(self, force: bool = False) -> str:
        with self._token_lock:
            if not force:
                if self.access_token and time.time() < self.token_expiry - 300:
                    return self.access_token
                if self._load_token():
                    return self.access_token
            if not self.client_id or not self.client_secret:
                raise BrokerError("TOSS_CLIENT_ID/TOSS_CLIENT_SECRET이 설정되지 않았습니다",
                                  code="missing-credentials")
            try:
                response = self.session.post(
                    self.base_url + "/oauth2/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=10,
                )
            except requests.RequestException as exc:
                raise BrokerError(f"토스 토큰 발급 연결 실패: {exc}",
                                  code="network-error", retryable=True) from exc
            if response.status_code != 200:
                self._raise_api_error(response)
            data = response.json()
            self.access_token = data["access_token"]
            self.token_expiry = time.time() + int(data.get("expires_in", 0))
            self._save_token()
            return self.access_token

    def _headers(self, account: bool = False) -> Dict[str, str]:
        headers = {"Authorization": f"Bearer {self.authenticate()}"}
        if account:
            if not self.account_seq:
                self.account_seq = self._resolve_account_seq()
            headers["X-Tossinvest-Account"] = self.account_seq
        return headers

    def _resolve_account_seq(self) -> str:
        data = self._request("GET", "/api/v1/accounts", account=False)
        accounts = data.get("result") or []
        brokerage = [a for a in accounts if a.get("accountType") == "BROKERAGE"]
        if len(brokerage) != 1:
            raise BrokerError(
                "TOSS_ACCOUNT_SEQ를 지정하거나 종합매매 계좌가 정확히 하나여야 합니다",
                code="account-selection-required",
            )
        return str(brokerage[0]["accountSeq"])

    def _request(self, method: str, path: str, *, account: bool = False,
                 params: Dict[str, Any] = None, body: Dict[str, Any] = None,
                 retry_auth: bool = True) -> Dict[str, Any]:
        try:
            response = self.session.request(
                method, self.base_url + path, headers=self._headers(account),
                params=params, json=body, timeout=10,
            )
        except requests.RequestException as exc:
            raise BrokerError(f"토스 API 연결 실패 ({path}): {exc}",
                              code="network-error", retryable=True) from exc
        if response.status_code == 401 and retry_auth:
            self.authenticate(force=True)
            return self._request(method, path, account=account, params=params,
                                 body=body, retry_auth=False)
        if response.status_code == 429:
            raise BrokerError(
                "토스 API 요청 한도를 초과했습니다",
                code="rate-limit-exceeded",
                request_id=response.headers.get("X-Request-Id"),
                retryable=True,
            )
        if not 200 <= response.status_code < 300:
            self._raise_api_error(response)
        try:
            return response.json()
        except ValueError as exc:
            raise BrokerError(f"토스 API JSON 파싱 실패 ({path})",
                              code="invalid-response") from exc

    @staticmethod
    def _raise_api_error(response) -> None:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        error = payload.get("error") or payload
        code = error.get("code") or f"http-{response.status_code}"
        message = error.get("message") or "토스 API 요청 실패"
        request_id = error.get("requestId") or response.headers.get("X-Request-Id")
        retryable = response.status_code in (429, 500, 502, 503, 504)
        raise BrokerError(message, code=code, request_id=request_id, retryable=retryable)

    def _buying_power(self, currency: str) -> float:
        result = self._request(
            "GET", "/api/v1/buying-power", account=True,
            params={"currency": currency},
        ).get("result") or {}
        return _float(result.get("cashBuyingPower"))

    def account_snapshot(self) -> AccountSnapshot:
        result = self._request("GET", "/api/v1/holdings", account=True).get("result")
        if result is None:
            raise BrokerError("보유 자산 응답에 result가 없습니다", code="invalid-response")
        holdings: List[Holding] = []
        for item in result.get("items") or []:
            market = Market(item["marketCountry"])
            quantity = _float(item.get("quantity"))
            holdings.append(Holding(
                symbol=item.get("symbol", ""), name=item.get("name", ""), market=market,
                currency=item.get("currency", ""), quantity=quantity,
                sellable_quantity=quantity,
                average_price=_float(item.get("averagePurchasePrice")),
                last_price=_float(item.get("lastPrice")),
                market_value=_float((item.get("marketValue") or {}).get("amount")),
                profit_loss=_float((item.get("profitLoss") or {}).get("amount")),
                profit_rate_pct=_float((item.get("profitLoss") or {}).get("rate")) * 100.0,
            ))
        cash = {"KRW": self._buying_power("KRW"), "USD": self._buying_power("USD")}
        market_value = _currency_map((result.get("marketValue") or {}).get("amount"))
        profit_loss = _currency_map((result.get("profitLoss") or {}).get("amount"))
        return AccountSnapshot(
            account_seq=self.account_seq, cash=cash, holdings=holdings,
            market_value=market_value, profit_loss=profit_loss, raw=result,
        )

    def quote(self, symbol: str, market: Market) -> Quote:
        rows = self._request("GET", "/api/v1/prices",
                             params={"symbols": symbol}).get("result") or []
        row = next((item for item in rows if item.get("symbol") == symbol), None)
        if not row:
            raise BrokerError(f"현재가 없음: {symbol}", code="stock-not-found")
        return Quote(symbol=symbol, market=market, currency=row.get("currency", ""),
                     price=_float(row.get("lastPrice")), timestamp=row.get("timestamp"))

    def bars(self, symbol: str, market: Market, interval: str = "1d",
             count: int = 200, before: Optional[str] = None) -> List[Bar]:
        params: Dict[str, Any] = {
            "symbol": symbol, "interval": interval,
            "count": max(1, min(int(count), 200)), "adjusted": "true",
        }
        if before:
            params["before"] = before
        result = self._request("GET", "/api/v1/candles", params=params).get("result") or {}
        return [Bar(
            symbol=symbol, market=market, currency=item.get("currency", ""),
            timestamp=item["timestamp"], open=_float(item.get("openPrice")),
            high=_float(item.get("highPrice")), low=_float(item.get("lowPrice")),
            close=_float(item.get("closePrice")), volume=_float(item.get("volume")),
            interval=interval,
        ) for item in result.get("candles") or []]

    def submit_order(self, order: OrderRequest) -> OrderAck:
        order.validate()
        client_order_id = order.client_order_id or uuid.uuid4().hex[:32]
        if self.execution_mode == ExecutionMode.SHADOW:
            return OrderAck(
                order_id=f"shadow-{client_order_id}", client_order_id=client_order_id,
                state=OrderState.ACCEPTED, symbol=order.symbol,
                message="shadow mode: 실제 주문을 전송하지 않았습니다",
            )
        body: Dict[str, Any] = {
            "clientOrderId": client_order_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "orderType": order.order_type.value,
            "timeInForce": order.time_in_force,
        }
        if order.quantity is not None:
            body["quantity"] = str(order.quantity)
        if order.amount is not None:
            body["orderAmount"] = str(order.amount)
        if order.price is not None:
            body["price"] = str(order.price)
        result = self._request("POST", "/api/v1/orders", account=True, body=body).get("result") or {}
        if not result.get("orderId"):
            raise BrokerError("주문 접수 응답에 orderId가 없습니다", code="invalid-response")
        return OrderAck(order_id=result["orderId"], client_order_id=result.get("clientOrderId"),
                        state=OrderState.ACCEPTED, symbol=order.symbol, raw=result)

    @staticmethod
    def _parse_order(result: Dict[str, Any]) -> OrderAck:
        execution = result.get("execution") or {}
        return OrderAck(
            order_id=result.get("orderId", ""), client_order_id=result.get("clientOrderId"),
            state=_ORDER_STATE.get(result.get("status"), OrderState.UNKNOWN),
            symbol=result.get("symbol"), filled_quantity=_float(execution.get("filledQuantity")),
            average_filled_price=(_float(execution.get("averageFilledPrice"))
                                  if execution.get("averageFilledPrice") is not None else None),
            raw=result,
        )

    def order(self, order_id: str) -> OrderAck:
        result = self._request("GET", f"/api/v1/orders/{order_id}", account=True).get("result") or {}
        return self._parse_order(result)

    def cancel_order(self, order_id: str) -> OrderAck:
        if self.execution_mode == ExecutionMode.SHADOW:
            return OrderAck(order_id=order_id, client_order_id=None,
                            state=OrderState.CANCELED, message="shadow cancel")
        self._request("POST", f"/api/v1/orders/{order_id}/cancel", account=True, body={})
        return self.order(order_id)

    def sellable_quantity(self, symbol: str) -> float:
        result = self._request(
            "GET", "/api/v1/sellable-quantity", account=True,
            params={"symbol": symbol},
        ).get("result") or {}
        return _float(result.get("sellableQuantity"))
