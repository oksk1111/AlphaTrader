"""
[v8.0] run_bot / 대시보드용 호환 계층 — 토스증권 `TossBroker` 위에 얹은 얇은 어댑터.

봇·대시보드·profit_tracker 는 한국투자증권 시절 응답 필드(`output1`, `output2`, `rt_cd`,
`dnca_tot_amt`, `ovrs_cblc_qty` …)를 200곳 넘게 읽는다. 그 호출부를 한 번에 정규 모델로
옮기면 실계좌 봇의 회귀 위험이 크다. 그래서 HTTP·토큰·주문 상태는 전부 `TossBroker`
(정규 모델)가 맡고, 이 모듈은 결과를 예전 모양으로 옮기기만 한다. 새 코드는 이 모듈이 아니라
`modules.brokers` 정규 모델을 쓴다 (AGENTS v8.0).

지키는 규칙:
  - 보유 0 → output1 == [] / 조회 실패 → None / 보유수량 실패 → None (v7.0.1 규칙 7, v7.0.2 규칙 9)
  - KR `tot_evlu_amt` = 주식 평가 + 예수금 (v7.0 규칙 6). KR 금액·수량은 정수 문자열
  - US 매수 = 현재가 +1% 지정가 (v7.0.2 규칙 11), 매도 = 시장가
  - 주문 성공(rt_cd '0')은 **체결**이다 — `submit_and_wait` 가 접수와 체결을 구분하고,
    시간 안에 안 채워진 잔량은 취소한다. 접수 후 거부·취소(체결 0)는 rt_cd '1'.
  - EXECUTION_MODE=shadow 면 주문은 전송되지 않고 `shadow: True` 로 표시된다.
"""

import datetime as _dt
import math
import os
import queue
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional

from modules.brokers.models import (
    BrokerError, ExecutionMode, Market, OrderRequest, OrderSide, OrderState, OrderType,
)
from modules.brokers.toss import TossBroker
from modules.brokers.toss_stream import TossStream

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8 (서버)
    from backports.zoneinfo import ZoneInfo

ORDER_CONFIRM_SEC = 10.0
# 계좌 제한 계열 — run_bot 은 이 코드를 받으면 해당 종목 매매를 멈춘다.
ACCOUNT_RESTRICTED_CODES = ("account-restricted", "prerequisite-required")
_TZ = {"US": ZoneInfo("America/New_York"), "KR": ZoneInfo("Asia/Seoul")}

_BROKER: Optional[TossBroker] = None
_BROKER_LOCK = threading.Lock()


def broker() -> TossBroker:
    """프로세스당 하나 — 토큰·계좌·호출 한도를 공유한다."""
    global _BROKER
    with _BROKER_LOCK:
        if _BROKER is None:
            _BROKER = TossBroker()
        return _BROKER


def _f(value, default=0.0):
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _int_str(value):
    """KR 금액·수량. 호출부가 int(str) 로 읽으므로 소수점을 남기면 안 된다."""
    return str(int(round(_f(value))))


def _num_str(value, nd=4):
    return f"{_f(value):.{nd}f}".rstrip("0").rstrip(".") if value not in (None, "") else "0"


def _floor(value):
    """US 소수점 보유분은 정수 주만 매매 대상이다."""
    return int(math.floor(_f(value) + 1e-9))


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        head, _, tail = str(ts).partition(".")
        tz = next((sep + tail.split(sep, 1)[1] for sep in "+-" if sep in tail), "")
        return _dt.datetime.fromisoformat(head + tz)


def _bar_date(ts, market):
    """일봉 timestamp(현지 자정의 KST 표기) → 거래소 현지 YYYYMMDD."""
    dt = _parse_ts(ts)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ["KR"])
    return dt.astimezone(_TZ[market]).strftime("%Y%m%d")


def _result(payload):
    return (payload or {}).get("result")


def _fail(code, message, **extra):
    return {"rt_cd": "1", "msg_cd": code, "msg1": f"{message} ({code})", **extra}


_FX = {"rate": None, "at": 0.0}
FALLBACK_USD_KRW = float(os.getenv("USD_KRW_RATE", "1450"))


