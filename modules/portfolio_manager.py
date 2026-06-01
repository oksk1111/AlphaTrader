import os
import json
import logging
import datetime
import pytz
from typing import List, Dict
from modules.kis_domestic import KisDomestic
from modules.kis_api import KisOverseas

logger = logging.getLogger(__name__)

PORTFOLIO_FILE = "database/portfolio_target.json"

# === 카테고리별 가중치 ===
# ETF(코어): 1.0 → 기존 매매 사이즈 그대로 유지 (수익률 영향 없음)
# ETF_LEV(레버리지 ETF): 1.0 → 기존과 동일 (이미 별도 리스크 관리)
# STOCK(개별주 새틀라이트): 0.4 → 단일 종목 변동성/이벤트 리스크 흡수 위해 저비중
CATEGORY_WEIGHTS = {
    "ETF": 1.0,
    "ETF_LEV": 1.0,
    "STOCK": 0.4,
}

# 후보군 풀 (Pool) - KIS API 검색의 한계를 보완하기 위한 고품질/장기 우상향/미래산업 ETF 사전 리스트
# 주기적으로 이 풀에서 상위 N개를 선택하여 실제 매매 포트폴리오로 승격
# 각 항목: code -> (name, category)
ETF_CANDIDATES = {
    # 한국 기술/테마 (안정적 수익/방산/반도체 등)
    "KR_TECH_VALUE": {
        "292150": ("TIGER 코리아TOP10", "ETF"),
        "495230": ("KoAct 코리아밸류업 액티브", "ETF"),
        "0080G0": ("KODEX 방산TOP10", "ETF"),
        "0151P0": ("RISE 코리아전략산업액티브", "ETF"),
        "069500": ("KODEX 200", "ETF"),
        "305720": ("KODEX 2차전지산업", "ETF"),
        "364980": ("TIGER KRX2차전지K-뉴딜", "ETF"),
        "0193T0": ("KODEX SK하이닉스단일종목레버리지", "ETF_LEV"),
        "0193W0": ("KODEX 삼성전자단일종목레버리지", "ETF_LEV"),
    },
    # 한국 상장 해외 기술 (나스닥/AI/반도체/우주항공)
    "KR_LISTED_US_TECH": {
        "0015B0": ("KoAct 미국나스닥성장기업 액티브", "ETF"),
        "456600": ("TIME 글로벌AI인공지능 액티브", "ETF"),
        "0174B0": ("KoAct 글로벌AI메모리반도체 액티브", "ETF"),
        "0180V0": ("ACE 미국우주테크 액티브", "ETF"),
        "0173Y0": ("KODEX 미국AI광통신네트워크", "ETF"),
        "133690": ("TIGER 미국나스닥100", "ETF"),
        "379800": ("KODEX 미국엔비디아밸류체인", "ETF"),
    },
    # 한국 우량 개별주 (코어 ETF의 기회손실 보완 - LG/현대/금융/바이오 등)
    # 저비중(STOCK weight) 적용 + 일일 모멘텀 재평가로 자동 편입/삭제
    "KR_STOCK_LARGECAP": {
        "005930": ("삼성전자", "STOCK"),
        "000660": ("SK하이닉스", "STOCK"),
        "005380": ("현대차", "STOCK"),
        "000270": ("기아", "STOCK"),
        "035420": ("NAVER", "STOCK"),
        "035720": ("카카오", "STOCK"),
        "066570": ("LG전자", "STOCK"),
        "051910": ("LG화학", "STOCK"),
        "003550": ("LG", "STOCK"),
        "373220": ("LG에너지솔루션", "STOCK"),
        "207940": ("삼성바이오로직스", "STOCK"),
        "028260": ("삼성물산", "STOCK"),
        "005490": ("POSCO홀딩스", "STOCK"),
        "012450": ("한화에어로스페이스", "STOCK"),
        "105560": ("KB금융", "STOCK"),
        "055550": ("신한지주", "STOCK"),
        "086790": ("하나금융지주", "STOCK"),
        "068270": ("셀트리온", "STOCK"),
        "096770": ("SK이노베이션", "STOCK"),
        "034730": ("SK", "STOCK"),
    },
    # 달러 기반 해외 ETF 후보 (미국 직접 투자)
    "US_DIRECT": {
        "SOXL": ("SOXL (Semiconductor 3X)", "ETF_LEV"),
        "TQQQ": ("TQQQ (Nasdaq 3X)", "ETF_LEV"),
        "UPRO": ("UPRO (Nasdaq 3X)", "ETF_LEV"),
        "TECL": ("TECL (S&P500 3X)", "ETF_LEV"),
        "QQQ": ("QQQ (Nasdaq 1X)", "ETF"),
        "SMH": ("SMH (Semiconductor 1X)", "ETF"),
        "SPY": ("SPY (S&P500 1X)", "ETF"),
    },
    # 미국 우량 개별주 (대형 테크/금융/필수소비) - 저비중 STOCK
    "US_STOCK_LARGECAP": {
        "AAPL": ("Apple", "STOCK"),
        "MSFT": ("Microsoft", "STOCK"),
        "GOOGL": ("Alphabet", "STOCK"),
        "AMZN": ("Amazon", "STOCK"),
        "META": ("Meta", "STOCK"),
        "NVDA": ("NVIDIA", "STOCK"),
        "AVGO": ("Broadcom", "STOCK"),
        "AMD": ("AMD", "STOCK"),
        "TSLA": ("Tesla", "STOCK"),
        "NFLX": ("Netflix", "STOCK"),
        "COST": ("Costco", "STOCK"),
        "LLY": ("Eli Lilly", "STOCK"),
        "JPM": ("JPMorgan", "STOCK"),
        "V": ("Visa", "STOCK"),
        "TSM": ("TSMC ADR", "STOCK"),
    },
}

