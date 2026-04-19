"""
멀티 브로커 추상 인터페이스 (Multi-Broker Abstract Interface)

모든 증권사 API는 이 인터페이스를 구현해야 합니다.
- KIS (한국투자증권): 현재 구현 완료
- 향후 추가: 키움, 삼성증권, 미래에셋 등

통일된 인터페이스로 매수/매도/잔고조회를 처리합니다.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any


class BrokerBase(ABC):
    """
    증권사 API 추상 기본 클래스
    
    모든 브로커는 이 클래스를 상속받아 구현합니다.
    US / KR 시장 모두 동일한 인터페이스로 접근 가능.
    """
    
    @property
    @abstractmethod
    def broker_name(self) -> str:
        """브로커 이름 (예: 'KIS', 'Kiwoom', 'Mirae')"""
        pass
    
    @property
    @abstractmethod
    def market(self) -> str:
        """시장 타입 ('US', 'KR', 'BOTH')"""
        pass
    
    # === 인증 ===
    
    @abstractmethod
    def authenticate(self) -> bool:
        """API 인증 (토큰 발급 등). 성공 시 True"""
        pass
    
    @abstractmethod
    def is_authenticated(self) -> bool:
        """현재 인증 상태 확인"""
        pass
    
    # === 잔고 ===
    
    @abstractmethod
    def get_balance(self) -> Optional[Dict[str, Any]]:
        """
        전체 계좌 잔고 조회
        
        Returns:
            {
                'deposit': float,           # 예수금 (원/달러)
                'total_asset': float,       # 총 평가 자산
                'total_profit': float,      # 총 평가 손익
                'total_profit_pct': float,  # 총 수익률 (%)
                'holdings': [               # 보유 종목 리스트
                    {
                        'ticker': str,
                        'name': str,
                        'quantity': int,
                        'avg_price': float,
                        'current_price': float,
                        'profit': float,
                        'profit_pct': float
                    }
                ],
                'raw': dict  # 원본 API 응답
            }
        """
        pass
    
    # === 시세 ===
    
    @abstractmethod
    def get_current_price(self, ticker: str, exchange: Optional[str] = None) -> Optional[float]:
        """
        현재가 조회
        
        Args:
            ticker: 종목 코드 (US: 'TQQQ', KR: '005930')
            exchange: 거래소 코드 (US만: 'NAS', 'AMS', 'NYS')
        
        Returns:
            현재가 (float) 또는 None
        """
        pass
    
    @abstractmethod
    def get_daily_ohlc(self, ticker: str, exchange: Optional[str] = None, days: int = 30) -> Optional[List[Dict]]:
        """
        일봉 OHLCV 데이터 조회
        
        Args:
            ticker: 종목 코드
            exchange: 거래소 코드
            days: 조회 일수
        
        Returns:
            [{'open': float, 'high': float, 'low': float, 'clos': float, 'volume': int, 'date': str}, ...]
            최신 데이터가 앞에 위치 (index 0 = 오늘/가장 최근)
        """
        pass
    
    @abstractmethod
    def get_quote(self, ticker: str, exchange: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        실시간 호가/시세 조회
        
        Returns:
            {
                'last': float,     # 현재가
                'tvol': int,       # 거래량
                'open': float,     # 시가
                'high': float,     # 고가
                'low': float,      # 저가
                'change': float,   # 전일 대비
                'change_pct': float # 등락률 (%)
            }
        """
        pass
    
    # === 주문 ===
    
    @abstractmethod
    def buy_market_order(self, ticker: str, quantity: int, exchange: Optional[str] = None) -> Optional[Dict]:
        """
        시장가 매수 주문
        
        Args:
            ticker: 종목 코드
            quantity: 매수 수량
            exchange: 거래소 코드
        
        Returns:
            {'rt_cd': '0', 'msg1': '...', 'order_no': '...'} 또는 실패 시 에러 응답
        """
        pass
    
    @abstractmethod
    def sell_market_order(self, ticker: str, quantity: int, exchange: Optional[str] = None) -> Optional[Dict]:
        """
        시장가 매도 주문
        
        Args:
            ticker: 종목 코드
            quantity: 매도 수량
            exchange: 거래소 코드
        
        Returns:
            {'rt_cd': '0', 'msg1': '...', 'order_no': '...'} 또는 실패 시 에러 응답
        """
        pass
    
    # === 유틸리티 ===
    
    def get_holding_ticker_ids(self) -> List[str]:
        """보유 종목 코드 리스트 반환"""
        balance = self.get_balance()
        if not balance or 'holdings' not in balance:
            return []
        return [h['ticker'] for h in balance['holdings'] if h.get('ticker')]
    
    def get_available_cash(self) -> float:
        """가용 예수금 반환"""
        balance = self.get_balance()
        if not balance:
            return 0.0
        return float(balance.get('deposit', 0))
    
    def __repr__(self):
        return f"<{self.__class__.__name__} broker={self.broker_name} market={self.market}>"