def usd_krw(max_age_sec=600):
    """USD→KRW 매매기준율 (토스 환율 API, 10분 캐시). 실패 시 마지막 값 → 기본값.
    표시·자산 환산 전용이다. 주문 자금 판단에는 쓰지 않는다 (AGENTS v7.0 규칙 4)."""
    if _FX["rate"] and time.time() - _FX["at"] < max_age_sec:
        return _FX["rate"]
    try:
        r = _result(broker()._request("GET", "/api/v1/exchange-rate",
                                      params={"baseCurrency": "USD", "quoteCurrency": "KRW"})) or {}
        rate = _f(r.get("midRate")) or _f(r.get("rate"))
        if rate > 0:
            _FX.update(rate=rate, at=time.time())
            return rate
    except Exception as e:
        print(f"[TOSS] 환율 조회 실패 → {_FX['rate'] or FALLBACK_USD_KRW}: {e}")
    return _FX["rate"] or FALLBACK_USD_KRW


def stock_names(symbols):
    """{symbol: 종목명}. 실패 시 {} — 표시용이라 매매에 영향이 없다."""
    out = {}
    symbols = [s for s in dict.fromkeys(symbols) if s]
    for i in range(0, len(symbols), 200):
        try:
            rows = _result(broker()._request("GET", "/api/v1/stocks",
                                             params={"symbols": ",".join(symbols[i:i + 200])})) or []
            out.update({r.get("symbol"): r.get("name") for r in rows if r.get("symbol")})
        except Exception as e:
            print(f"[TOSS] 종목 정보 조회 실패: {e}")
    return out


