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

# 후보군 풀 (Pool) - KIS API 검색의 한계를 보완하기 위한 고품질/장기 우상향/미래산업 ETF 사전 리스트
# 주기적으로 이 풀에서 상위 N개를 선택하여 실제 매매 포트폴리오로 승격
ETF_CANDIDATES = {
    # 한국 기술/테마 (안정적 수익/방산/반도체 등)
    "KR_TECH_VALUE": {
        "292150": "TIGER 코리아TOP10",
        "495230": "KoAct 코리아밸류업 액티브",
        "0080G0": "KODEX 방산TOP10",
        "0151P0": "RISE 코리아전략산업액티브",
        "069500": "KODEX 200", 
        "305720": "KODEX 2차전지산업",
        "364980": "TIGER KRX2차전지K-뉴딜",
        "0193T0": "KODEX SK하이닉스단일종목레버리지",
        "0193W0": "KODEX 삼성전자단일종목레버리지",
    },
    # 한국 상장 해외 기술 (나스닥/AI/반도체/우주항공)
    "KR_LISTED_US_TECH": {
        "0015B0": "KoAct 미국나스닥성장기업 액티브",
        "456600": "TIME 글로벌AI인공지능 액티브",
        "0174B0": "KoAct 글로벌AI메모리반도체 액티브",
        "0180V0": "ACE 미국우주테크 액티브",
        "0173Y0": "KODEX 미국AI광통신네트워크",
        "133690": "TIGER 미국나스닥100",
        "379800": "KODEX 미국엔비디아밸류체인",
    },
    # 달러 기반 해외 ETF 후보 (미국 직접 투자)
    "US_DIRECT": {
        "SOXL": "SOXL (Semiconductor 3X)",
        "TQQQ": "TQQQ (Nasdaq 3X)",
        "UPRO": "UPRO (Nasdaq 3X)",
        "TECL": "TECL (S&P500 3X)",
        "QQQ": "QQQ (Nasdaq 1X)",
        "SMH": "SMH (Semiconductor 1X)",
        "SPY": "SPY (S&P500 1X)"
    }
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
        """후보군의 ETF들을 평가하여 최적의 포트폴리오를 선정"""
        logger.info("📊 Evaluating ETF candidates for Dynamic Portfolio...")
        
        evaluation_results = {
            "KR": [],
            "US": []
        }
        
        # 1. 한국 상장 ETF 평가
        kr_candidates = {**ETF_CANDIDATES["KR_TECH_VALUE"], **ETF_CANDIDATES["KR_LISTED_US_TECH"]}
        for code, name in kr_candidates.items():
            ohlc = self.kis_kr.get_daily_ohlc(code)
            if ohlc:
                score = self.get_momemtum_score(ohlc)
                evaluation_results["KR"].append({"code": code, "name": name, "momentum": score})
                
        # 2. 미국 직상장 ETF 평가
        for symbol, name in ETF_CANDIDATES["US_DIRECT"].items():
            exchange = US_EXCHANGE_MAP.get(symbol, 'NAS')
            ohlc = self.kis_us.get_daily_ohlc(symbol, exchange=exchange)
            if ohlc:
                score = self.get_momemtum_score(ohlc)
                target_type = "3X" if "3X" in name else "1X"
                evaluation_results["US"].append({"symbol": symbol, "name": name, "momentum": score, "type": target_type})
                
        # Sort by momentum
        evaluation_results["KR"].sort(key=lambda x: x['momentum'], reverse=True)
        evaluation_results["US"].sort(key=lambda x: x['momentum'], reverse=True)
        
        return evaluation_results

    def generate_and_save_portfolio(self, max_kr=7, max_us_1x=3, max_us_3x=3):
        """평가 결과를 기반으로 상위 종목을 선정하여 JSON에 저장"""
        results = self.evaluate_candidates()
        
        # 필터링: 모멘텀이 양수인 종목만 통과시키되 부족하면 상위 N개
        # US 종목 거래소 매핑 (NAS=NASDAQ, AMS=NYSE/AMEX 통합 KIS 코드)
        US_EXCHANGE_MAP = {
            'TQQQ': 'NAS', 'SOXL': 'AMS', 'NVDL': 'NAS', 'TECL': 'AMS', 'FNGU': 'AMS',
            'UPRO': 'AMS', 'QQQ': 'NAS', 'SMH': 'NAS', 'SPY': 'AMS', 'SOXX': 'NAS', 'XLK': 'AMS',
        }
        def _us_entry(item, weight):
            sym = item['symbol']
            return {"symbol": sym, "exchange": US_EXCHANGE_MAP.get(sym, 'NAS'), "weight": weight}

        kr_selected = [item['code'] for item in results["KR"] if item['momentum'] > -5.0][:max_kr]
        us_1x_selected = [_us_entry(item, 0.3) for item in results["US"] if item['type'] == '1X'][:max_us_1x]
        us_3x_selected = [_us_entry(item, 0.7) for item in results["US"] if item['type'] == '3X'][:max_us_3x]
        
        portfolio = {
            "updated_at": datetime.datetime.now(pytz.timezone('Asia/Seoul')).isoformat(),
            "TARGET_TICKERS_KR_1X": kr_selected,
            "TARGET_TICKERS_US_1X": us_1x_selected,
            "TARGET_TICKERS_US_3X": us_3x_selected,
            "meta": {
                "kr_eval": results["KR"],
                "us_eval": results["US"]
            }
        }
        
        os.makedirs(os.path.dirname(PORTFOLIO_FILE), exist_ok=True)
        with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
            json.dump(portfolio, f, indent=4, ensure_ascii=False)
            
        logger.info(f"✅ Dynamic Portfolio Updated and Saved to {PORTFOLIO_FILE}")
        return portfolio

    @classmethod
    def load_portfolio(cls):
        """저장된 포트폴리오를 불러옵니다. 없으면 None 반환"""
        if os.path.exists(PORTFOLIO_FILE):
            with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return None