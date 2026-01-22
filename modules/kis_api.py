import requests
import json
import time
import os
from collections import deque
from config import KIS_BASE_URL, KIS_APP_KEY, KIS_APP_SECRET, KIS_CANO, KIS_ACNT_PRDT_CD

class RateLimiter:
    """
    Token Bucket / Sliding Window Rate Limiter
    Ensures we do not exceed 'max_calls' per 'period' seconds.
    """
    def __init__(self, max_calls=15, period=1.0):
        self.max_calls = max_calls
        self.period = period
        self.timestamps = deque()

    def wait(self):
        """Blocks execution if rate limit is hit"""
        now = time.time()
        
        # Remove timestamps older than the period
        while self.timestamps and now - self.timestamps[0] > self.period:
            self.timestamps.popleft()
            
        if len(self.timestamps) >= self.max_calls:
            # Calculate wait time
            wait_time = self.period - (now - self.timestamps[0])
            if wait_time > 0:
                time.sleep(wait_time)
            # Clean up again after sleeping
            now = time.time()
            while self.timestamps and now - self.timestamps[0] > self.period:
                self.timestamps.popleft()
                
        self.timestamps.append(time.time())

class KisOverseas:
    def __init__(self):
        self.url = KIS_BASE_URL
        self.app_key = KIS_APP_KEY
        self.app_secret = KIS_APP_SECRET
        self.acc_no_prefix = KIS_CANO
        self.acc_no_suffix = KIS_ACNT_PRDT_CD
        self.access_token = None
        self.token_expiry = 0
        
        # System-level Rate Limiter (Max 15 req/sec safely under 20 limit)
        self.limiter = RateLimiter(max_calls=15, period=1.0)
        
        self._refresh_token()

    def _refresh_token(self):
        """Access Token 발급 (파일 캐싱 적용)"""
        token_file = "database/kis_token_cache.json"
        
        # Ensure database directory exists
        os.makedirs("database", exist_ok=True)
        
        # 1. 파일에서 토큰 읽기 시도
        if os.path.exists(token_file):
            try:
                with open(token_file, "r") as f:
                    data = json.load(f)
                    # 만료 시간 5분 전까지 유효한 것으로 간주
                    if time.time() < data.get("expiry", 0) - 300:
                        self.access_token = data["access_token"]
                        self.token_expiry = data["expiry"]
                        # print(f"[KIS] Loaded cached token. Expires in {int(self.token_expiry - time.time())}s.")
                        return
            except Exception as e:
                print(f"[KIS] Failed to load token cache: {e}")

        # 2. 토큰이 없거나 만료된 경우 새로 발급
        path = "/oauth2/tokenP"
        headers = {"content-type": "application/json"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }
        
        try:
            res = requests.post(self.url + path, headers=headers, data=json.dumps(body))
            
            # Handle Rate Limit (1 request per minute)
            if res.status_code == 403 and "EGW00133" in res.text:
                print("[KIS] Token rate limit hit. Waiting 60 seconds...")
                time.sleep(60) # 1분 대기
                res = requests.post(self.url + path, headers=headers, data=json.dumps(body))

            if res.status_code != 200:
                print(f"[KIS] Token Refresh Error: {res.status_code} {res.text}")
            
            res.raise_for_status()
            data = res.json()
            
            self.access_token = data['access_token']
            self.token_expiry = time.time() + int(data['expires_in'])
            
            # 3. 파일에 저장
            try:
                with open(token_file, "w") as f:
                    json.dump({
                        "access_token": self.access_token,
                        "expiry": self.token_expiry
                    }, f)
                print(f"[KIS] Token refreshed and cached. Expires in {data['expires_in']} seconds.")
            except Exception as e:
                print(f"[KIS] Failed to save token cache: {e}")
                
        except Exception as e:
            print(f"[KIS] Token refresh failed: {e}")
            raise

    def _get_headers(self, tr_id):
        self._refresh_token()
        return {
            "content-type": "application/json",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id
        }

    def _request(self, method, path, headers=None, params=None, data=None):
        self.limiter.wait()
        try:
            if method == "GET":
                res = requests.get(self.url + path, headers=headers, params=params, timeout=10)
            elif method == "POST":
                res = requests.post(self.url + path, headers=headers, data=data, timeout=10)
            
            # Simple error logging
            if res.status_code != 200:
                # 404/500 etc
                pass 
            
            # Note: We rely on caller to check res['rt_cd'] usually, 
            # but raise_for_status handles HTTP errors.
            return res.json()
        except Exception as e:
            print(f"[KIS_API] Request Exception ({path}): {e}")
            return None

    def get_current_price(self, ticker, exchange="NAS"):
        """현재가 조회 (주식현재가 시세)"""
        # HHDFS76200200 : 해외주식 현재가 상세 (미국)
        tr_id = "HHDFS76200200" 
        path = "/uapi/overseas-price/v1/quotations/price"
        
        headers = self._get_headers(tr_id)
        params = {
            "AUTH": "",
            "EXCD": exchange, # NAS, NYS, AMS
            "SYMB": ticker
        }
        
        # Use Rate Limited Request
        res = self._request("GET", path, headers=headers, params=params)
        
        if res and res['rt_cd'] == '0':
            return float(res['output']['last'])
        
        if res:
            print(f"[KIS] Current Price Error: {res.get('msg1')} (Code: {res.get('msg_cd')})")
        return None

    def get_quote(self, ticker, exchange="NAS"):
        """해외주식 현재가 상세 조회 (시가, 고가, 저가 포함)"""
        path = "/uapi/overseas-price/v1/quotations/price"
        headers = self._get_headers("HHDFS00000300")
        params = {
            "AUTH": "",
            "EXCD": exchange,
            "SYMB": ticker
        }
        
        try:
            res = requests.get(self.url + path, headers=headers, params=params)
            res.raise_for_status()
            data = res.json()
            if data['rt_cd'] != '0':
                print(f"[KIS] Error getting quote: {data['msg1']}")
                return None
            return data['output'] # last, open, high, low, base, etc.
        except Exception as e:
            print(f"[KIS] Exception getting quote: {e}")
            return None

    def get_daily_ohlc(self, ticker, exchange="NAS", period="D"):
        """해외주식 기간별 시세 (일봉)"""
        # HHDFS76240000 : 해외주식 기간별시세(일/주/월/년)
        path = "/uapi/overseas-price/v1/quotations/dailyprice"
        headers = self._get_headers("HHDFS76240000")
        
        # 오늘 날짜 기준
        import datetime
        today = datetime.datetime.now().strftime("%Y%m%d")
        
        params = {
            "AUTH": "",
            "EXCD": exchange,
            "SYMB": ticker,
            "GUBN": "0", # 0:일, 1:주, 2:월
            "BYMD": today,
            "MODP": "1" # 0:수정주가미반영, 1:수정주가반영
        }
        
        try:
            res = requests.get(self.url + path, headers=headers, params=params)
            res.raise_for_status()
            data = res.json()
            if data['rt_cd'] != '0':
                print(f"[KIS] Error getting OHLC: {data['msg1']}")
                return None
            
            items = data['output2']
            if not items:
                print(f"[KIS] OHLC List is Empty! (Params: {params})")
            return items # 일별 데이터 리스트
        except Exception as e:
            print(f"[KIS] Exception getting OHLC: {e}")
            return None

    def buy_market_order(self, ticker, qty, exchange="NAS"):
        """해외주식 시장가 매수"""
        # 모의투자/실전투자 TR_ID 구분 필요
        # 실전: TTTT1002U (미국 매수 주문) / 모의: VTTT1002U
        tr_id = "VTTT1002U" if "openapivts" in self.url else "TTTT1002U"
        
        path = "/uapi/overseas-stock/v1/trading/order"
        headers = self._get_headers(tr_id)

        # [CRITICAL UPDATE] Exchange Code Mapping
        # Price/OHLC APIs use 3-char codes (NAS, AMS, NYS).
        # Order APIs require 4-char codes (NASD, AMEX, NYSE).
        order_exchange = exchange
        if exchange == "NAS":
            order_exchange = "NASD"
        elif exchange == "AMS":
            order_exchange = "AMEX"
        elif exchange == "NYS":
            order_exchange = "NYSE"
        
        data = {
            "CANO": self.acc_no_prefix,
            "ACNT_PRDT_CD": self.acc_no_suffix,
            "OVRS_EXCG_CD": order_exchange,
            "PDNO": ticker,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": "0", # 시장가는 0
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00" # 00: 지정가, 32: 시장가 (미국주식 시장가 주문은 보통 지원 안하거나 제한적일 수 있음. 여기서는 기획서대로 진행하되 확인 필요)
            # *주의*: KIS API에서 미국주식 시장가(32)가 안될 경우 지정가로 현재가보다 높게 쏘는 방식 사용해야 함.
            # 일단 기획서의 '시장가' 의도를 살려 00(지정가) + 높은 가격 or 32 시도. 
            # API 문서상 미국주식은 지정가(00), LOO(32), LOC(34) 등임. 장중 시장가는 보통 지원 안함.
            # 따라서 "최유리 지정가" 혹은 "현재가 + alpha"로 주문해야 함.
            # 여기서는 편의상 00(지정가)로 하되 가격을 0으로 두면 에러날 수 있음.
            # 기획서에는 "시장가"라고 되어있으나 API 구현상 수정 필요.
            # -> 수정: 현재가 조회 후 +1% 가격으로 지정가 주문 (시장가 효과)
        }
        
        # 현재가 조회 (Use original 3-char exchange code for Price API)
        current_price = self.get_current_price(ticker, exchange)
        if not current_price:
            return None
            
        # 매수 주문 가격 (현재가 * 1.01)
        buy_price = round(current_price * 1.01, 2)
        data["OVRS_ORD_UNPR"] = str(buy_price)
        
        try:
            res = requests.post(self.url + path, headers=headers, data=json.dumps(data))
            res.raise_for_status()
            return res.json()
        except Exception as e:
            print(f"[KIS] Order failed: {e}")
            return None

    def sell_market_order(self, ticker, qty, exchange="NAS"):
        """해외주식 시장가 매도"""
        tr_id = "VTTT1006U" if "openapivts" in self.url else "TTTT1006U"
        path = "/uapi/overseas-stock/v1/trading/order"
        headers = self._get_headers(tr_id)
        
        # 현재가 조회 (Use original 3-char exchange code)
        current_price = self.get_current_price(ticker, exchange)
        if not current_price:
            return None
            
        # 매도 주문 가격 (현재가 * 0.99) - 즉시 체결 유도
        sell_price = round(current_price * 0.99, 2)
        
        # [CRITICAL UPDATE] Exchange Code Mapping
        order_exchange = exchange
        if exchange == "NAS":
            order_exchange = "NASD"
        elif exchange == "AMS":
            order_exchange = "AMEX"
        elif exchange == "NYS":
            order_exchange = "NYSE"

        data = {
            "CANO": self.acc_no_prefix,
            "ACNT_PRDT_CD": self.acc_no_suffix,
            "OVRS_EXCG_CD": order_exchange,
            "PDNO": ticker,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": str(sell_price),
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00" 
        }
        
        try:
            res = requests.post(self.url + path, headers=headers, data=json.dumps(data))
            res.raise_for_status()
            return res.json()
        except Exception as e:
            print(f"[KIS] Sell Order failed: {e}")
            return None

    def get_balance(self):
        """잔고 조회 (전체 거래소)"""
        tr_id = "VTTS3012R" if "openapivts" in self.url else "TTTS3012R"
        path = "/uapi/overseas-stock/v1/trading/inquire-balance"
        headers = self._get_headers(tr_id)
        
        all_holdings = []
        exchanges = ["NASD", "NYSE", "AMEX"]  # 모든 거래소 조회
        
        for exch in exchanges:
            params = {
                "CANO": self.acc_no_prefix,
                "ACNT_PRDT_CD": self.acc_no_suffix,
                "OVRS_EXCG_CD": exch,
                "TR_CRCY_CD": "USD",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": ""
            }
            
            try:
                res = requests.get(self.url + path, headers=headers, params=params)
                res.raise_for_status()
                data = res.json()
                
                if data.get('rt_cd') == '0' and 'output1' in data:
                    for item in data['output1']:
                        item['_exchange'] = exch  # 거래소 정보 추가
                    all_holdings.extend(data['output1'])
            except Exception as e:
                print(f"[KIS] Balance check failed for {exch}: {e}")
        
        return {"output1": all_holdings} if all_holdings else None

    def get_foreign_balance(self):
        """외화예수금 조회 (USD) - CTRP6504R"""
        # 실전: CTRP6504R / 모의: VTTC8434R
        tr_id = "VTTC8434R" if "openapivts" in self.url else "CTRP6504R"
        path = "/uapi/domestic-stock/v1/trading/inquire-balance"
        
        headers = self._get_headers(tr_id)
        
        params = {
            "CANO": self.acc_no_prefix,
            "ACNT_PRDT_CD": self.acc_no_suffix,
            "WCRC_FRCR_DVSN_CD": "02", # 외화
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
            "INQR_DVSN": "02", # 계좌별
            "fund_sttl_icld_yn": "N",
            "fncg_amt_auto_rdpt_yn": "N",
            "prcs_dvsn": "00",
            "TR_MKET_CD": "00",
            "NATN_CD": "840", # USA
            "INQR_DVSN_CD": "00"
        }
        
        try:
            res = requests.get(self.url + path, headers=headers, params=params)
            res.raise_for_status()
            data = res.json()
            # print(f"[DEBUG] Foreign Balance Response: {data}")  # Uncomment for deep debug
            if data['rt_cd'] == '0' and 'output2' in data:
                # Find USD item
                for item in data['output2']:
                    if item['crcy_cd'].strip() == 'USD':
                        return {
                            'deposit': float(item['frcr_dncl_amt_2']), # 예수금
                            'withdraw_possible': float(item['frcr_drwg_psbl_amt_1']) # 출금가능
                        }
                # If USD not found, return raw for debugging
                return {'debug_raw': data['output2'], 'deposit': 0}
            else:
                print(f"[KIS] Foreign Balance Error: {data.get('msg1')} (Code: {data.get('msg_cd')})")
            return None
        except Exception as e:
            print(f"[KIS] Foreign Balance check failed: {e}")
            return None