class _Client:
    market: Market = None
    _tag = "TOSS"

    def __init__(self):
        self.b = broker()
        self.url = self.b.base_url
        self.b.authenticate()   # 자격증명·허용 IP 오류를 세션 시작에서 드러낸다

    @property
    def _mk(self):
        return self.market.value

    # ---- 시세 ----------------------------------------------------------------
    def _price(self, ticker):
        try:
            q = self.b.quote(ticker, self.market)
            return q.price if q.price > 0 else None
        except Exception as e:   # 종목 하나의 실패가 세션을 죽이지 않게 (v5.1)
            print(f"[{self._tag}] 현재가 조회 실패 [{ticker}]: {e}")
            return None

    def _history(self, ticker, bars=200, max_pages=4):
        """오래된 것 → 최신 순 [{'date','open','high','low','close'}]. 실패 시 모은 만큼."""
        rows, before = {}, None
        for _ in range(max_pages):
            try:
                page = self.b.bars(ticker, self.market, "1d", 200, before)
            except Exception as e:
                print(f"[{self._tag}] 일봉 조회 실패 [{ticker}]: {e}")
                break
            fresh = 0
            for bar in page:
                d = _bar_date(bar.timestamp, self._mk)
                if not d or bar.close <= 0 or d in rows:
                    continue
                rows[d] = {"date": d, "close": bar.close, "open": bar.open or bar.close,
                           "high": bar.high or bar.close, "low": bar.low or bar.close}
                fresh += 1
            if len(rows) >= bars or fresh == 0 or len(page) < 200:
                break
            before = page[-1].timestamp     # 최신순 응답의 마지막 = 가장 오래된 봉 (inclusive → 중복은 위에서 거른다)
        return [rows[k] for k in sorted(rows)]

    def _ohlc_rows(self, ticker, count):
        """최신순 KIS 호환 일봉 [{'clos','open','high','low','tvol', ...}]. 실패 시 None."""
        try:
            page = self.b.bars(ticker, self.market, "1d", count)
        except Exception as e:
            print(f"[{self._tag}] 일봉 조회 실패 [{ticker}]: {e}")
            return None
        rows = [{"xymd": _bar_date(bar.timestamp, self._mk), "stck_bsop_date": _bar_date(bar.timestamp, self._mk),
                 "clos": _num_str(bar.close), "open": _num_str(bar.open or bar.close),
                 "high": _num_str(bar.high or bar.close), "low": _num_str(bar.low or bar.close),
                 "tvol": _int_str(bar.volume)} for bar in page if bar.close > 0]
        return rows

    def get_daily_ohlc(self, ticker, exchange=None, period="D", days=100):
        return self._ohlc_rows(ticker, days)

    def get_daily_history(self, ticker, exchange=None, bars=200, max_pages=4):
        return self._history(ticker, bars, max_pages)

    # ---- 계좌 ----------------------------------------------------------------
    def get_holding_qty(self, ticker):
        """매도가능 수량. 보유 없으면 0, **조회 실패면 None** (v7.0.2 규칙 9)."""
        try:
            return _floor(self.b.sellable_quantity(ticker))
        except BrokerError as e:
            if e.code == "stock-not-found":
                return 0
            print(f"[{self._tag}] get_holding_qty 실패 [{ticker}]: {e}")
        except Exception as e:
            print(f"[{self._tag}] get_holding_qty 오류 [{ticker}]: {e}")
        return None

    def _snapshot(self):
        try:
            return self.b.account_snapshot()
        except Exception as e:
            print(f"[{self._tag}] Balance check failed: {e}")
            return None

    # ---- 주문 ----------------------------------------------------------------
    def _place(self, symbol, side, order_type, qty, price=None):
        qty = int(qty)
        if qty <= 0:
            return _fail("invalid-quantity", f"수량 {qty} <= 0")
        try:
            ack = self.b.submit_and_wait(OrderRequest(
                symbol=symbol, market=self.market, side=side, order_type=order_type,
                quantity=qty, price=price), timeout_sec=ORDER_CONFIRM_SEC)
        except BrokerError as e:
            print(f"[{self._tag}] 주문 거부 {side.value} {symbol} x{qty}: {e} ({e.code}, requestId={e.request_id})")
            return _fail(e.code, str(e), request_id=e.request_id)
        except Exception as e:
            print(f"[{self._tag}] 주문 예외 {side.value} {symbol} x{qty}: {e}")
            return None
        shadow = self.b.execution_mode == ExecutionMode.SHADOW
        filled = qty if shadow else _floor(ack.filled_quantity)
        if not shadow and filled <= 0:
            # 접수됐지만 거부·취소(시간 내 미체결 → 잔량 취소 포함). 체결로 세지 않는다.
            return _fail(ack.state.value.lower(), f"주문 {ack.state.value} (체결 0)", order_id=ack.order_id)
        note = ("SHADOW — 주문 미전송" if shadow else
                f"{ack.state.value} {filled}/{qty}주 @ {ack.average_filled_price}")
        print(f"[{self._tag}] {side.value} {symbol} {note}")
        return {"rt_cd": "0", "msg_cd": "", "msg1": note, "output": {"ODNO": ack.order_id},
                "order_id": ack.order_id, "filled_qty": filled,
                "avg_price": ack.average_filled_price, "shadow": shadow,
                "partial": not shadow and ack.state != OrderState.FILLED}

    def _closed_orders(self, start_date, end_date, max_pages=20):
        def iso(d):
            d = str(d)
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
        params = {"status": "CLOSED", "from": iso(start_date), "to": iso(end_date), "limit": 100}
        out = []
        for _ in range(max_pages):
            page = _result(self.b._request("GET", "/api/v1/orders", account=True, params=params)) or {}
            out += page.get("orders") or []
            if not page.get("hasNext") or not page.get("nextCursor"):
                break
            params["cursor"] = page["nextCursor"]
        cur = "USD" if self.market == Market.US else "KRW"
        return [o for o in out if (o.get("currency") or cur) == cur]

    def _executed(self, start_date, end_date, sell_buy):
        rows = []
        for o in self._closed_orders(start_date, end_date):
            ex = o.get("execution") or {}
            if _f(ex.get("filledQuantity")) <= 0:
                continue
            if sell_buy == "01" and o.get("side") != "SELL" or sell_buy == "02" and o.get("side") != "BUY":
                continue
            ts = _parse_ts(o.get("orderedAt"))
            rows.append((o, ex, ts))
        return rows