# US 종목 거래소 매핑 (NAS=NASDAQ, AMS=NYSE/AMEX 통합 KIS 코드)
US_EXCHANGE_MAP = {
    'TQQQ': 'NAS', 'SOXL': 'AMS', 'NVDL': 'NAS', 'TECL': 'AMS', 'FNGU': 'AMS',
    'UPRO': 'AMS', 'QQQ': 'NAS', 'SMH': 'NAS', 'SPY': 'AMS', 'SOXX': 'NAS', 'XLK': 'AMS',
}

class PortfolioManager:
    def __init__(self):
        self.kis_kr = KisDomestic()
        self.kis_us = KisOverseas()
        
    def get_momemtum_score(self, ohlc_data: List[Dict]) -> float:
        """최근 20일 데이터 기반으로 20일 수익률과 최대 낙폭을 계산하여 모멘텀 스코어 산출"""
        if not ohlc_data or len(ohlc_data) < 5:
            return -999.0
            
        try:
            # ohlc_data is ordered from present to past
            recent = ohlc_data[:20]
            current_price = float(recent[0].get('clos', recent[0].get('stck_clpr', '0')))
            past_price = float(recent[-1].get('clos', recent[-1].get('stck_clpr', '0')))
            
            if past_price == 0:
                return -999.0
            # 수익률 계산
            return (current_price - past_price) / past_price * 100.0
        except Exception as e:
            logger.error(f"Momemtum Calc Error: {e}")
            return -999.0

    def evaluate_candidates(self) -> Dict:
        """후보군의 종목들을 평가하여 모멘텀 스코어를 산출 (카테고리 정보 포함)"""
        logger.info("📊 Evaluating ETF + Stock candidates for Dynamic Portfolio...")

        evaluation_results = {"KR": [], "US": []}

        # 1. 한국 후보 (ETF + 우량주)
        kr_pool = {}
        for group in ("KR_TECH_VALUE", "KR_LISTED_US_TECH", "KR_STOCK_LARGECAP"):
            kr_pool.update(ETF_CANDIDATES.get(group, {}))
        for code, meta in kr_pool.items():
            name, category = meta if isinstance(meta, tuple) else (str(meta), "ETF")
            try:
                ohlc = self.kis_kr.get_daily_ohlc(code)
            except Exception as e:
                logger.warning(f"[Eval/KR] {code} OHLC fetch failed: {e}")
                continue
            if not ohlc:
                continue
            score = self.get_momemtum_score(ohlc)
            evaluation_results["KR"].append({
                "code": code, "name": name,
                "momentum": score, "category": category,
            })

        # 2. 미국 후보 (ETF + 우량주)
        us_pool = {}
        for group in ("US_DIRECT", "US_STOCK_LARGECAP"):
            us_pool.update(ETF_CANDIDATES.get(group, {}))
        for symbol, meta in us_pool.items():
            name, category = meta if isinstance(meta, tuple) else (str(meta), "ETF")
            exchange = US_EXCHANGE_MAP.get(symbol, 'NAS')
            try:
                ohlc = self.kis_us.get_daily_ohlc(symbol, exchange=exchange)
            except Exception as e:
                logger.warning(f"[Eval/US] {symbol} OHLC fetch failed: {e}")
                continue
            if not ohlc:
                continue
            score = self.get_momemtum_score(ohlc)
            # type 필드는 기존 대시보드/리포트 호환을 위해 유지 (3X / 1X)
            target_type = "3X" if "3X" in name else "1X"
            evaluation_results["US"].append({
                "symbol": symbol, "name": name,
                "momentum": score, "type": target_type,
                "category": category, "exchange": exchange,
            })

        # Sort by momentum
        evaluation_results["KR"].sort(key=lambda x: x['momentum'], reverse=True)
        evaluation_results["US"].sort(key=lambda x: x['momentum'], reverse=True)

        return evaluation_results

    def generate_and_save_portfolio(
        self,
        max_kr_etf: int = 10,
        max_kr_stock: int = 4,
        max_us_1x: int = 3,
        max_us_3x: int = 3,
        max_us_stock: int = 4,
        min_etf_momentum: float = -5.0,
        min_stock_momentum: float = 5.0,
    ):
        """평가 결과를 기반으로 카테고리별 상위 종목을 선정하여 JSON에 저장.

        - ETF: 모멘텀이 ``min_etf_momentum`` 이상이면 선정 (방어적 임계).
        - STOCK(개별주 새틀라이트): 모멘텀이 ``min_stock_momentum`` 이상일 때만 편입.
          → 약세 종목 자동 제외 = 일일 재평가로 동적 삭제.
        - 가중치: ETF=1.0(기존 사이즈 유지), STOCK=0.4(저비중).
        """
        results = self.evaluate_candidates()

        def _kr_entry(item):
            cat = item.get("category", "ETF")
            weight = CATEGORY_WEIGHTS.get(cat, 1.0)
            # ETF 는 기존 스키마(단순 코드 문자열) 유지 → 다운스트림 호환
            if cat in ("ETF", "ETF_LEV"):
                return item["code"]
            # STOCK 은 dict 로 저장하여 weight/category 운반
            return {
                "code": item["code"],
                "category": cat,
                "weight": weight,
            }

        def _us_entry(item):
            sym = item["symbol"]
            cat = item.get("category", "ETF")
            weight = CATEGORY_WEIGHTS.get(cat, 1.0)
            return {
                "symbol": sym,
                "exchange": item.get("exchange") or US_EXCHANGE_MAP.get(sym, 'NAS'),
                "weight": weight,
                "category": cat,
            }

        # ===== KR 선정 =====
        kr_etfs = [it for it in results["KR"]
                   if it.get("category", "ETF") in ("ETF", "ETF_LEV")
                   and it["momentum"] > min_etf_momentum]
        kr_stocks = [it for it in results["KR"]
                     if it.get("category") == "STOCK"
                     and it["momentum"] >= min_stock_momentum]
        kr_selected = (
            [_kr_entry(it) for it in kr_etfs[:max_kr_etf]]
            + [_kr_entry(it) for it in kr_stocks[:max_kr_stock]]
        )

        # ===== US 선정 =====
        us_etf_1x = [it for it in results["US"]
                     if it.get("category", "ETF") == "ETF" and it.get("type") == "1X"]
        us_etf_3x = [it for it in results["US"]
                     if it.get("category") == "ETF_LEV" or it.get("type") == "3X"]
        us_stocks = [it for it in results["US"]
                     if it.get("category") == "STOCK"
                     and it["momentum"] >= min_stock_momentum]

        us_1x_selected = [_us_entry(it) for it in us_etf_1x[:max_us_1x]]
        us_3x_selected = [_us_entry(it) for it in us_etf_3x[:max_us_3x]]
        us_stock_selected = [_us_entry(it) for it in us_stocks[:max_us_stock]]

        portfolio = {
            "updated_at": datetime.datetime.now(pytz.timezone('Asia/Seoul')).isoformat(),
            "TARGET_TICKERS_KR_1X": kr_selected,
            "TARGET_TICKERS_US_1X": us_1x_selected + us_stock_selected,
            "TARGET_TICKERS_US_3X": us_3x_selected,
            "meta": {
                "kr_eval": results["KR"],
                "us_eval": results["US"],
                "selection": {
                    "kr_etf": [it["code"] for it in kr_etfs[:max_kr_etf]],
                    "kr_stock": [it["code"] for it in kr_stocks[:max_kr_stock]],
                    "us_1x": [it["symbol"] for it in us_etf_1x[:max_us_1x]],
                    "us_3x": [it["symbol"] for it in us_etf_3x[:max_us_3x]],
                    "us_stock": [it["symbol"] for it in us_stocks[:max_us_stock]],
                },
                "thresholds": {
                    "min_etf_momentum": min_etf_momentum,
                    "min_stock_momentum": min_stock_momentum,
                },
            },
        }

        os.makedirs(os.path.dirname(PORTFOLIO_FILE), exist_ok=True)
        with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
            json.dump(portfolio, f, indent=4, ensure_ascii=False)

        logger.info(
            f"✅ Dynamic Portfolio Updated: KR_ETF={len(kr_etfs[:max_kr_etf])}, "
            f"KR_STOCK={len(kr_stocks[:max_kr_stock])}, "
            f"US_1X={len(us_1x_selected)}, US_3X={len(us_3x_selected)}, "
            f"US_STOCK={len(us_stock_selected)} → {PORTFOLIO_FILE}"
        )
        return portfolio

    @classmethod
    def load_portfolio(cls):
        """저장된 포트폴리오를 불러옵니다. 없으면 None 반환"""
        if os.path.exists(PORTFOLIO_FILE):
            with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return None