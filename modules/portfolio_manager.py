import os
import json
import logging
import datetime
import pytz
from typing import List, Dict
from modules.kis_domestic import KisDomestic
from modules.kis_api import KisOverseas

try:
    # 위험조정 점수 산정에 ATR% 사용 (없으면 모멘텀만으로 폴백)
    from strategies.technical import calculate_atr_pct
except Exception:  # pragma: no cover - 방어적 임포트
    calculate_atr_pct = None

logger = logging.getLogger(__name__)

PORTFOLIO_FILE = "database/portfolio_target.json"

# === 카테고리별 가중치 ===
# ETF(코어): 1.0 → 기존 매매 사이즈 그대로 유지 (수익률 영향 없음)
# ETF_LEV(레버리지 ETF): 1.0 → 기존과 동일 (이미 별도 리스크 관리)
# ETF_SAT(테마 위성 ETF): 0.35 → 엔비디아/구글/AI 소프트웨어 등 고변동 테마는 저비중
# STOCK(개별주 새틀라이트): 0.4 → 단일 종목 변동성/이벤트 리스크 흡수 위해 저비중
CATEGORY_WEIGHTS = {
    "ETF": 1.0,
    "ETF_LEV": 1.0,
    "ETF_SAT": 0.35,
    "STOCK": 0.4,
}

