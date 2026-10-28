"""
[v8.0] 토스증권 Open API 클라이언트.

`TossOverseas` / `TossDomestic` 은 `KisOverseas` / `KisDomestic` 과 **같은 메서드
이름·인자·응답 모양**을 갖는다. 봇·대시보드·profit_tracker 는 KIS 응답 필드
(`output1`, `rt_cd`, `dnca_tot_amt`, `ovrs_cblc_qty` …)를 200곳 넘게 읽는다. 그걸
전부 고치는 대신 이 어댑터가 토스 응답을 그 모양으로 옮긴다. 증권사 선택은
`modules.broker` 한 곳에서만 한다.

지켜야 하는 운영 규칙 (AGENTS.md):
  - 규칙 7/9: "없음"과 "실패"를 같은 값으로 반환하지 않는다.
      보유 0 → output1 == [] / 조회 실패 → None / 보유수량 실패 → None
  - 규칙 4: 시장 간 자금을 섞지 않는다. 토스 Open API 는 자동환전이 없어
      US 매수는 USD 매수가능금액만 쓴다 (FAQ: insufficient-buying-power).
  - 규칙 6: KR `tot_evlu_amt` 는 이미 예수금을 포함한 값으로 만든다.
  - 규칙 11: US 매수는 현재가 +1% 지정가. run_bot 의 1주 판정(unit_mult)과 같다.

토스 고유 제약:
  - 클라이언트당 유효 토큰은 1개. 새로 발급하면 이전 토큰이 즉시 무효화된다.
    봇과 대시보드가 각자 발급하면 서로를 끊는다 → 파일 캐시 + 파일 락으로 공유.
  - 허용 IP 에서만 호출 가능 (403 ip-not-allowed).
  - API 그룹별 초당 한도. 429 는 처리되지 않은 요청이라 재시도해도 안전하다.
"""

import contextlib
import datetime as _dt
import json
import math
import os
import threading
import time
import uuid
from collections import deque

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo

from config import TOSS_CLIENT_ID, TOSS_CLIENT_SECRET, TOSS_ACCOUNT_SEQ, TOSS_BASE_URL

_DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "database")
TOKEN_FILE = os.path.join(_DB_DIR, "toss_token_cache.json")
ACCOUNT_FILE = os.path.join(_DB_DIR, "toss_account_cache.json")
_LOCK_FILE = TOKEN_FILE + ".lock"

# 문서 한도보다 낮게 잡는다 — 봇과 대시보드가 같은 클라이언트 한도를 나눠 쓴다.
GROUP_TPS = {
    "AUTH": 2, "ACCOUNT": 1, "ASSET": 4, "STOCK": 4, "MARKET_INFO": 2,
    "MARKET_DATA": 12, "MARKET_DATA_CHART": 15, "RANKING": 4,
    "ORDER": 8, "ORDER_HISTORY": 4, "ORDER_INFO": 2,
}

_TZ = {"US": ZoneInfo("America/New_York"), "KR": ZoneInfo("Asia/Seoul")}

# 계좌 제한 계열 — run_bot 은 이 코드를 받으면 해당 시장 매매를 멈춘다.
ACCOUNT_RESTRICTED_CODES = ("account-restricted", "prerequisite-required")

# 웹소켓 personal:order 가 채우는 최신 주문 스냅샷 {orderId: order}. wait_order 가 먼저 본다.
ORDER_EVENTS = {}
_ORDER_EVENTS_LOCK = threading.Lock()
_FINAL_STATUSES = ("FILLED", "CANCELED", "REJECTED", "REPLACED")


# 웹소켓 체결 틱 {symbol: (price, monotonic)}. 신선하면 _price() 가 REST 대신 쓴다 —
# 주문 직전 현재가 조회 왕복(수십~수백 ms)을 없앤다.
LIVE_PRICES = {}
LIVE_MAX_AGE_SEC = 3.0


def record_trade(symbol, price):
    LIVE_PRICES[symbol] = (price, time.monotonic())


def live_price(symbol, max_age_sec=LIVE_MAX_AGE_SEC):
    hit = LIVE_PRICES.get(symbol)
    return hit[0] if hit and time.monotonic() - hit[1] <= max_age_sec else None


def record_order_event(order):
    """웹소켓 수신 스레드가 호출. 주문 스냅샷을 최신 상태로 덮어쓴다."""
    oid = (order or {}).get("orderId")
    if not oid:
        return
    with _ORDER_EVENTS_LOCK:
        ORDER_EVENTS[oid] = order
        if len(ORDER_EVENTS) > 500:
            for k in list(ORDER_EVENTS)[:100]:
                ORDER_EVENTS.pop(k, None)


class TossApiError(Exception):
    def __init__(self, status, code, message, request_id=None):
        super().__init__(f"[{status}] {code}: {message}")
        self.status = status
        self.code = code or "unknown"
        self.message = message or ""
        self.request_id = request_id


def _f(value, default=0.0):
    """BigDecimal(문자열/숫자/None) → float"""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_str(value):
    """KR 금액·수량 필드. 호출부가 int(str) 로 읽으므로 소수점을 남기면 안 된다."""
    return str(int(round(_f(value))))