class TossOverseas(_Client):
    """미국 주식. `exchange` 인자는 호환용 (토스 주문은 심볼만 쓴다)."""
    market = Market.US
    _tag = "TOSS-US"

    def get_current_price(self, ticker, exchange="NAS"):
        return self._price(ticker)

    def get_quote(self, ticker, exchange="NAS"):
        """last/open/high/low/base/tvol — 오늘 봉 + 현재가."""
        try:
            page = self.b.bars(ticker, self.market, "1d", 2)
        except Exception as e:
            print(f"[{self._tag}] get_quote 실패 [{ticker}]: {e}")
            return None
        today = _dt.datetime.now(_TZ["US"]).strftime("%Y%m%d")
        cur = page[0] if page and _bar_date(page[0].timestamp, "US") == today else None
        prev = page[1] if cur and len(page) > 1 else (page[0] if page else None)
        last = self._price(ticker) or (cur.close if cur else 0.0)
        if not last:
            return None
        return {"last": last, "open": cur.open if cur else 0.0, "high": cur.high if cur else 0.0,
                "low": cur.low if cur else 0.0, "base": prev.close if prev else 0.0,
                "tvol": cur.volume if cur else 0.0}

    def buy_market_order(self, ticker, qty, exchange="NAS"):
        """현재가 +1% 지정가 — 시장가 효과 + 상한 보호 (run_bot 1주 판정과 같은 단가)."""
        current = self._price(ticker)
        if not current:
            return None
        scale = 100 if current >= 1 else 10000
        return self._place(ticker, OrderSide.BUY, OrderType.LIMIT, qty,
                           math.floor(current * 1.01 * scale) / scale)

    def sell_market_order(self, ticker, qty, exchange="NAS"):
        return self._place(ticker, OrderSide.SELL, OrderType.MARKET, qty)

    def get_balance(self):
        """보유 0 → {'output1': []}, 조회 실패 → None (v7.0.1 규칙 7)."""
        snap = self._snapshot()
        if snap is None:
            return None
        rows, pl, mv, buy = [], 0.0, 0.0, 0.0
        for h in snap.holdings_for(self.market):
            q = _floor(h.quantity)
            cost = h.average_price * h.quantity
            pl, mv, buy = pl + h.profit_loss, mv + h.market_value, buy + cost
            rows.append({
                "ovrs_pdno": h.symbol, "pdno": h.symbol, "ovrs_item_name": h.name, "prdt_name": h.name,
                "ovrs_cblc_qty": str(q), "ord_psbl_qty": str(q), "ccld_qty_smtl1": str(q),
                "pchs_avg_pric": _num_str(h.average_price), "avg_unpr3": _num_str(h.average_price),
                "now_pric2": _num_str(h.last_price), "ovrs_now_pric1": _num_str(h.last_price),
                "frcr_evlu_pfls_amt": _num_str(h.profit_loss, 2), "evlu_pfls_amt": _num_str(h.profit_loss, 2),
                "evlu_pfls_rt": _num_str(h.profit_rate_pct, 2), "evlu_pfls_rt1": _num_str(h.profit_rate_pct, 2),
                "ovrs_stck_evlu_amt": _num_str(h.market_value, 2), "frcr_evlu_amt": _num_str(h.market_value, 2),
                "frcr_pchs_amt1": _num_str(cost, 2), "ovrs_excg_cd": "", "_exchange": "",
            })
        return {"rt_cd": "0", "output1": rows, "_failed_exchanges": [], "output2": [{
            "tot_evlu_pfls_amt": _num_str(pl, 2), "ovrs_stck_evlu_amt": _num_str(mv, 2),
            "ovrs_tot_pfls": _num_str(pl / buy * 100 if buy else 0, 2), "frcr_pchs_amt1": _num_str(buy, 2),
            "ovrs_ord_psbl_amt": _num_str(snap.cash_for(self.market), 2)}]}

    def get_foreign_balance(self):
        """USD 매수가능금액. 실패 → None (현금 0 이 아니다 — v7.0.2 규칙 9)."""
        try:
            usd = self.b._buying_power("USD")
        except Exception as e:
            print(f"[{self._tag}] Foreign Balance check failed: {e}")
            return None
        return {"deposit": usd, "withdraw_possible": usd}

    def get_executed_orders(self, start_date, end_date, sell_buy="00"):
        try:
            rows = self._executed(start_date, end_date, sell_buy)
        except Exception as e:
            return {"rt_cd": "1", "msg1": str(e), "output": []}
        return {"rt_cd": "0", "msg1": "", "output": [{
            "pdno": o.get("symbol"), "ovrs_pdno": o.get("symbol"), "prdt_name": o.get("symbol"),
            "odno": o.get("orderId"), "sll_buy_dvsn_cd": "01" if o.get("side") == "SELL" else "02",
            "sll_buy_dvsn_cd_name": "매도" if o.get("side") == "SELL" else "매수",
            "ord_dt": ts.strftime("%Y%m%d") if ts else "", "ord_tmd": ts.strftime("%H%M%S") if ts else "",
            "ft_ccld_qty": _num_str(ex.get("filledQuantity"), 6), "ccld_qty": _num_str(ex.get("filledQuantity"), 6),
            "ft_ccld_unpr3": _num_str(ex.get("averageFilledPrice")), "ccld_unpr": _num_str(ex.get("averageFilledPrice")),
            "ft_ccld_amt": _num_str(ex.get("filledAmount"), 2), "ccld_amt": _num_str(ex.get("filledAmount"), 2),
        } for o, ex, ts in rows]}