# 한 계좌 통합 KR 포트폴리오 기본 오버라이드.
# 코어 ETF는 full size 로 두고, 밸류체인/소프트웨어 테마는 저비중으로만 편입한다.
KR_WEIGHT_OVERRIDES = {
    "483320": 0.50,  # ACE 엔비디아밸류체인액티브
    "483340": 0.25,  # ACE 구글밸류체인액티브
    "0041D0": 0.25,  # KODEX 미국AI소프트웨어TOP10
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
        "091160": ("KODEX 반도체", "ETF"),
        "305720": ("KODEX 2차전지산업", "ETF"),
        "364980": ("TIGER KRX2차전지K-뉴딜", "ETF"),
    },
    # 한국 상장 해외 기술 (나스닥/AI/반도체)
    # ISA 제안안 반영: 우주항공 테마는 축소하고, 반도체/전력/광통신/AI 소프트웨어로 재편.
    "KR_LISTED_US_TECH": {
        "426030": ("TIME 미국나스닥100액티브", "ETF"),
        "0015B0": ("KoAct 미국나스닥성장기업 액티브", "ETF"),
        "456600": ("TIME 글로벌AI인공지능 액티브", "ETF"),
        "0174B0": ("KoAct 글로벌AI메모리반도체 액티브", "ETF"),
        "0173Y0": ("KODEX 미국AI광통신네트워크", "ETF"),
        "487230": ("KODEX 미국AI전력핵심인프라", "ETF"),
        "381180": ("TIGER 미국필라델피아반도체나스닥", "ETF"),
        "133690": ("TIGER 미국나스닥100", "ETF"),
        # 기존 379800 라벨은 잘못 매핑되어 있었음. 실제 379800 은 KODEX 미국S&P500.
        "379800": ("KODEX 미국S&P500", "ETF"),
        "483320": ("ACE 엔비디아밸류체인액티브", "ETF_SAT"),
        "483340": ("ACE 구글밸류체인액티브", "ETF_SAT"),
        "0041D0": ("KODEX 미국AI소프트웨어TOP10", "ETF_SAT"),
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

    def get_composite_score(self, ohlc_data: List[Dict]) -> Dict:
        """[v3.0] 추세추종 복합 점수.

        단순 20일 수익률 대신 **멀티 lookback 위험조정 모멘텀 + 추세 확인**을 사용해
        '강하고 매끄럽게 상승하는 싸이클 선도 종목'을 상위로 끌어올린다.

        - blended = 0.2·R5 + 0.5·R20 + 0.3·R60  (중기 추세 강조)
        - risk_adj = blended / ATR%            (변동성 대비 효율 = 매끄러운 상승 우대)
        - 추세 보너스: 가격 > MA20(+0.3), MA20 ≥ MA60(+0.2)
        - 추세 이탈(가격 < MA20): 강등(×0.5) → 약세 종목 자동 후순위

        Returns: {score, momentum(R20), blended, atr_pct, trend_ok}
        """
        fallback = {"score": -999.0, "momentum": -999.0, "blended": -999.0,
                    "atr_pct": 0.0, "trend_ok": False}
        if not ohlc_data or len(ohlc_data) < 6:
            return fallback
        try:
            closes = [float(x.get('clos', x.get('stck_clpr', 0)) or 0) for x in ohlc_data]
            closes = [c for c in closes if c > 0]
            if len(closes) < 6:
                return fallback
            cur = closes[0]  # index 0 = 최신

            def _ret(n):
                if len(closes) > n and closes[n] > 0:
                    return (cur - closes[n]) / closes[n] * 100.0
                return None

            r5, r20, r60 = _ret(5), _ret(20), _ret(60)
            parts = [(0.2, r5), (0.5, r20), (0.3, r60)]
            wsum = sum(w for w, v in parts if v is not None)
            if wsum <= 0:
                return fallback
            blended = sum(w * v for w, v in parts if v is not None) / wsum

            ma20 = sum(closes[:20]) / min(len(closes), 20)
            ma60 = sum(closes[:60]) / min(len(closes), 60)
            trend_ok = cur > ma20
            trend_strong = cur > ma20 >= ma60

            atr_pct = 0.0
            if calculate_atr_pct is not None:
                try:
                    atr_pct = calculate_atr_pct(ohlc_data, period=14) or 0.0
                except Exception:
                    atr_pct = 0.0
            denom = max(atr_pct, 1.0)  # 0 division 방지 + 최소 변동성 가정
            risk_adj = blended / denom

            mult = 1.0
            if trend_ok:
                mult += 0.3
            if trend_strong:
                mult += 0.2
            if not trend_ok:
                mult = 0.5  # 추세 이탈 종목 강등

            score = risk_adj * mult
            return {
                "score": round(score, 4),
                "momentum": round(r20 if r20 is not None else blended, 2),
                "blended": round(blended, 2),
                "atr_pct": round(atr_pct, 2),
                "trend_ok": bool(trend_ok),
            }
        except Exception as e:
            logger.error(f"Composite score error: {e}")
            return fallback

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
            comp = self.get_composite_score(ohlc)
            evaluation_results["KR"].append({
                "code": code, "name": name,
                "momentum": comp["momentum"], "score": comp["score"],
                "trend_ok": comp["trend_ok"], "atr_pct": comp["atr_pct"],
                "category": category,
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
            comp = self.get_composite_score(ohlc)
            # type 필드는 기존 대시보드/리포트 호환을 위해 유지 (3X / 1X)
            target_type = "3X" if "3X" in name else "1X"
            evaluation_results["US"].append({
                "symbol": symbol, "name": name,
                "momentum": comp["momentum"], "score": comp["score"],
                "trend_ok": comp["trend_ok"], "atr_pct": comp["atr_pct"],
                "type": target_type,
                "category": category, "exchange": exchange,
            })

        # 위험조정 복합 점수(score) 기준 정렬 → 싸이클 선도 종목이 상위로
        evaluation_results["KR"].sort(key=lambda x: x['score'], reverse=True)
        evaluation_results["US"].sort(key=lambda x: x['score'], reverse=True)

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
        max_kr: int = None,
    ):
        """평가 결과를 기반으로 카테고리별 상위 종목을 선정하여 JSON에 저장.

        - 정렬: 위험조정 복합 점수(score) → 싸이클 선도 종목 우선.
        - ETF: 모멘텀이 ``min_etf_momentum`` 이상이면 선정 (방어적 임계).
        - 레버리지(3X) ETF / 개별주: **추세 확인(trend_ok=가격>20MA)** 통과 시에만 편입.
          → 하락추세 레버리지 ETF의 변동성 decay 손실 / 약세 개별주 자동 제외.
        - 가중치: ETF=1.0(기존 사이즈 유지), STOCK=0.4(저비중).
        - ``max_kr``: 구버전 호출 호환용 alias (max_kr_etf 로 매핑).
        """
        if max_kr is not None:
            max_kr_etf = max_kr
        results = self.evaluate_candidates()

        def _kr_entry(item):
            cat = item.get("category", "ETF")
            weight = KR_WEIGHT_OVERRIDES.get(item["code"], CATEGORY_WEIGHTS.get(cat, 1.0))
            # 코어 ETF/레버리지 ETF 는 기존 스키마(단순 코드 문자열) 유지 → 다운스트림 호환
            if cat in ("ETF", "ETF_LEV") and weight == 1.0:
                return item["code"]
            # ETF_SAT / STOCK 은 dict 로 저장하여 weight/category 운반
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
               if it.get("category", "ETF") in ("ETF", "ETF_LEV", "ETF_SAT")
                   and it["momentum"] > min_etf_momentum]
        kr_stocks = [it for it in results["KR"]
                     if it.get("category") == "STOCK"
                     and it["momentum"] >= min_stock_momentum
                     and it.get("trend_ok", True)]
        kr_selected = (
            [_kr_entry(it) for it in kr_etfs[:max_kr_etf]]
            + [_kr_entry(it) for it in kr_stocks[:max_kr_stock]]
        )

        # ===== US 선정 =====
        us_etf_1x = [it for it in results["US"]
                     if it.get("category", "ETF") == "ETF" and it.get("type") == "1X"]
        # 레버리지(3X) ETF 는 추세 확인 통과(가격>20MA) 시에만 → 하락추세 decay 손실 차단
        us_etf_3x = [it for it in results["US"]
                     if (it.get("category") == "ETF_LEV" or it.get("type") == "3X")
                     and it.get("trend_ok", True)]
        us_stocks = [it for it in results["US"]
                     if it.get("category") == "STOCK"
                     and it["momentum"] >= min_stock_momentum
                     and it.get("trend_ok", True)]

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