def _num_str(value, nd=4):
    return f"{_f(value):.{nd}f}".rstrip("0").rstrip(".") if value not in (None, "") else "0"


def _floor_qty(value):
    """US 소수점 보유분은 정수 주만 매매 대상이다."""
    return int(math.floor(_f(value) + 1e-9))


def _parse_ts(ts):
    if not ts:
        return None
    s = str(ts).replace("Z", "+00:00")
    try:
        return _dt.datetime.fromisoformat(s)
    except ValueError:
        # 소수점 자릿수가 3/6 이 아닌 경우 등
        head, _, tail = s.partition(".")
        tz = ""
        for sep in ("+", "-"):
            if sep in tail:
                tz = sep + tail.split(sep, 1)[1]
                break
        return _dt.datetime.fromisoformat(head + tz)


def _bar_date(ts, market):
    """일봉 timestamp(현지 자정을 KST 로 표기) → 거래소 현지 YYYYMMDD."""
    dt = _parse_ts(ts)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    return dt.astimezone(_TZ[market]).strftime("%Y%m%d")


class _Limiter:
    def __init__(self, tps):
        self.tps = max(1, int(tps))
        self.ts = deque()
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            while self.ts and now - self.ts[0] >= 1.0:
                self.ts.popleft()
            if len(self.ts) >= self.tps:
                time.sleep(max(0.0, 1.0 - (now - self.ts[0])) + 0.01)
                now = time.monotonic()
                while self.ts and now - self.ts[0] >= 1.0:
                    self.ts.popleft()
            self.ts.append(time.monotonic())