class KISBrokerUS(BrokerBase):
    """
    한국투자증권 (KIS) - US 시장 래퍼
    
    기존 KisOverseas 클래스를 BrokerBase 인터페이스로 래핑합니다.
    향후 KisOverseas의 내부 구현을 변경하지 않고도
    통일된 인터페이스로 접근할 수 있습니다.
    """
    
    def __init__(self):
        from modules.kis_api import KisOverseas
        self._client = KisOverseas()
    
    @property
    def broker_name(self) -> str:
        return "KIS"
    
    @property
    def market(self) -> str:
        return "US"
    
    def authenticate(self) -> bool:
        # KIS는 생성 시 자동 인증
        return True
    
    def is_authenticated(self) -> bool:
        return self._client is not None
    
    def get_balance(self) -> Optional[Dict[str, Any]]:
        raw = self._client.get_balance()
        foreign = self._client.get_foreign_balance()
        
        if not raw:
            return None
        
        deposit = float(foreign.get('deposit', 0)) if foreign else 0
        
        holdings = []
        for h in raw.get('output1', []):
            ticker_id = h.get('ovrs_pdno', '') or h.get('pdno', '')
            holdings.append({
                'ticker': ticker_id,
                'name': h.get('prdt_name', ''),
                'quantity': int(float(h.get('ccld_qty_smtl1', h.get('ord_psbl_qty', 0)))),
                'avg_price': float(h.get('frcr_pchs_amt1', 0)),
                'current_price': float(h.get('ovrs_now_pric1', 0)),
                'profit': float(h.get('evlu_pfls_amt', 0)),
                'profit_pct': float(h.get('evlu_pfls_rt', 0))
            })
        
        summary_list = raw.get('output2', [{}])
        summary: dict = summary_list[0] if isinstance(summary_list, list) and summary_list else {}
        
        return {
            'deposit': deposit,
            'total_asset': deposit,  # US는 환산 필요
            'total_profit': float(summary.get('tot_evlu_pfls_amt', 0)),
            'total_profit_pct': float(summary.get('ovrs_tot_pfls', 0)),
            'holdings': holdings,
            'raw': raw
        }
    
    def get_current_price(self, ticker: str, exchange: Optional[str] = None) -> Optional[float]:
        return self._client.get_current_price(ticker, exchange or '')
    
    def get_daily_ohlc(self, ticker: str, exchange: Optional[str] = None, days: int = 30) -> Optional[List[Dict]]:
        return self._client.get_daily_ohlc(ticker, exchange or '')
    
    def get_quote(self, ticker: str, exchange: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return self._client.get_quote(ticker, exchange or '')
    
    def buy_market_order(self, ticker: str, quantity: int, exchange: Optional[str] = None) -> Optional[Dict]:
        return self._client.buy_market_order(ticker, quantity, exchange or '')
    
    def sell_market_order(self, ticker: str, quantity: int, exchange: Optional[str] = None) -> Optional[Dict]:
        return self._client.sell_market_order(ticker, quantity, exchange or '')


class KISBrokerKR(BrokerBase):
    """
    한국투자증권 (KIS) - KR 시장 래퍼
    
    기존 KisDomestic 클래스를 BrokerBase 인터페이스로 래핑합니다.
    """
    
    def __init__(self):
        from modules.kis_domestic import KisDomestic
        self._client = KisDomestic()
    
    @property
    def broker_name(self) -> str:
        return "KIS"
    
    @property
    def market(self) -> str:
        return "KR"
    
    def authenticate(self) -> bool:
        return True
    
    def is_authenticated(self) -> bool:
        return self._client is not None
    
    def get_balance(self) -> Optional[Dict[str, Any]]:
        raw = self._client.get_balance()
        
        if not raw:
            return None
        
        summary_list = raw.get('output2', [{}])
        summary: dict = summary_list[0] if isinstance(summary_list, list) and summary_list else {}
        
        deposit = float(summary.get('dnca_tot_amt', 0))
        
        holdings = []
        for h in raw.get('output1', []):
            holdings.append({
                'ticker': h.get('pdno', ''),
                'name': h.get('prdt_name', ''),
                'quantity': int(float(h.get('hldg_qty', 0))),
                'avg_price': float(h.get('pchs_avg_pric', 0)),
                'current_price': float(h.get('prpr', 0)),
                'profit': float(h.get('evlu_pfls_amt', 0)),
                'profit_pct': float(h.get('evlu_pfls_rt', 0))
            })
        
        return {
            'deposit': deposit,
            'total_asset': float(summary.get('tot_evlu_amt', deposit)),
            'total_profit': float(summary.get('evlu_pfls_smtl_amt', 0)),
            'total_profit_pct': 0,  # KR API에 총 수익률 필드 없음
            'holdings': holdings,
            'raw': raw
        }
    
    def get_current_price(self, ticker: str, exchange: Optional[str] = None) -> Optional[float]:
        return self._client.get_current_price(ticker)
    
    def get_daily_ohlc(self, ticker: str, exchange: Optional[str] = None, days: int = 30) -> Optional[List[Dict]]:
        return self._client.get_daily_ohlc(ticker)
    
    def get_quote(self, ticker: str, exchange: Optional[str] = None) -> Optional[Dict[str, Any]]:
        # KisDomestic에 get_quote가 없으면 현재가로 대체
        price = self._client.get_current_price(ticker)
        if price:
            return {'last': price, 'tvol': 0}
        return None
    
    def buy_market_order(self, ticker: str, quantity: int, exchange: Optional[str] = None) -> Optional[Dict]:
        return self._client.buy_market_order(ticker, quantity)
    
    def sell_market_order(self, ticker: str, quantity: int, exchange: Optional[str] = None) -> Optional[Dict]:
        return self._client.sell_market_order(ticker, quantity)


def get_broker(market: str, broker_type: str = "KIS") -> BrokerBase:
    """
    브로커 팩토리 함수
    
    Args:
        market: 'US' 또는 'KR'
        broker_type: 'KIS' (향후 'KIWOOM', 'MIRAE' 등 추가)
    
    Returns:
        BrokerBase 구현체
    """
    if broker_type == "KIS":
        if market == "US":
            return KISBrokerUS()
        elif market == "KR":
            return KISBrokerKR()
    
    raise ValueError(f"지원하지 않는 브로커/시장: {broker_type}/{market}")