class TossDomestic(_Client):
    """국내 주식."""
    market = Market.KR
    _tag = "TOSS-KR"

    def get_current_price(self, ticker, exchange=None):
        return self._price(ticker)

    def get_quote(self, ticker, exchange=None):
        price = self._price(ticker)
        return {"last": price, "tvol": 0} if price else None

    def buy_market_order(self, ticker, qty, exchange=None):
        return self._place(ticker, OrderSide.BUY, OrderType.MARKET, qty)

    def sell_market_order(self, ticker, qty, exchange=None):
        return self._place(ticker, OrderSide.SELL, OrderType.MARKET, qty)

    def sell_limit_order(self, ticker, qty, price):
        """지정가 매도 — 시장가 실패 시 fallback. 호가단위로 내림(즉시 체결 유도)."""
        tick = self._tick_size(price)
        return self._place(ticker, OrderSide.SELL, OrderType.LIMIT, qty, int(float(price) // tick * tick))

    @staticmethod
    def _tick_size(price):
        """KRX 호가단위 (코스피/코스닥 일반, 2023-01 개정 반영)"""
        p = float(price) if price else 0
        for limit, tick in ((2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500)):
            if p < limit:
                return tick
        return 1000

    def get_balance(self):
        """KIS TTTC8434R 호환. 실패 → None. 현금 = 토스 KRW 매수가능금액(미수 미발생)."""
        snap = self._snapshot()
        if snap is None:
            return None
        cash = snap.cash_for(self.market)
        rows, pl, mv, buy = [], 0.0, 0.0, 0.0
        for h in snap.holdings_for(self.market):
            cost = h.average_price * h.quantity
            pl, mv, buy = pl + h.profit_loss, mv + h.market_value, buy + cost
            rows.append({
                "pdno": h.symbol, "prdt_name": h.name, "hldg_qty": str(_floor(h.quantity)),
                "ord_psbl_qty": str(_floor(h.sellable_quantity)),
                "pchs_avg_pric": _num_str(h.average_price, 2), "prpr": _int_str(h.last_price),
                "evlu_amt": _int_str(h.market_value), "pchs_amt": _int_str(cost),
                "evlu_pfls_amt": _int_str(h.profit_loss), "evlu_pfls_rt": _num_str(h.profit_rate_pct, 2),
            })
        return {"rt_cd": "0", "msg_cd": "", "msg1": "", "output1": rows, "output2": [{
            "dnca_tot_amt": _int_str(cash), "prvs_rcdl_excc_amt": _int_str(cash),
            "nxdy_excc_amt": _int_str(cash), "ord_psbl_cash": _int_str(cash),
            "scts_evlu_amt": _int_str(mv), "pchs_amt_smtl_amt": _int_str(buy),
            "evlu_pfls_smtl_amt": _int_str(pl),
            "tot_evlu_amt": _int_str(mv + cash),    # v7.0 규칙 6: 예수금 포함
        }]}

    def get_executed_orders(self, start_date, end_date, sell_buy="00"):
        try:
            rows = self._executed(start_date, end_date, sell_buy)
        except Exception as e:
            return {"rt_cd": "1", "msg1": str(e), "output1": []}
        return {"rt_cd": "0", "msg1": "", "output1": [{
            "pdno": o.get("symbol"), "prdt_name": o.get("symbol"), "odno": o.get("orderId"),
            "sll_buy_dvsn_cd": "01" if o.get("side") == "SELL" else "02",
            "sll_buy_dvsn_cd_name": "매도" if o.get("side") == "SELL" else "매수",
            "ord_dt": ts.strftime("%Y%m%d") if ts else "", "ord_tmd": ts.strftime("%H%M%S") if ts else "",
            "tot_ccld_qty": str(_floor(ex.get("filledQuantity"))), "ord_qty": _int_str(o.get("quantity")),
            "avg_prvs": _int_str(ex.get("averageFilledPrice")), "tot_ccld_amt": _int_str(ex.get("filledAmount")),
        } for o, ex, ts in rows]}

    # ---- 순위 (market_scanner 호환) — v7.0 로테이션은 스캐너를 쓰지 않는다 ----
    def _rank(self, rtype, duration):
        try:
            r = _result(self.b._request("GET", "/api/v1/rankings", params={
                "type": rtype, "marketCountry": "KR", "duration": duration,
                "excludeInvestmentCaution": "true", "count": 100})) or {}
        except Exception as e:
            print(f"[{self._tag}] 랭킹 조회 실패 ({rtype}): {e}")
            return []
        items = r.get("rankings") or []
        names = stock_names([it.get("symbol") for it in items])
        return [{
            "mksc_shrn_iscd": it.get("symbol"), "stck_shrn_iscd": it.get("symbol"),
            "hts_kor_isnm": names.get(it.get("symbol")) or it.get("symbol"),
            "stck_prpr": _int_str((it.get("price") or {}).get("lastPrice")),
            "prdy_ctrt": _num_str(_f((it.get("price") or {}).get("changeRate")) * 100, 2),
            "acml_vol": _int_str(it.get("tradingVolume")), "acml_tr_pbmn": _int_str(it.get("tradingAmount")),
            "vol_inrt": "0",   # 토스 랭킹에는 전일 대비 거래량 증가율이 없다
        } for it in items]

    def get_volume_rank(self):
        return self._rank("MARKET_TRADING_VOLUME", "realtime")

    def get_fluctuation_rank(self):
        return self._rank("TOP_GAINERS", "1d")

    def get_trading_value_rank(self):
        return self._rank("MARKET_TRADING_AMOUNT", "realtime")


class LiveFeed:
    """세션 동안의 토스 실시간 스트림 (체결 틱·호가·본인 주문 이벤트).

    가격·주문 캐시는 수신부(`toss_stream.remember`)가 채우고, `TossBroker.quote()` /
    `submit_and_wait()` 가 그걸 먼저 본다 — 주문 직전 시세 왕복과 체결 폴링 지연을 없앤다.
    여기의 소비 스레드는 큐를 비우고 가격 콜백만 부른다. **주문하지 않는다** (v8.0 규칙).
    """

    def __init__(self, market: str, codes: Iterable[str],
                 on_price: Optional[Callable[[str, float], None]] = None):
        b = broker()
        if not b.account_seq:
            b.account_seq = b._resolve_account_seq()
        mk = Market(market.upper())
        self.stream = TossStream(b.authenticate, [(mk, c) for c in dict.fromkeys(codes) if c], b.account_seq)
        self.on_price = on_price
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._drain, daemon=True, name=f"TossFeed-{mk.value}")

    def start(self):
        self.stream.start()
        self._thread.start()
        return self

    def _drain(self):
        while not self._stop.is_set():
            try:
                event = self.stream.events.get(timeout=1.0)
            except queue.Empty:
                continue
            if event.kind == "trade" and self.on_price:
                try:
                    self.on_price(event.symbol, float(event.data.get("price") or 0))
                except Exception:
                    pass

    @property
    def connected(self):
        return self.stream.connected

    def stop(self):
        self._stop.set()
        self.stream.stop()