@contextlib.contextmanager
def _file_lock(path, timeout=20.0, stale=15.0):
    """프로세스 간 락 (봇·대시보드가 토큰을 동시에 재발급하지 않도록). O_EXCL 기반이라 OS 무관."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    deadline = time.time() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > stale:
                    os.remove(path)     # 락을 쥔 프로세스가 죽었다
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise TimeoutError(f"lock timeout: {path}")
            time.sleep(0.1)
    try:
        yield
    finally:
        os.close(fd)
        try:
            os.remove(path)
        except OSError:
            pass


class TossSession:
    """프로세스당 하나. 토큰·계좌·호출 한도를 공유한다."""

    def __init__(self, base_url=None, client_id=None, client_secret=None, account_seq=None):
        self.base_url = (base_url or TOSS_BASE_URL).rstrip("/")
        self.client_id = client_id or TOSS_CLIENT_ID
        self.client_secret = client_secret or TOSS_CLIENT_SECRET
        self._env_account = account_seq or TOSS_ACCOUNT_SEQ
        self._account_seq = None
        self._token = None
        self._expiry = 0.0
        self._lock = threading.RLock()
        self._limiters = {g: _Limiter(t) for g, t in GROUP_TPS.items()}

    # ---- token -------------------------------------------------------------
    @staticmethod
    def _read_cache():
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d.get("access_token"), float(d.get("expiry", 0))
        except (OSError, ValueError):
            return None, 0.0

    @staticmethod
    def _write_cache(token, expiry):
        os.makedirs(_DB_DIR, exist_ok=True)
        tmp = TOKEN_FILE + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"access_token": token, "expiry": expiry}, f)
        os.replace(tmp, TOKEN_FILE)

    def _issue(self):
        if not self.client_id or not self.client_secret:
            raise TossApiError(0, "missing-credentials",
                               "TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 가 .env 에 없습니다")
        self._limiters["AUTH"].wait()
        res = requests.post(
            self.base_url + "/oauth2/token",
            data={"grant_type": "client_credentials",
                  "client_id": self.client_id, "client_secret": self.client_secret},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10)
        try:
            body = res.json()
        except ValueError:
            body = {}
        if res.status_code != 200 or "access_token" not in body:
            # OAuth2 표준 에러 형식 {"error", "error_description"}
            raise TossApiError(res.status_code, body.get("error") or "token-error",
                               body.get("error_description") or res.text[:200])
        self._token = body["access_token"]
        self._expiry = time.time() + int(body.get("expires_in", 0))
        self._write_cache(self._token, self._expiry)
        print(f"[TOSS] Token issued. Expires in {body.get('expires_in')}s.")

    def token(self):
        if self._token and time.time() < self._expiry - 120:
            return self._token
        with self._lock:
            tok, exp = self._read_cache()
            if tok and time.time() < exp - 120:
                self._token, self._expiry = tok, exp
                return tok
            with _file_lock(_LOCK_FILE):
                tok, exp = self._read_cache()      # 락 대기 중 다른 프로세스가 발급했을 수 있다
                if tok and time.time() < exp - 120:
                    self._token, self._expiry = tok, exp
                else:
                    self._issue()
            return self._token

    def _recover_auth(self, failed_token):
        """401 처리. 다른 프로세스가 새 토큰을 받아 우리 것이 revoke 됐다면 그걸 쓴다.
        파일의 토큰도 같은(=죽은) 토큰일 때만 재발급한다 — 서로 끊는 무한 루프 방지."""
        with self._lock:
            with _file_lock(_LOCK_FILE):
                tok, exp = self._read_cache()
                if tok and tok != failed_token and time.time() < exp - 60:
                    self._token, self._expiry = tok, exp
                else:
                    self._issue()

    # ---- account -----------------------------------------------------------
    def account_seq(self):
        if self._account_seq:
            return self._account_seq
        if self._env_account:
            self._account_seq = str(self._env_account).strip()
            return self._account_seq
        try:
            with open(ACCOUNT_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f).get("accountSeq")
            if cached:
                self._account_seq = str(cached)
                return self._account_seq
        except (OSError, ValueError):
            pass
        accounts = self.request("GET", "/api/v1/accounts", "ACCOUNT") or []
        brokerage = [a for a in accounts if a.get("accountType") in (None, "BROKERAGE")]
        if not brokerage:
            raise TossApiError(404, "account-not-found", "종합매매(BROKERAGE) 계좌가 없습니다")
        self._account_seq = str(brokerage[0]["accountSeq"])
        os.makedirs(_DB_DIR, exist_ok=True)
        with open(ACCOUNT_FILE, "w", encoding="utf-8") as f:
            json.dump({"accountSeq": self._account_seq,
                       "accountNo": brokerage[0].get("accountNo")}, f)
        return self._account_seq

    def _forget_account(self):
        self._account_seq = None
        with contextlib.suppress(OSError):
            os.remove(ACCOUNT_FILE)

    # ---- request -----------------------------------------------------------
    def request(self, method, path, group, params=None, body=None, account=False,
                idempotent=None):
        """성공 시 envelope 의 `result` 를 반환, 실패 시 TossApiError.

        idempotent: 네트워크 오류/5xx 에서 재전송해도 되는가. GET 은 기본 True.
          주문(POST)은 clientOrderId 를 실었을 때만 True — 같은 키 재요청은 첫 주문 결과를
          그대로 돌려주므로 중복 주문이 나지 않는다.
        """
        if idempotent is None:
            idempotent = method == "GET"
        last_err = None
        for attempt in range(4):
            tok = self.token()
            headers = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}
            if account:
                headers["X-Tossinvest-Account"] = self.account_seq()
            data = None
            if body is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(body)
            self._limiters.get(group, self._limiters["MARKET_DATA"]).wait()
            try:
                res = requests.request(method, self.base_url + path, params=params,
                                       data=data, headers=headers, timeout=10)
            except requests.RequestException as e:
                last_err = TossApiError(0, "network-error", str(e))
                if idempotent and attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise last_err
            try:
                payload = res.json() if res.content else {}
            except ValueError:
                payload = {}
            err = payload.get("error") if isinstance(payload, dict) else None
            code = (err or {}).get("code") if isinstance(err, dict) else None

            if res.status_code == 200:
                return payload.get("result") if isinstance(payload, dict) else payload
            if res.status_code == 429:
                # 처리되지 않은 요청이므로 주문이라도 재시도가 안전하다.
                wait = _f(res.headers.get("Retry-After"), 1.0)
                last_err = TossApiError(429, code or "rate-limit-exceeded", "rate limited")
                time.sleep(min(5.0, max(0.2, wait)) * (attempt + 1))
                continue
            if res.status_code == 401 and attempt < 2:
                self._recover_auth(tok)
                continue
            if res.status_code == 409 and code == "request-in-progress" and attempt < 3:
                time.sleep(1.0)          # 같은 clientOrderId 의 첫 요청이 아직 처리 중
                continue
            if res.status_code >= 500 and idempotent and attempt < 2:
                last_err = TossApiError(res.status_code, code or "internal-error", res.text[:200])
                time.sleep(1.0 * (attempt + 1))
                continue
            if code == "account-not-found":
                self._forget_account()
            msg = (err or {}).get("message") if isinstance(err, dict) else None
            raise TossApiError(res.status_code, code or f"http-{res.status_code}",
                               msg or res.text[:300],
                               (err or {}).get("requestId") if isinstance(err, dict) else None)
        raise last_err or TossApiError(0, "retries-exhausted", path)


_SESSION = None
_SESSION_LOCK = threading.Lock()


def session():
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                _SESSION = TossSession()
    return _SESSION


_FX = {"rate": None, "at": 0.0}
FALLBACK_USD_KRW = float(os.getenv("USD_KRW_RATE", "1450"))


def usd_krw(max_age_sec=600):
    """USD→KRW 매매기준율 (토스 환율 API, 10분 캐시). 조회 실패 시 마지막 값 → 기본값.

    표시·자산 환산 전용이다. 주문 자금 판단에는 쓰지 않는다 (AGENTS 규칙 4).
    """
    if _FX["rate"] and time.time() - _FX["at"] < max_age_sec:
        return _FX["rate"]
    try:
        r = session().request("GET", "/api/v1/exchange-rate", "MARKET_INFO",
                              params={"baseCurrency": "USD", "quoteCurrency": "KRW"}) or {}
        rate = _f(r.get("midRate")) or _f(r.get("rate"))
        if rate > 0:
            _FX.update(rate=rate, at=time.time())
            return rate
    except Exception as e:
        print(f"[TOSS] 환율 조회 실패 → {_FX['rate'] or FALLBACK_USD_KRW}: {e}")
    return _FX["rate"] or FALLBACK_USD_KRW


def stock_names(symbols):
    """{symbol: 종목명}. 실패 시 {} — 이름은 표시용이라 실패해도 매매에 영향이 없다."""
    out = {}
    symbols = [s for s in dict.fromkeys(symbols) if s]
    for i in range(0, len(symbols), 200):
        try:
            rows = session().request("GET", "/api/v1/stocks", "STOCK",
                                     params={"symbols": ",".join(symbols[i:i + 200])}) or []
            out.update({r.get("symbol"): r.get("name") for r in rows if r.get("symbol")})
        except Exception as e:
            print(f"[TOSS] 종목 정보 조회 실패: {e}")
    return out


def _order_ok(order_id, client_order_id, note="주문 접수"):
    return {"rt_cd": "0", "msg_cd": "", "msg1": note,
            "output": {"ODNO": order_id}, "order_id": order_id,
            "client_order_id": client_order_id}


def _order_fail(e):
    return {"rt_cd": "1", "msg_cd": e.code, "msg1": f"{e.message} ({e.code})".strip(),
            "http_status": e.status, "request_id": e.request_id}


class _TossMarketClient:
    """시장 공통 부분. market = 'US' | 'KR'."""

    market = None
    _tag = "TOSS"

    def __init__(self):
        self.s = session()
        self.url = self.s.base_url
        # KIS 클라이언트처럼 생성 시 인증한다 — 자격증명 오류를 세션 시작에서 드러내기 위함.
        self.s.token()

    # ---- 시세 ----------------------------------------------------------------
    def _price(self, ticker):
        live = live_price(ticker)
        if live:
            return live
        try:
            rows = self.s.request("GET", "/api/v1/prices", "MARKET_DATA",
                                  params={"symbols": ticker}) or []
            for r in rows:
                if str(r.get("symbol", "")).upper() == str(ticker).upper():
                    v = _f(r.get("lastPrice"))
                    return v if v > 0 else None
        except TossApiError as e:
            print(f"[{self._tag}] 현재가 조회 실패 [{ticker}]: {e}")
        except Exception as e:  # 세션 루프까지 전파되지 않게 (v5.1 와 동일)
            print(f"[{self._tag}] 현재가 파싱 오류 [{ticker}]: {e}")
        return None

    def get_prices(self, tickers):
        """여러 종목 현재가를 한 번에 (최대 200). {ticker: price}. 실패 시 {}."""
        tickers = [t for t in dict.fromkeys(tickers) if t]
        out = {}
        for i in range(0, len(tickers), 200):
            try:
                rows = self.s.request("GET", "/api/v1/prices", "MARKET_DATA",
                                      params={"symbols": ",".join(tickers[i:i + 200])}) or []
            except Exception as e:
                print(f"[{self._tag}] 다건 현재가 조회 실패: {e}")
                continue
            for r in rows:
                v = _f(r.get("lastPrice"))
                if v > 0:
                    out[r.get("symbol")] = v
        return out

    def _candles(self, ticker, count=200, before=None):
        params = {"symbol": ticker, "interval": "1d", "count": min(200, int(count)),
                  "adjusted": "true"}
        if before:
            params["before"] = before
        return self.s.request("GET", "/api/v1/candles", "MARKET_DATA_CHART", params=params) or {}

    def _ohlc_rows(self, ticker, count):
        """최신순 KIS 호환 일봉. 실패 시 None."""
        try:
            page = self._candles(ticker, count=count)
        except Exception as e:
            print(f"[{self._tag}] 일봉 조회 실패 [{ticker}]: {e}")
            return None
        rows = []
        for c in page.get("candles") or []:
            close = _f(c.get("closePrice"))
            if close <= 0:
                continue
            d = _bar_date(c.get("timestamp"), self.market)
            rows.append({
                "xymd": d, "stck_bsop_date": d,
                "clos": _num_str(close), "open": _num_str(c.get("openPrice") or close),
                "high": _num_str(c.get("highPrice") or close),
                "low": _num_str(c.get("lowPrice") or close),
                "tvol": _int_str(c.get("volume")),
            })
        if not rows:
            print(f"[{self._tag}] OHLC List is Empty! [{ticker}]")
        return rows

    def _history(self, ticker, bars=200, max_pages=4):
        """오래된 것 → 최신 순 [{'date','open','high','low','close'}]. 실패 시 지금까지 모은 것."""
        rows = {}
        before = None
        for _ in range(max_pages):
            try:
                page = self._candles(ticker, count=200, before=before)
            except Exception as e:
                print(f"[{self._tag}] get_daily_history 실패 [{ticker}]: {e}")
                break
            fresh = 0
            for c in page.get("candles") or []:
                d = _bar_date(c.get("timestamp"), self.market)
                close = _f(c.get("closePrice"))
                if not d or close <= 0 or d in rows:
                    continue
                rows[d] = {"date": d, "close": close,
                           "open": _f(c.get("openPrice"), close),
                           "high": _f(c.get("highPrice"), close),
                           "low": _f(c.get("lowPrice"), close)}
                fresh += 1
            before = page.get("nextBefore")
            if len(rows) >= bars or fresh == 0 or not before:
                break
        return [rows[k] for k in sorted(rows)]

    # ---- 계좌 ----------------------------------------------------------------
    def _holdings(self):
        """이 시장의 보유 종목. 실패 시 TossApiError."""
        ov = self.s.request("GET", "/api/v1/holdings", "ASSET", account=True) or {}
        return [it for it in (ov.get("items") or [])
                if (it.get("marketCountry") or ("US" if it.get("currency") == "USD" else "KR")) == self.market]

    def _buying_power(self):
        """현금 기반 매수가능금액 (미수 미발생). 실패 시 TossApiError — 0 으로 읽지 않는다."""
        cur = "USD" if self.market == "US" else "KRW"
        r = self.s.request("GET", "/api/v1/buying-power", "ORDER_INFO",
                           params={"currency": cur}, account=True) or {}
        if r.get("cashBuyingPower") is None:
            raise TossApiError(200, "missing-field", "cashBuyingPower 없음")
        return _f(r.get("cashBuyingPower"))

    def get_holding_qty(self, ticker):
        """매도가능 수량. 보유 없으면 0, **조회 실패면 None** (AGENTS 규칙 9)."""
        try:
            r = self.s.request("GET", "/api/v1/sellable-quantity", "ORDER_INFO",
                               params={"symbol": ticker}, account=True) or {}
            if r.get("sellableQuantity") is None:
                return None
            return _floor_qty(r.get("sellableQuantity"))
        except TossApiError as e:
            if e.code == "stock-not-found":
                return 0
            print(f"[{self._tag}] get_holding_qty 실패 [{ticker}]: {e}")
            return None
        except Exception as e:
            print(f"[{self._tag}] get_holding_qty 오류 [{ticker}]: {e}")
            return None

    # ---- 주문 ----------------------------------------------------------------
    def _place(self, symbol, side, order_type, qty, price=None):
        qty = int(qty)
        if qty <= 0:
            return {"rt_cd": "1", "msg_cd": "invalid-quantity", "msg1": f"수량 {qty} <= 0"}
        cid = f"sniper-{uuid.uuid4().hex[:24]}"
        body = {"clientOrderId": cid, "symbol": symbol, "side": side,
                "orderType": order_type, "quantity": str(qty), "timeInForce": "DAY"}
        if order_type == "LIMIT":
            body["price"] = str(price)
        try:
            r = self.s.request("POST", "/api/v1/orders", "ORDER", body=body,
                               account=True, idempotent=True) or {}
            oid = r.get("orderId")
            if not oid:
                return {"rt_cd": "1", "msg_cd": "missing-order-id", "msg1": "orderId 없음", "raw": r}
            print(f"[{self._tag}] 주문 접수 {side} {symbol} x{qty} {order_type}"
                  f"{' @' + str(price) if price else ''} → {oid[:12]}…")
            return _order_ok(oid, cid)
        except TossApiError as e:
            print(f"[{self._tag}] 주문 거부 {side} {symbol} x{qty}: {e} (requestId={e.request_id})")
            return _order_fail(e)
        except Exception as e:
            print(f"[{self._tag}] 주문 예외 {side} {symbol} x{qty}: {e}")
            return None

    def get_order(self, order_id):
        try:
            return self.s.request("GET", f"/api/v1/orders/{order_id}", "ORDER_HISTORY", account=True)
        except Exception as e:
            print(f"[{self._tag}] 주문 조회 실패 {order_id[:12]}…: {e}")
            return None

    def wait_order(self, order_id, timeout_sec=15.0, poll_sec=1.0):
        """주문이 종결(체결/취소/거부)될 때까지 기다린다. 웹소켓 이벤트를 먼저, 없으면 REST.

        Returns: 마지막 주문 스냅샷 (dict) 또는 None. 체결 대기 중 타임아웃이면
        status=PENDING 스냅샷이 돌아온다 — 실패가 아니다(DAY 주문은 장중 유효).
        """
        if not order_id:
            return None
        deadline = time.monotonic() + max(0.0, timeout_sec)
        last = None
        next_rest = time.monotonic() + min(poll_sec, 0.5)
        while True:
            ev = ORDER_EVENTS.get(order_id)
            if ev:
                last = ev
                if ev.get("status") in _FINAL_STATUSES:
                    return ev
            if time.monotonic() >= next_rest:
                snap = self.get_order(order_id)
                if snap:
                    last = snap
                    if snap.get("status") in _FINAL_STATUSES:
                        return snap
                next_rest = time.monotonic() + poll_sec
            if time.monotonic() >= deadline:
                return last
            time.sleep(0.05)

    def _closed_orders(self, start_date, end_date, max_pages=20):
        """기간 내 종료 주문 (KST, YYYYMMDD). 실패 시 TossApiError."""
        def iso(d):
            d = str(d)
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
        params = {"status": "CLOSED", "from": iso(start_date), "to": iso(end_date), "limit": 100}
        out = []
        for _ in range(max_pages):
            page = self.s.request("GET", "/api/v1/orders", "ORDER_HISTORY",
                                  params=params, account=True) or {}
            out += page.get("orders") or []
            if not page.get("hasNext") or not page.get("nextCursor"):
                break
            params["cursor"] = page["nextCursor"]
        cur = "USD" if self.market == "US" else "KRW"
        return [o for o in out if (o.get("currency") or cur) == cur]

    @staticmethod
    def _side_ok(order, sell_buy):
        side = order.get("side")
        return sell_buy in ("00", "", None) or (sell_buy == "01" and side == "SELL") \
            or (sell_buy == "02" and side == "BUY")


class TossOverseas(_TossMarketClient):
    """미국 주식. KisOverseas 와 같은 인터페이스. `exchange` 인자는 호환용 (토스는 심볼만 쓴다)."""

    market = "US"
    _tag = "TOSS-US"

    def get_current_price(self, ticker, exchange="NAS"):
        return self._price(ticker)

    def get_quote(self, ticker, exchange="NAS"):
        """KIS 해외 현재가상세 호환: last/open/high/low/base/tvol (오늘 봉 + 현재가)."""
        try:
            page = self._candles(ticker, count=2)
        except Exception as e:
            print(f"[{self._tag}] get_quote 실패 [{ticker}]: {e}")
            return None
        candles = page.get("candles") or []
        today = _dt.datetime.now(_TZ["US"]).strftime("%Y%m%d")
        cur = candles[0] if candles and _bar_date(candles[0].get("timestamp"), "US") == today else None
        prev = candles[1] if cur and len(candles) > 1 else (candles[0] if candles else None)
        last = self._price(ticker) or (_f(cur.get("closePrice")) if cur else 0.0)
        if not last:
            return None
        return {
            "last": last,
            "open": _f(cur.get("openPrice")) if cur else 0.0,
            "high": _f(cur.get("highPrice")) if cur else 0.0,
            "low": _f(cur.get("lowPrice")) if cur else 0.0,
            "base": _f(prev.get("closePrice")) if prev else 0.0,
            "tvol": _f(cur.get("volume")) if cur else 0.0,
        }

    def get_daily_ohlc(self, ticker, exchange="NAS", period="D", days=100):
        return self._ohlc_rows(ticker, days)

    def get_daily_history(self, ticker, exchange="NAS", bars=200, max_pages=4):
        return self._history(ticker, bars, max_pages)

    def buy_market_order(self, ticker, qty, exchange="NAS"):
        """현재가 +1% 지정가 (DAY). 시장가 효과 + 상한 보호. run_bot 의 1주 판정과 같은 단가."""
        current = self._price(ticker)
        if not current:
            return None
        price = math.floor(current * 1.01 * 100) / 100 if current >= 1 else \
            math.floor(current * 1.01 * 10000) / 10000
        return self._place(ticker, "BUY", "LIMIT", qty, price)

    def sell_market_order(self, ticker, qty, exchange="NAS"):
        """시장가 매도 — 손절은 체결이 우선이다."""
        return self._place(ticker, "SELL", "MARKET", qty)

    def get_balance(self):
        """보유 0 → {'output1': []}, 조회 실패 → None (AGENTS 규칙 7)."""
        try:
            items = self._holdings()
        except Exception as e:
            print(f"[{self._tag}] Balance check failed: {e}")
            return None
        rows = []
        tot_pl = tot_mv = tot_buy = 0.0
        for it in items:
            sym = it.get("symbol")
            q = _floor_qty(it.get("quantity"))
            mv = _f((it.get("marketValue") or {}).get("amount"))
            pl = _f((it.get("profitLoss") or {}).get("amount"))
            buy = _f((it.get("marketValue") or {}).get("purchaseAmount"))
            rate = _f((it.get("profitLoss") or {}).get("rate")) * 100
            tot_pl, tot_mv, tot_buy = tot_pl + pl, tot_mv + mv, tot_buy + buy
            rows.append({
                "ovrs_pdno": sym, "pdno": sym,
                "ovrs_item_name": it.get("name", ""), "prdt_name": it.get("name", ""),
                "ovrs_cblc_qty": str(q), "ord_psbl_qty": str(q), "ccld_qty_smtl1": str(q),
                "quantity_exact": _num_str(it.get("quantity"), 6),
                "pchs_avg_pric": _num_str(it.get("averagePurchasePrice")),
                "avg_unpr3": _num_str(it.get("averagePurchasePrice")),
                "now_pric2": _num_str(it.get("lastPrice")),
                "ovrs_now_pric1": _num_str(it.get("lastPrice")),
                "frcr_evlu_pfls_amt": _num_str(pl, 2), "evlu_pfls_amt": _num_str(pl, 2),
                "evlu_pfls_rt": _num_str(rate, 2), "evlu_pfls_rt1": _num_str(rate, 2),
                "ovrs_stck_evlu_amt": _num_str(mv, 2), "frcr_evlu_amt": _num_str(mv, 2),
                "frcr_pchs_amt1": _num_str(buy, 2),
                "ovrs_excg_cd": "", "_exchange": "",
            })
        summary = {
            "tot_evlu_pfls_amt": _num_str(tot_pl, 2),
            "ovrs_tot_pfls": _num_str(tot_pl / tot_buy * 100 if tot_buy else 0, 2),
            "frcr_pchs_amt1": _num_str(tot_buy, 2),
            "ovrs_stck_evlu_amt": _num_str(tot_mv, 2),
        }
        return {"rt_cd": "0", "output1": rows, "output2": [summary], "_failed_exchanges": []}

    def get_foreign_balance(self):
        """USD 매수가능금액. 실패 → None (현금 0 이 아니다 — AGENTS 규칙 9)."""
        try:
            usd = self._buying_power()
        except Exception as e:
            print(f"[{self._tag}] Foreign Balance check failed: {e}")
            return None
        return {"deposit": usd, "withdraw_possible": usd}

    def get_executed_orders(self, start_date, end_date, sell_buy="00"):
        """KIS TTTS3035R 호환 {'rt_cd','output':[...]} — 체결분만."""
        try:
            orders = self._closed_orders(start_date, end_date)
        except Exception as e:
            return {"rt_cd": "1", "msg1": str(e), "output": []}
        out = []
        for o in orders:
            ex = o.get("execution") or {}
            q = _f(ex.get("filledQuantity"))
            if q <= 0 or not self._side_ok(o, sell_buy):
                continue
            ts = _parse_ts(o.get("orderedAt"))
            out.append({
                "pdno": o.get("symbol"), "ovrs_pdno": o.get("symbol"), "prdt_name": o.get("symbol"),
                "odno": o.get("orderId"),
                "sll_buy_dvsn_cd": "01" if o.get("side") == "SELL" else "02",
                "sll_buy_dvsn_cd_name": "매도" if o.get("side") == "SELL" else "매수",
                "ord_dt": ts.strftime("%Y%m%d") if ts else "",
                "ord_tmd": ts.strftime("%H%M%S") if ts else "",
                "ft_ccld_qty": _num_str(q, 6), "ccld_qty": _num_str(q, 6),
                "ft_ccld_unpr3": _num_str(ex.get("averageFilledPrice")),
                "ccld_unpr": _num_str(ex.get("averageFilledPrice")),
                "ft_ccld_amt": _num_str(ex.get("filledAmount"), 2),
                "ccld_amt": _num_str(ex.get("filledAmount"), 2),
                "commission": _num_str(ex.get("commission"), 4),
            })
        return {"rt_cd": "0", "msg1": "", "output": out}


class TossDomestic(_TossMarketClient):
    """국내 주식. KisDomestic 과 같은 인터페이스."""

    market = "KR"
    _tag = "TOSS-KR"

    def get_current_price(self, ticker, exchange=None):
        return self._price(ticker)

    def get_quote(self, ticker, exchange=None):
        price = self._price(ticker)
        return {"last": price, "tvol": 0} if price else None

    def get_daily_ohlc(self, ticker, days=100):
        return self._ohlc_rows(ticker, days)

    def get_daily_history(self, ticker, bars=200, max_pages=4):
        return self._history(ticker, bars, max_pages)

    def get_balance(self):
        """KIS TTTC8434R 호환. 실패 → None.

        현금은 토스 cashBuyingPower(KRW, 미수 미발생 기준) 하나로 dnca_tot_amt /
        prvs_rcdl_excc_amt / ord_psbl_cash 를 채운다 — run_bot 이 어느 키를 먼저 읽든
        같은 '실제 원화 매수 여력'이 나온다. 현금 조회가 실패하면 잔고 전체를 실패로
        본다 (현금 0 으로 읽으면 '자금 부족'으로 기록돼 원인이 가려진다).
        """
        try:
            items = self._holdings()
            cash = self._buying_power()
        except Exception as e:
            print(f"[{self._tag}] Balance check failed: {e}")
            return None
        rows = []
        tot_pl = tot_mv = tot_buy = 0.0
        for it in items:
            q = _floor_qty(it.get("quantity"))
            mv = _f((it.get("marketValue") or {}).get("amount"))
            pl = _f((it.get("profitLoss") or {}).get("amount"))
            buy = _f((it.get("marketValue") or {}).get("purchaseAmount"))
            tot_pl, tot_mv, tot_buy = tot_pl + pl, tot_mv + mv, tot_buy + buy
            rows.append({
                "pdno": it.get("symbol"), "prdt_name": it.get("name", ""),
                "hldg_qty": str(q), "ord_psbl_qty": str(q),
                "pchs_avg_pric": _num_str(it.get("averagePurchasePrice"), 2),
                "prpr": _int_str(it.get("lastPrice")),
                "evlu_amt": _int_str(mv), "pchs_amt": _int_str(buy),
                "evlu_pfls_amt": _int_str(pl),
                "evlu_pfls_rt": _num_str(_f((it.get("profitLoss") or {}).get("rate")) * 100, 2),
            })
        summary = {
            "dnca_tot_amt": _int_str(cash),
            "prvs_rcdl_excc_amt": _int_str(cash),
            "nxdy_excc_amt": _int_str(cash),
            "ord_psbl_cash": _int_str(cash),
            "scts_evlu_amt": _int_str(tot_mv),
            "pchs_amt_smtl_amt": _int_str(tot_buy),
            "evlu_pfls_smtl_amt": _int_str(tot_pl),
            # AGENTS 규칙 6: tot_evlu_amt 는 예수금을 이미 포함한다.
            "tot_evlu_amt": _int_str(tot_mv + cash),
        }
        return {"rt_cd": "0", "msg_cd": "", "msg1": "", "output1": rows, "output2": [summary]}

    def get_executed_orders(self, start_date, end_date, sell_buy="00"):
        """KIS TTTC8001R 호환 {'rt_cd','output1':[...]} — 체결분만."""
        try:
            orders = self._closed_orders(start_date, end_date)
        except Exception as e:
            return {"rt_cd": "1", "msg1": str(e), "output1": []}
        out = []
        for o in orders:
            ex = o.get("execution") or {}
            q = _floor_qty(ex.get("filledQuantity"))
            if q <= 0 or not self._side_ok(o, sell_buy):
                continue
            ts = _parse_ts(o.get("orderedAt"))
            out.append({
                "pdno": o.get("symbol"), "prdt_name": o.get("symbol"), "odno": o.get("orderId"),
                "sll_buy_dvsn_cd": "01" if o.get("side") == "SELL" else "02",
                "sll_buy_dvsn_cd_name": "매도" if o.get("side") == "SELL" else "매수",
                "ord_dt": ts.strftime("%Y%m%d") if ts else "",
                "ord_tmd": ts.strftime("%H%M%S") if ts else "",
                "tot_ccld_qty": str(q), "ord_qty": _int_str(o.get("quantity")),
                "avg_prvs": _int_str(ex.get("averageFilledPrice")),
                "tot_ccld_amt": _int_str(ex.get("filledAmount")),
            })
        return {"rt_cd": "0", "msg1": "", "output1": out}

    def buy_market_order(self, ticker, qty):
        return self._place(ticker, "BUY", "MARKET", qty)

    def sell_market_order(self, ticker, qty):
        return self._place(ticker, "SELL", "MARKET", qty)

    def sell_limit_order(self, ticker, qty, price):
        """지정가 매도 — 시장가 실패 시 fallback. 호가단위로 내림(즉시 체결 유도)."""
        tick = self._tick_size(price)
        p = int(float(price) // tick * tick) if tick > 0 else int(price)
        return self._place(ticker, "SELL", "LIMIT", qty, p)

    @staticmethod
    def _tick_size(price):
        """KRX 호가단위 (코스피/코스닥 일반, 2023-01 개정 반영)"""
        p = float(price) if price else 0
        if p < 2000: return 1
        if p < 5000: return 5
        if p < 20000: return 10
        if p < 50000: return 50
        if p < 200000: return 100
        if p < 500000: return 500
        return 1000

    # ---- 순위 (market_scanner 호환) — v7.0 은 급등주 스캐너를 쓰지 않는다 ----
    def _rank(self, rtype, duration="1d"):
        try:
            r = self.s.request("GET", "/api/v1/rankings", "RANKING",
                               params={"type": rtype, "marketCountry": "KR", "duration": duration,
                                       "excludeInvestmentCaution": "true", "count": 100}) or {}
        except Exception as e:
            print(f"[{self._tag}] 랭킹 조회 실패 ({rtype}): {e}")
            return []
        items = r.get("rankings") or []
        names = stock_names([it.get("symbol") for it in items])
        out = []
        for it in items:
            pr = it.get("price") or {}
            out.append({
                "mksc_shrn_iscd": it.get("symbol"), "stck_shrn_iscd": it.get("symbol"),
                "hts_kor_isnm": names.get(it.get("symbol")) or it.get("symbol"),
                "stck_prpr": _int_str(pr.get("lastPrice")),
                "prdy_ctrt": _num_str(_f(pr.get("changeRate")) * 100, 2),
                "acml_vol": _int_str(it.get("tradingVolume")),
                "acml_tr_pbmn": _int_str(it.get("tradingAmount")),
                "vol_inrt": "0",   # 토스 랭킹에는 전일 대비 거래량 증가율이 없다
            })
        return out

    def get_volume_rank(self):
        return self._rank("MARKET_TRADING_VOLUME", "realtime")

    def get_fluctuation_rank(self):
        return self._rank("TOP_GAINERS", "1d")

    def get_trading_value_rank(self):
        return self._rank("MARKET_TRADING_AMOUNT", "realtime")
