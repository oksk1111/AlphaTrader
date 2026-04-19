import requests
import json
import time
import os
from config import KIS_BASE_URL, KIS_APP_KEY, KIS_APP_SECRET, KIS_CANO, KIS_ACNT_PRDT_CD
from modules.kis_api import RateLimiter

class KisDomestic:
    def __init__(self):
        self.url = KIS_BASE_URL
        self.app_key = KIS_APP_KEY
        self.app_secret = KIS_APP_SECRET
        self.acc_no_prefix = KIS_CANO
        self.acc_no_suffix = KIS_ACNT_PRDT_CD
        self.access_token = None
        self.token_expiry = 0
        
        # Share rate limiter concept or create new one
        self.limiter = RateLimiter(max_calls=15, period=1.0)
        
        self._refresh_token()

    def _refresh_token(self):
        """Access Token 발급 (파일 캐싱 적용 - kis_api.py와 공유)"""
        token_file = "database/kis_token_cache.json"
        
        # Ensure database directory exists
        os.makedirs("database", exist_ok=True)
        
        # 1. 파일에서 토큰 읽기 시도
        if os.path.exists(token_file):
            try:
                with open(token_file, "r") as f:
                    data = json.load(f)
                    if time.time() < data.get("expiry", 0) - 300:
                        self.access_token = data["access_token"]
                        self.token_expiry = data["expiry"]
                        return
            except Exception as e:
                print(f"[KIS-KR] Failed to load token cache: {e}")

        # 2. 토큰이 없거나 만료된 경우 새로 발급
        if time.time() < self.token_expiry:
             return

        path = "/oauth2/tokenP"
        headers = {"content-type": "application/json"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }
        
        try:
            res = requests.post(self.url + path, headers=headers, data=json.dumps(body))
            
            if res.status_code == 403 and "EGW00133" in res.text:
                print("[KIS-KR] Token rate limit hit. Waiting 60 seconds...")
                time.sleep(60)
                res = requests.post(self.url + path, headers=headers, data=json.dumps(body))

            if res.status_code != 200:
                print(f"[KIS-KR] Token Refresh Error: {res.status_code} {res.text}")
            
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
                print(f"[KIS-KR] Token refreshed and cached.")
            except Exception as e:
                 print(f"[KIS-KR] Failed to save token cache: {e}")
                 
        except Exception as e:
            print(f"[KIS-KR] Token refresh failed: {e}")
            raise

    def _get_headers(self, tr_id):
        self._refresh_token()
        return {
            "content-type": "application/json",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P" # 개인
        }

    def _request(self, method, path, headers=None, params=None, data=None):
        self.limiter.wait()
        try:
            res = None
            if method == "GET":
                res = requests.get(self.url + path, headers=headers, params=params)
            elif method == "POST":
                res = requests.post(self.url + path, headers=headers, data=data)
            
            if res:
                return res.json()
            return None
        except Exception as e:
            print(f"[KIS-KR] Request Exception: {e}")
            return None

    def get_current_price(self, ticker):
        """국내주식 현재가 조회 - FHKST01010100"""
        path = "/uapi/domestic-stock/v1/quotations/inquire-price"
        headers = self._get_headers("FHKST01010100")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J", # 주식, ETF, ETN
            "FID_INPUT_ISCD": ticker
        }
        
        res = self._request("GET", path, headers=headers, params=params)
        if res and res['rt_cd'] == '0':
            return float(res['output']['stck_prpr']) # 현재가
        return None

    def get_daily_ohlc(self, ticker):
        """국내주식 기간별 시세 (일봉) - FHKST01010400"""
        path = "/uapi/domestic-stock/v1/quotations/inquire-daily-price"
        headers = self._get_headers("FHKST01010400")
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "1" # 수정주가 반영
        }
        
        res = self._request("GET", path, headers=headers, params=params)
        if res and res['rt_cd'] == '0':
            # Format to match strategies expectations: [{'clos': '100', ...}]
            # API returns stck_clpr (close), stck_oprc (open), etc.
            output_list = []
            for item in res['output']:
                output_list.append({
                    'clos': item['stck_clpr'],
                    'open': item['stck_oprc'],
                    'high': item['stck_hgpr'],
                    'low': item['stck_lwpr'],
                    'tvol': item.get('acml_vol', '0')
                })
            return output_list
        return None

    def get_balance(self):
        """주식 잔고 조회 - TTTC8434R (실전/모의 구분 필요)"""
        # 실전: TTTC8434R, 모의: VTTC8434R
        tr_id = "VTTC8434R" if "openapivts" in self.url else "TTTC8434R"
        path = "/uapi/domestic-stock/v1/trading/inquire-balance"
        headers = self._get_headers(tr_id)
        
        params = {
            "CANO": self.acc_no_prefix,
            "ACNT_PRDT_CD": self.acc_no_suffix,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "N",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": ""
        }
        
        res = self._request("GET", path, headers=headers, params=params)
        return res

    def get_executed_orders(self, start_date, end_date, sell_buy="00"):
        """주식일별주문체결조회 - TTTC8001R
        
        Args:
            start_date: 조회시작일자 (YYYYMMDD)
            end_date: 조회종료일자 (YYYYMMDD)
            sell_buy: 매도매수구분 (00:전체, 01:매도, 02:매수)
        Returns:
            체결 내역 dict (output1: 개별체결, output2: 합산)
        """
        tr_id = "VTTC8001R" if "openapivts" in self.url else "TTTC8001R"
        path = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        headers = self._get_headers(tr_id)
        
        params = {
            "CANO": self.acc_no_prefix,
            "ACNT_PRDT_CD": self.acc_no_suffix,
            "INQR_STRT_DT": start_date,
            "INQR_END_DT": end_date,
            "SLL_BUY_DVSN_CD": sell_buy,
            "INQR_DVSN": "00",
            "PDNO": "",
            "CCLD_DVSN": "01",
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": ""
        }
        
        res = self._request("GET", path, headers=headers, params=params)
        return res

    def buy_market_order(self, ticker, qty):
        """국내주식 시장가 매수"""
        # 실전: TTTC0802U / 모의: VTTC0802U
        tr_id = "VTTC0802U" if "openapivts" in self.url else "TTTC0802U"
        path = "/uapi/domestic-stock/v1/trading/order-cash"
        headers = self._get_headers(tr_id)
        
        data = {
            "CANO": self.acc_no_prefix,
            "ACNT_PRDT_CD": self.acc_no_suffix,
            "PDNO": ticker,
            "ORD_DVSN": "01", # 01: 시장가
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0" # 시장가는 0
        }
        
        return self._request("POST", path, headers=headers, data=json.dumps(data))

    def sell_market_order(self, ticker, qty):
        """국내주식 시장가 매도"""
        # 실전: TTTC0801U / 모의: VTTC0801U
        tr_id = "VTTC0801U" if "openapivts" in self.url else "TTTC0801U"
        path = "/uapi/domestic-stock/v1/trading/order-cash"
        headers = self._get_headers(tr_id)
        
        data = {
            "CANO": self.acc_no_prefix,
            "ACNT_PRDT_CD": self.acc_no_suffix,
            "PDNO": ticker,
            "ORD_DVSN": "01", # 01: 시장가
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0"
        }
        
        return self._request("POST", path, headers=headers, data=json.dumps(data))

    def get_volume_rank(self):
        """거래량 급증 종목 순위 (전일 대비 급증) - FHPST01710000"""
        path = "/uapi/domestic-stock/v1/ranking/volume-surge"
        headers = self._get_headers("FHPST01710000")
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",  # 주식
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000",       # 전체
            "FID_TRGT_CLS_CODE": "0",       # 전체
            "FID_TRGT_EXLS_CLS_CODE": "0",  # 제외없음
            "FID_INPUT_PRICE_1": "",        # 가격대 상관없음
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",              # 거래량 상관없음
            "FID_INPUT_DATE_1": ""          # 오늘
        }
        
        res = self._request("GET", path, headers=headers, params=params)
        if res and res['rt_cd'] == '0':
            return res['output']
        return []

    def get_fluctuation_rank(self):
        """등락률 순위 (상승률 상위) - FHPST01700000"""
        path = "/uapi/domestic-stock/v1/ranking/fluctuation"
        headers = self._get_headers("FHPST01700000")
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20170",
            "FID_INPUT_ISCD": "0000",
            "FID_RANK_SORT_CLS_CODE": "0",  # 상승률순
            "FID_INPUT_CNT_1": "0",         # 순위 시작
            "FID_PBLC_YN": "Y",             # 상장여부
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_TRGT_EXLS_CLS_CODE": "0",
            "FID_TRGT_CLS_CODE": "0",
            "FID_JAE_GUBUN": "0",           # 제외옵션 없음
            "FID_COND_VOL_CX_CD": "0"
        }
        
        res = self._request("GET", path, headers=headers, params=params)
        if res and res['rt_cd'] == '0':
            return res['output']
        return []

    def get_trading_value_rank(self):
        """거래대금 상위 순위 (우량주 발굴용) - FHPST01780000"""
        path = "/uapi/domestic-stock/v1/ranking/trade-value"
        headers = self._get_headers("FHPST01780000")
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",  # 주식
            "FID_COND_SCR_DIV_CODE": "20178",
            "FID_INPUT_ISCD": "0000",       # 전체
            "FID_RANK_SORT_CLS_CODE": "0",  # 순위순
            "FID_INPUT_CNT_1": "0",         # 순위 시작
            "FID_PBLC_YN": "Y",             # 상장여부
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_TRGT_EXLS_CLS_CODE": "0",
            "FID_TRGT_CLS_CODE": "0",
            "FID_JAE_GUBUN": "0",
            "FID_COND_VOL_CX_CD": "0"
        }
        
        res = self._request("GET", path, headers=headers, params=params)
        if res and res['rt_cd'] == '0':
            return res['output']
        return []

