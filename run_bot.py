import time
import datetime
import schedule
import sys
import json
import os
import traceback
import pytz
from modules.kis_api import KisOverseas
from modules.kis_domestic import KisDomestic
from modules.gemini_analyst import GeminiAnalyst
from modules.logger import logger
from modules.telegram_notifier import TelegramNotifier
from strategies.technical import (
    calculate_ma, calculate_short_ma, check_trend, check_volume_spike,
    check_gap_down, check_consecutive_decline, check_portfolio_drawdown,
    calculate_atr_pct
)
from strategies.volatility_breakout import calculate_target_price
from modules.account_manager import update_all_accounts
from modules.market_scanner import scanner  # New scanner module
from modules.multi_llm import MultiLLMAnalyst
from modules.auto_strategy import AutoStrategyOptimizer
from modules import trade_journal

def safe_float(value, default=0.0):
    """빈 문자열이나 None을 안전하게 float로 변환"""
    try:
        if value is None or value == '':
            return default
        return float(value)
    except (ValueError, TypeError):
        return default

# Configuration
CONFIG_FILE = "user_config.json"
MAX_RETRIES_PER_TICKER = 3  # Maximum buy retries per ticker per session
FAILED_TICKERS = set()  # Track permanently failed tickers (account restrictions)
LEVERAGE_THRESHOLD_KRW = 10_000_000  # 1000만원 기준 (KR 시장만 적용)
DYNAMIC_TARGETS = [] # Found by scanner

# === Risk Management Constants ===
STOP_LOSS_PCT = -3.0          # 손절매 기준 (%)
TRAILING_STOP_ACTIVATION = 3.0 # 트레일링 스탑 활성화 기준 (%)
TRAILING_STOP_DROP = 1.5       # 고점 대비 하락 기준 (%)
GAP_DOWN_THRESHOLD = 3.0       # 갭다운 매수 차단 기준 (%)
CONSECUTIVE_DECLINE_DAYS = 2   # 연속 하락 확인 일수
CONSECUTIVE_DECLINE_PCT = 3.0  # 연속 하락 누적 기준 (%)
PORTFOLIO_DRAWDOWN_PCT = 5.0   # 포트폴리오 전체 드로다운 차단 기준 (%)
DCA_MONITOR_INTERVAL = 60      # DCA 감시 루프 주기 (초)
DCA_REENTRY_INTERVAL_MIN = 15  # DCA 재평가 주기 (분)
DCA_LEVERAGED_REENTRY_INTERVAL_MIN = 30  # 레버리지 ETF 전용 DCA 재평가 주기 (분)
DCA_MAX_BUYS_PER_SESSION = 3   # 종목당 세션 최대 DCA 매수 횟수
AI_RETRY_COOLDOWN_SEC = 300    # AI 거부 후 재평가 쿨다운 (초)
TREND_REENTRY_COOLDOWN_SEC = 180  # 추세 이탈 매도 후 재진입 재평가 주기 (초)

# === [NEW] Sell Order Safety Constants ===
SELL_MARKET_RETRY = 3          # 시장가 매도 시도 횟수
SELL_LIMIT_RETRY = 2           # 지정가 fallback 시도 횟수
SELL_LIMIT_DISCOUNT_PCT = 0.5  # 지정가 fallback 할인율 (현재가 대비 %)
STOP_LOSS_MAX_RETRY_CYCLES = 5 # 손절매 재시도 사이클 한도 (이후 강제 정리)

# === [NEW] Volatility Rebound (오버솔드 반등 매수) ===
REBOUND_BUY_ENABLED = True       # 큰 하락 후 반등 매수 트리거 활성화
REBOUND_DROP_THRESHOLD_PCT = 5.0 # 5일 누적 하락 또는 갭다운이 이 % 이상이면 후보
REBOUND_INTRADAY_BOUNCE_PCT = 1.0 # 당일 저점 대비 반등 최소 %
REBOUND_MAX_BUYS_PER_SESSION = 1 # 종목당 세션 반등 매수 한도

# === [NEW v2.4] Partial Take-Profit (1차 부분 익절) ===
# trailing_stop 활성가(+5%/+7%) 도달 시 보유의 일부를 먼저 청산해 수익을 잠그고,
# 나머지는 trailing_stop 으로 추가 상승을 추적하는 비대칭 청산 전략.
# 효과: profit factor 개선 + 평균 win 증가 + 최대 win 손실 위험 감소.
PARTIAL_TP_ENABLED = True        # 부분 익절 활성화
PARTIAL_TP_RATIO = 0.5           # 1차 청산 비율 (0.5 = 보유 수량의 50%)
PARTIAL_TP_MIN_QTY = 2           # 1차 청산 최소 수량 (이보다 적으면 부분익절 skip → 전량 trailing 유지)

# === [NEW v2.5] Risk Improvements ===
# (1) Breakeven Stop — 일정 수익 도달 시 손절을 매수가(+버퍼)로 끌어올려 이긴거래 손실화 차단
BREAKEVEN_ENABLED = True
BREAKEVEN_TRIGGER_PCT_US = 3.0          # +3% 도달 시 Breakeven 아밍 (US/ETF)
BREAKEVEN_TRIGGER_PCT_KR_STOCK = 4.0    # +4% 도달 시 Breakeven 아밍 (KR 개별주)
BREAKEVEN_BUFFER_PCT = 0.2              # 매수가 대비 +0.2% 위치에 스탑 고정 (수수료 커버)

# (2) Correlation Cap — 같은 섭터/지수 그룹 동시 보유 수 한도 (분산 강제)
CORRELATION_CAP_ENABLED = True
CORRELATION_MAX_PER_GROUP = 2           # 그룹당 동시 보유 최대 종목 수
CORRELATION_GROUPS = [
    # 원자재 세트는 user_config.json 의 risk_management.correlation_groups 에서 덮어쓰기 가능
    {"TQQQ", "TECL", "FNGU", "NVDL", "SOXL", "QQQ", "NVDA"},
    {"005930", "000660"},  # 삼성전자 · SK하이닉스 (반도체 상관도 고움)
]

# (3) Losing Streak Throttle — 당일 손절/손실 누적 시 자동 신규 매수 일시정지
LOSING_STREAK_ENABLED = True
LOSING_STREAK_MAX_STOPS = 3             # 일일 손절/트레일링 손실 누적 건수
LOSING_STREAK_DAILY_PNL_PCT = -3.0      # 일일 실현 PnL이 이 % 이하면 구매 중단

# (4) ATR-based Dynamic Stop — 종목별 변동성에 맞춰 손절 폭 자동 조정
ATR_DYNAMIC_STOP_ENABLED = True
ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 2.0               # eff_stop = min(base_stop, -ATR*mult) (절대값이 큰 것 채택 → 더 느슨한 손절)
ATR_STOP_MAX_PCT = -10.0                # 손절폭 하한 (아무리 변동성 커도 -10% 이상 바이아스 견제)

# (5) Pullback Re-buy — 부분익절 후 5MA 재터치 시 소량 재진입
PULLBACK_REBUY_ENABLED = True
PULLBACK_REBUY_RATIO = 0.3              # 재진입 수량 비율 (최초 매수 수량의 30%)
PULLBACK_REBUY_MAX_PER_TICKER = 1       # 종목당 세션 최대 재진입 횟수

# Telegram Notifier for alerts
telegram = TelegramNotifier()

def send_alert(message: str, is_error: bool = False):
    """시스템 알림 발송"""
    prefix = "🚨 [ERROR] " if is_error else "ℹ️ [INFO] "
    full_message = f"{prefix}{message}\n\n⏰ {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    
    if telegram.is_configured():
        telegram.send_message(full_message)
    
    if is_error:
        logger.error(message)
    else:
        logger.info(message)

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {"trading_mode": "safe", "strategy": "day", "persona": "aggressive"}

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

def get_effective_market_config(config, market=None):
    """시장별 override를 반영한 유효 설정 반환"""
    effective = {
        "trading_mode": config.get("trading_mode", "safe"),
        "strategy": config.get("strategy", "day"),
        "persona": config.get("persona", "aggressive")
    }

    if not market:
        return effective

    market_settings = config.get("market_settings", {})
    market_override = market_settings.get(str(market).lower(), {})
    for key in effective:
        if key in market_override:
            effective[key] = market_override[key]

    return effective

def check_and_upgrade_mode(total_asset_krw):
    """
    KR 시장 전용: 예치금이 1000만원 이상이면 자동으로 레버리지 모드로 전환
    US 시장은 3배 ETF 제한이 없으므로 이 함수는 KR에만 영향
    """
    config = load_config()
    current_mode = config.get("trading_mode", "safe")
    
    if total_asset_krw >= LEVERAGE_THRESHOLD_KRW and current_mode == "safe":
        logger.info(f"🎉 축하합니다! 총 자산이 {total_asset_krw:,.0f}원으로 1000만원을 달성했습니다!")
        logger.info("🚀 KR 시장 레버리지 ETF 해제! (US 시장은 이미 제한 없음)")
        config["trading_mode"] = "risky"
        config.setdefault("market_settings", {})
        config["market_settings"].setdefault("kr", {})
        config["market_settings"]["kr"]["trading_mode"] = "risky"
        save_config(config)
        return True
    return False

# Load initial config
user_config = load_config()
base_config = get_effective_market_config(user_config)
IS_SAFE_MODE = True if base_config.get("trading_mode") == "safe" else False
STRATEGY_MODE = base_config.get("strategy", "day")  # 'day', 'swing', or 'dca'
PERSONA = base_config.get("persona", "aggressive") # 'aggressive', 'neutral', 'conservative'

DCA_SETTINGS = user_config.get("dca_settings", {
    "enabled": True,
    "daily_investment_pct": 5,
    "buy_delay_minutes": 30,
    "reentry_interval_minutes": 15,
    "leveraged_reentry_interval_minutes": 30,
    "min_investment_usd": 10,
    "max_investment_usd": 100,
    "max_buys_per_session": 1
})

DCA_REENTRY_INTERVAL_MIN = DCA_SETTINGS.get("reentry_interval_minutes", DCA_REENTRY_INTERVAL_MIN)
DCA_LEVERAGED_REENTRY_INTERVAL_MIN = DCA_SETTINGS.get(
    "leveraged_reentry_interval_minutes",
    DCA_LEVERAGED_REENTRY_INTERVAL_MIN
)
DCA_MAX_BUYS_PER_SESSION = DCA_SETTINGS.get("max_buys_per_session", DCA_MAX_BUYS_PER_SESSION)

# Load Risk Management Settings from config (overrides defaults)
risk_config = user_config.get("risk_management", {})
if risk_config:
    STOP_LOSS_PCT = risk_config.get("stop_loss_pct", STOP_LOSS_PCT)
    TRAILING_STOP_ACTIVATION = risk_config.get("trailing_stop_activation_pct", TRAILING_STOP_ACTIVATION)
    TRAILING_STOP_DROP = risk_config.get("trailing_stop_drop_pct", TRAILING_STOP_DROP)
    GAP_DOWN_THRESHOLD = risk_config.get("gap_down_threshold_pct", GAP_DOWN_THRESHOLD)
    CONSECUTIVE_DECLINE_DAYS = risk_config.get("consecutive_decline_days", CONSECUTIVE_DECLINE_DAYS)
    CONSECUTIVE_DECLINE_PCT = risk_config.get("consecutive_decline_pct", CONSECUTIVE_DECLINE_PCT)
    PORTFOLIO_DRAWDOWN_PCT = risk_config.get("portfolio_drawdown_pct", PORTFOLIO_DRAWDOWN_PCT)
    DCA_MONITOR_INTERVAL = risk_config.get("dca_monitor_interval_sec", DCA_MONITOR_INTERVAL)
    REBOUND_BUY_ENABLED = risk_config.get("rebound_buy_enabled", REBOUND_BUY_ENABLED)
    REBOUND_DROP_THRESHOLD_PCT = risk_config.get("rebound_drop_threshold_pct", REBOUND_DROP_THRESHOLD_PCT)
    REBOUND_INTRADAY_BOUNCE_PCT = risk_config.get("rebound_intraday_bounce_pct", REBOUND_INTRADAY_BOUNCE_PCT)
    REBOUND_MAX_BUYS_PER_SESSION = risk_config.get("rebound_max_buys_per_session", REBOUND_MAX_BUYS_PER_SESSION)
    # === v2.5 risk knobs (모두 선택 사항, 미설정 시 코드 상단 기본값 사용) ===
    PARTIAL_TP_ENABLED = bool(risk_config.get("partial_tp_enabled", PARTIAL_TP_ENABLED))
    PARTIAL_TP_RATIO = float(risk_config.get("partial_tp_ratio", PARTIAL_TP_RATIO))
    BREAKEVEN_ENABLED = bool(risk_config.get("breakeven_enabled", BREAKEVEN_ENABLED))
    BREAKEVEN_TRIGGER_PCT_US = float(risk_config.get("breakeven_trigger_pct_us", BREAKEVEN_TRIGGER_PCT_US))
    BREAKEVEN_TRIGGER_PCT_KR_STOCK = float(risk_config.get("breakeven_trigger_pct_kr_stock", BREAKEVEN_TRIGGER_PCT_KR_STOCK))
    BREAKEVEN_BUFFER_PCT = float(risk_config.get("breakeven_buffer_pct", BREAKEVEN_BUFFER_PCT))
    CORRELATION_CAP_ENABLED = bool(risk_config.get("correlation_cap_enabled", CORRELATION_CAP_ENABLED))
    CORRELATION_MAX_PER_GROUP = int(risk_config.get("correlation_max_per_group", CORRELATION_MAX_PER_GROUP))
    _user_groups = risk_config.get("correlation_groups")
    if isinstance(_user_groups, list) and _user_groups:
        try:
            CORRELATION_GROUPS = [set(map(str, g)) for g in _user_groups if g]
        except Exception:
            pass
    LOSING_STREAK_ENABLED = bool(risk_config.get("losing_streak_enabled", LOSING_STREAK_ENABLED))
    LOSING_STREAK_MAX_STOPS = int(risk_config.get("losing_streak_max_stops", LOSING_STREAK_MAX_STOPS))
    LOSING_STREAK_DAILY_PNL_PCT = float(risk_config.get("losing_streak_daily_pnl_pct", LOSING_STREAK_DAILY_PNL_PCT))
    ATR_DYNAMIC_STOP_ENABLED = bool(risk_config.get("atr_dynamic_stop_enabled", ATR_DYNAMIC_STOP_ENABLED))
    ATR_PERIOD = int(risk_config.get("atr_period", ATR_PERIOD))
    ATR_STOP_MULTIPLIER = float(risk_config.get("atr_stop_multiplier", ATR_STOP_MULTIPLIER))
    PULLBACK_REBUY_ENABLED = bool(risk_config.get("pullback_rebuy_enabled", PULLBACK_REBUY_ENABLED))
    PULLBACK_REBUY_RATIO = float(risk_config.get("pullback_rebuy_ratio", PULLBACK_REBUY_RATIO))

logger.info(f"Loaded Config: Mode={user_config.get('trading_mode')}, Strategy={STRATEGY_MODE}, Persona={PERSONA}")

# 1. Leveraged Targets (Requires >10M KRW Deposit & Education)
TARGET_TICKERS_US_3X = [
    {'symbol': "TQQQ", 'exchange': "NAS"},  # ProShares UltraPro QQQ (나스닥 3X, 유동성 최고)
    {'symbol': "SOXL", 'exchange': "AMS"},  # Direxion Daily Semiconductor Bull 3X
    {'symbol': "NVDL", 'exchange': "NAS"},  # GraniteShares 2x Long NVDA
    {'symbol': "TECL", 'exchange': "AMS"},  # Direxion Daily Technology Bull 3X
    {'symbol': "FNGU", 'exchange': "AMS"},  # MicroSectors FANG+ Index 3X
]

TARGET_TICKERS_KR_2X = [
    "122630", # KODEX Leverage (KOSPI 200 2x)
    "233740", # KODEX KOSDAQ150 Leverage
    "449200", # KODEX US Tech Top10
]

# 2. Non-Leveraged (1x) Targets (Stock Centric for < 10M KRW restrictions)
TARGET_TICKERS_US_1X = [
    {'symbol': "NVDA", 'exchange': "NAS"},  # NVIDIA
    {'symbol': "MSFT", 'exchange': "NAS"},  # Microsoft
    {'symbol': "AAPL", 'exchange': "NAS"},  # Apple
    {'symbol': "GOOGL", 'exchange': "NAS"}, # Google
    {'symbol': "TSLA", 'exchange': "NAS"},  # Tesla
    {'symbol': "PLTR", 'exchange': "NAS"},  # Palantir
    {'symbol': "TSM",  'exchange': "NAS"},  # TSMC
]

# Changed from ETF to Blue Chip Logic for users with deposit restrictions
TARGET_TICKERS_KR_1X = [
    "426030", # TIME 미국나스닥100액티브
    "000660", # SK Hynix (SK하이닉스)
    "005930", # Samsung Electronics (삼성전자)
    "012450", # Hanwha Aerospace (한화에어로스페이스)
    "005380", # Hyundai Motor (현대차)
    "035420", # Naver (네이버)
    # ===== 신규 추가 요청 ETF =====
    "292150", # TIGER 코리아TOP10
    "495230", # KoAct 코리아밸류업 액티브
    "0080G0", # KODEX 방산TOP10
    "0151P0", # RISE 코리아전략산업액티브
    "0015B0", # KoAct 미국나스닥성장기업 액티브
    "456600", # TIME 글로벌AI인공지능 액티브
    "0174B0", # KoAct 글로벌AI메모리반도체 액티브
    "0180V0", # ACE 미국우주테크 액티브
    "0173Y0", # KODEX 미국AI광통신네트워크
]

# Select Tickers based on Mode
# US 시장은 3배 ETF 제한 없음 - 항상 3X + 1X 모두 사용
TARGET_TICKERS_US = TARGET_TICKERS_US_3X + [t for t in TARGET_TICKERS_US_1X if t not in TARGET_TICKERS_US_3X]
# KR 시장만 예탁금 기준으로 safe/risky 분리
TARGET_TICKERS_KR = TARGET_TICKERS_KR_1X if IS_SAFE_MODE else TARGET_TICKERS_KR_2X

# === KR 시장별 차별화된 리스크 관리 상수 ===
KR_STOCK_STOP_LOSS_PCT = -7.0       # KR 개별주 손절 (ETF 대비 완화, 단기 변동성 흡수)
KR_STOCK_TRAILING_ACTIVATION = 7.0  # KR 개별주 트레일링 활성화 (충분한 익절 폭 확보)
KR_STOCK_TRAILING_DROP = 4.0        # KR 개별주 고점 대비 하락 (조기 청산 방지)
KR_STOCK_GAP_DOWN_THRESHOLD = 6.0   # KR 개별주 갭다운 기준

# KR ETF 종목 코드 (ETF인지 개별주인지 구분용)
KR_ETF_CODES = {'122630', '233740', '449200', '426030', '069500', '229200', '114800',
                '292150', '495230', '0080G0', '0151P0', '0015B0',
                '456600', '0174B0', '0180V0', '0173Y0'}

# US 레버리지 ETF 심볼 목록 (3X ETF는 DCA 매수 조건 완화 적용)
US_LEVERAGED_ETF_SYMBOLS = {t['symbol'] for t in TARGET_TICKERS_US_3X}

# ----------------------------------------------------
# [Dynamic Portfolio] Load evaluated candidates if exists
# ----------------------------------------------------
import os
try:
    from modules.portfolio_manager import PortfolioManager
    dyn_pf = PortfolioManager.load_portfolio()
    if dyn_pf:
        kr_dyn = dyn_pf.get('TARGET_TICKERS_KR_1X', [])
        if kr_dyn:
            # Append uniquely
            for t in kr_dyn:
                if t not in TARGET_TICKERS_KR_1X:
                    TARGET_TICKERS_KR_1X.append(t)
            KR_ETF_CODES.update(kr_dyn)
            kr_dyn_us = dyn_pf.get('TARGET_TICKERS_US_1X', [])
            TARGET_TICKERS_US_1X = kr_dyn_us if kr_dyn_us else TARGET_TICKERS_US_1X
            us_dyn_3x = dyn_pf.get('TARGET_TICKERS_US_3X', [])
            TARGET_TICKERS_US_3X = us_dyn_3x if us_dyn_3x else TARGET_TICKERS_US_3X
        
        # Redefine target tickers
        TARGET_TICKERS_US = TARGET_TICKERS_US_3X + [t for t in TARGET_TICKERS_US_1X if t not in TARGET_TICKERS_US_3X]
        TARGET_TICKERS_KR = TARGET_TICKERS_KR_1X if IS_SAFE_MODE else TARGET_TICKERS_KR_2X
except Exception as e:
    print(f'Dynamic Portfolio Load Error: {e}')

def calculate_signal_strength(current_price, target_price, ma20, ohlc_data):
    """
    매수 신호 강도 계산 (0.0 ~ 1.0)
    - 목표가 돌파 정도
    - 20MA 대비 위치
    - 거래량 증가율 (향후 추가 가능)
    """
    if not current_price or not target_price or not ma20:
        return 0.5  # 기본값
    
    strength = 0.0
    
    # 1. 목표가 돌파 강도 (0 ~ 0.4)
    # 목표가를 얼마나 초과했는지 (최대 2% 초과 시 만점)
    breakout_pct = (current_price - target_price) / target_price
    breakout_score = min(breakout_pct / 0.02, 1.0) * 0.4
    strength += max(breakout_score, 0)
    
    # 2. 20MA 대비 상승 강도 (0 ~ 0.3)
    # 현재가가 MA20보다 얼마나 위인지 (최대 3% 위 시 만점)
    ma_distance_pct = (current_price - ma20) / ma20
    ma_score = min(ma_distance_pct / 0.03, 1.0) * 0.3
    strength += max(ma_score, 0)
    
    # 3. 최근 변동성 대비 돌파 강도 (0 ~ 0.3)
    if ohlc_data and len(ohlc_data) >= 5:
        try:
            recent_ranges = []
            for i in range(min(5, len(ohlc_data))):
                high = safe_float(ohlc_data[i]['high'])
                low = safe_float(ohlc_data[i]['low'])
                recent_ranges.append(high - low)
            avg_range = sum(recent_ranges) / len(recent_ranges)
            today_move = current_price - target_price
            volatility_score = min(today_move / avg_range, 1.0) * 0.3 if avg_range > 0 else 0.15
            strength += max(volatility_score, 0)
        except:
            strength += 0.15  # 기본값
    else:
        strength += 0.15
    
    return min(strength, 1.0)

def calculate_order_quantity(available_cash, current_price, signal_strength=0.5, num_targets=1):
    """
    신호 강도에 따른 동적 매수 수량 계산
    
    - signal_strength: 0.0 ~ 1.0 (약한 신호 ~ 강한 신호)
    - 강한 신호(0.8+): 가용 자금의 30%까지 투자
    - 보통 신호(0.5~0.8): 가용 자금의 20%까지 투자
    - 약한 신호(0.3~0.5): 가용 자금의 10%까지 투자
    - 매우 약한 신호(<0.3): 최소 수량만 투자
    """
    if not available_cash or not current_price or current_price <= 0:
        return 1
    
    # 분산 투자를 위해 종목 수로 나눔
    per_ticker_cash = available_cash / max(num_targets, 1)
    
    # 신호 강도에 따른 투자 비율 결정 (자본 대비 너무 적은 매수를 방지하기 위해 상향 조정)
    if signal_strength >= 0.8:
        position_pct = 1.0  # 강한 신호: 종목 할당 금액의 100%
        logger.info(f"📈 강한 매수 신호! (강도: {signal_strength:.2f}) → 포지션 100%")
    elif signal_strength >= 0.5:
        position_pct = 0.7  # 보통 신호: 70%
        logger.info(f"📊 보통 매수 신호 (강도: {signal_strength:.2f}) → 포지션 70%")
    elif signal_strength >= 0.3:
        position_pct = 0.4  # 약한 신호: 40%
        logger.info(f"📉 약한 매수 신호 (강도: {signal_strength:.2f}) → 포지션 40%")
    else:
        position_pct = 0.2  # 매우 약한 신호: 20%
        logger.info(f"⚠️ 매우 약한 신호 (강도: {signal_strength:.2f}) → 최소 포지션 20%")
    
    max_investment = per_ticker_cash * position_pct
    
    # 1주당 가격이 할당 금액보다 비쌀 경우, 가용 자금이 충분하다면 우선 1주는 매수할 수 있도록 max_investment 보정
    if max_investment < current_price and available_cash >= current_price:
        max_investment = current_price
        
    qty = int(max_investment / current_price)
    
    return max(qty, 1)  # 최소 1주

def calculate_dca_quantity(available_cash, current_price, num_targets=1, dca_settings=None, market='US'):
    """
    DCA 전략용 매수 수량 계산
    - 매일 일정 비율/금액을 분할 매수
    """
    if not available_cash or not current_price or current_price <= 0:
        return 1
    
    if dca_settings is None:
        dca_settings = DCA_SETTINGS
    
    daily_pct = dca_settings.get("daily_investment_pct", 5) / 100
    min_investment = dca_settings.get("min_investment_usd", 10)
    max_investment = dca_settings.get("max_investment_usd", 100)

    # KR Market Conversion
    currency_symbol = "$"
    if market != 'US':
        exchange_rate = 1450 # Conservative Exchange Rate
        min_investment *= exchange_rate
        max_investment *= exchange_rate
        currency_symbol = "₩"
    
    # 종목별 투자 금액 계산
    per_ticker_cash = available_cash / max(num_targets, 1)
    
    # daily_pct를 목표별 할당금액(per_ticker_cash)이 아닌 가용 자금 전체(available_cash) 기준으로 계산하되,
    # 한 종목에 너무 많은 자금이 몰리지 않도록 per_ticker_cash를 한도로 둠
    target_investment_amount = available_cash * daily_pct
    investment_amount = min(target_investment_amount, per_ticker_cash)
    
    # 최소/최대 제한 적용
    investment_amount = max(min_investment, min(max_investment, investment_amount))

    # 주문 가능 금액이 1주 가격보다 작으나 가용 자금(available_cash)이 충분하다면 최소 1주는 살 수 있도록 조정
    if investment_amount < current_price and available_cash >= current_price:
        investment_amount = current_price

    if investment_amount < current_price:
        logger.warning(
            f"💸 DCA 스킵: 주문가능금액 부족 ({currency_symbol}{investment_amount:,.2f} < 1주 {currency_symbol}{current_price:,.2f})"
        )
        return 0
    
    qty = int(investment_amount / current_price)
    
    logger.info(f"📈 DCA 매수: {currency_symbol}{investment_amount:,.0f} → {qty}주 (가격: {currency_symbol}{current_price:,.0f})")
    
    return max(qty, 0)

K_VALUE = 0.4  # 0.5 → 0.4: 타겟가를 낮춰 상승 초기 진입 용이 (레버리지 ETF 최적화)

def get_market_status():
    """
    Returns 'US', 'KR', or 'CLOSED' based on current KST time.
    Includes weekday check (markets closed on weekends).
    """
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.datetime.now(kst)
    t = int(now.strftime("%H%M"))
    weekday = now.weekday()  # 0=Monday, 6=Sunday
    
    # US Market: 23:30 ~ 06:00 KST
    # Evening (23:30~23:59): Mon-Fri KST = US Mon-Fri sessions
    # Morning (00:00~06:00): Tue-Sat KST = US Mon-Fri sessions (continued from prev night)
    if 2330 <= t <= 2359 and weekday <= 4:  # Mon-Fri evening
        return 'US'
    if 0 <= t < 600 and 1 <= weekday <= 5:  # Tue-Sat morning
        return 'US'
    
    # KR Market: 09:00 ~ 15:20, Mon-Fri only
    if 900 <= t <= 1520 and weekday <= 4:
        return 'KR'
        
    return 'CLOSED'

def is_market_open_for(market):
    """매도 주문이 실제로 체결될 수 있는 시장 시간인지 확인.
    한국 정규장은 09:00~15:30, 동시호가/장 외 시간에는 시장가 거부.
    """
    try:
        status = get_market_status()
        if status == 'CLOSED':
            return False
        return status == market
    except Exception:
        # 안전하게 False 반환 (시간 정보 실패 시 매도 시도 안 함)
        return False


def _lookup_price(kis_client, ticker, market, exchange):
    """journal 기록용 현재가 조회 (실패 시 0.0)."""
    try:
        if market == 'US':
            return float(kis_client.get_current_price(ticker, exchange or 'NAS') or 0.0)
        return float(kis_client.get_current_price(ticker) or 0.0)
    except Exception:
        return 0.0


def _journal_record(market, ticker, reason, exit_price, sold_qty, phase, monitor_data=None):
    """trade_journal 기록 헬퍼. 절대로 매매 흐름을 막지 않는다."""
    try:
        trade_journal.record_close(
            ticker=ticker,
            market=market,
            trigger=str(reason or 'unknown'),
            exit_price=float(exit_price or 0.0),
            sold_qty=int(sold_qty or 0),
            monitor_data=monitor_data or {},
            phase=phase,
            version='v2.5',
        )
    except Exception as _je:
        try:
            logger.warning(f"[{ticker}] trade_journal 기록 실패: {_je}")
        except Exception:
            pass


# ============================================================
# [v2.5] Daily Risk State - 일일 손절/손실 누적 추적기
# ============================================================
_daily_state = {
    'date': None,
    'stop_count': 0,
    'realized_pnl_pct': 0.0,
    'paused': False,
}


def _daily_state_reset_if_needed():
    """KST 자정 기준 일일 상태 리셋."""
    try:
        today = datetime.datetime.now(KST).strftime('%Y-%m-%d')
    except Exception:
        today = datetime.datetime.now().strftime('%Y-%m-%d')
    if _daily_state.get('date') != today:
        _daily_state['date'] = today
        _daily_state['stop_count'] = 0
        _daily_state['realized_pnl_pct'] = 0.0
        _daily_state['paused'] = False


def _record_loss_event(pnl_pct, trigger):
    """손절/트레일링 손실 이벤트 누적. losing-streak throttle 판정에 사용."""
    if not LOSING_STREAK_ENABLED:
        return
    _daily_state_reset_if_needed()
    # 손실(또는 stop_loss 트리거) 만 카운트
    if trigger in ('stop_loss', 'breakeven_stop') or (pnl_pct is not None and pnl_pct < 0):
        _daily_state['stop_count'] = int(_daily_state.get('stop_count', 0)) + 1
    if pnl_pct is not None:
        try:
            _daily_state['realized_pnl_pct'] = float(_daily_state.get('realized_pnl_pct', 0.0)) + float(pnl_pct)
        except Exception:
            pass
    if (_daily_state['stop_count'] >= LOSING_STREAK_MAX_STOPS
            or _daily_state['realized_pnl_pct'] <= LOSING_STREAK_DAILY_PNL_PCT):
        if not _daily_state.get('paused'):
            _daily_state['paused'] = True
            try:
                logger.warning(
                    f"🛑 [LosingStreak] 일일 손절 {_daily_state['stop_count']}건 / "
                    f"누적 {_daily_state['realized_pnl_pct']:.2f}% → 신규 매수 일시 중단"
                )
            except Exception:
                pass


def is_losing_streak_pause():
    """오늘 신규 매수가 차단되어야 하는지 여부."""
    if not LOSING_STREAK_ENABLED:
        return False
    _daily_state_reset_if_needed()
    return bool(_daily_state.get('paused'))


def is_correlation_capped(ticker, monitoring_targets):
    """상관 그룹 내 이미 보유(status=='bought') 종목 수가 한도 이상인지 체크.

    True 면 신규 매수 차단.
    """
    if not CORRELATION_CAP_ENABLED or not CORRELATION_GROUPS:
        return False, None
    try:
        t = str(ticker)
        for grp in CORRELATION_GROUPS:
            if t not in grp:
                continue
            held = 0
            for tk, d in (monitoring_targets or {}).items():
                if str(tk) == t:
                    continue
                if str(tk) in grp and d and d.get('status') == 'bought' and int(d.get('buys', 0)) > 0:
                    held += 1
            if held >= CORRELATION_MAX_PER_GROUP:
                return True, sorted(list(grp))
        return False, None
    except Exception:
        return False, None


def get_breakeven_trigger_pct(market, ticker):
    """시장/종목별 Breakeven 활성화 임계치."""
    if not BREAKEVEN_ENABLED:
        return None
    if market == 'KR' and ticker not in KR_ETF_CODES:
        return float(BREAKEVEN_TRIGGER_PCT_KR_STOCK)
    return float(BREAKEVEN_TRIGGER_PCT_US)


def effective_stop_loss_pct(base_pct, ohlc):
    """ATR 동적 손절폭 산정.

    base_pct: 기존 손절 % (음수, 예: -5.0)
    반환: 더 보수적(=절대값 큰) 손절폭, 단 ATR_STOP_MAX_PCT 로 하한 보호.
    """
    if not ATR_DYNAMIC_STOP_ENABLED or not ohlc:
        return base_pct
    atr_pct = calculate_atr_pct(ohlc, period=ATR_PERIOD)
    if atr_pct is None or atr_pct <= 0:
        return base_pct
    atr_stop = -ATR_STOP_MULTIPLIER * atr_pct
    # 더 큰 절대값을 채택 (= 더 느슨한 손절) → 노이즈 컷 회피
    candidate = min(base_pct, atr_stop)
    return max(candidate, ATR_STOP_MAX_PCT)


def safe_sell(kis_client, market, ticker, qty_hint, exchange=None,
              reason="sell", allow_limit_fallback=True, monitor_data=None):
    """매도 주문 안전 실행기.

    Returns: dict {
        'success': bool,
        'sold_qty': int (실제 매도 수량 또는 이미 청산된 보유 0),
        'phase': 'market'|'limit'|'already_flat'|'deferred'|'failed',
        'error': str (실패 시 메시지)
    }

    동작:
      1) 시장이 닫혀있으면 deferred 반환 (실패 알림 보내지 말 것)
      2) 실제 보유 수량 재조회 → 0이면 already_flat (성공으로 처리)
      3) 시장가 매도 SELL_MARKET_RETRY 회 시도
      4) 실패 시 (allow_limit_fallback) 현재가 -SELL_LIMIT_DISCOUNT_PCT% 지정가로
         SELL_LIMIT_RETRY 회 시도
      5) 그래도 실패하면 failed 반환 + error 메시지 (호출자가 알림 결정)
    """
    if not is_market_open_for(market):
        return {'success': False, 'sold_qty': 0, 'phase': 'deferred',
                'error': f'market {market} closed'}

    # 1) 실제 보유 수량 재조회 (KIS API mismatch 방지)
    try:
        actual_qty = kis_client.get_holding_qty(ticker)
    except Exception as _e:
        logger.warning(f"[{ticker}] get_holding_qty 실패 ({_e}) → 캐시 수량 사용")
        actual_qty = qty_hint

    effective_qty = max(0, min(int(qty_hint or 0), int(actual_qty or 0))) if actual_qty else 0

    if actual_qty <= 0:
        # 외부 청산도 trade_journal 에 기록 (exit_price 미상 → 0)
        _journal_record(market, ticker, reason, exit_price=0.0, sold_qty=0,
                        phase='already_flat', monitor_data=monitor_data)
        return {'success': True, 'sold_qty': 0, 'phase': 'already_flat',
                'error': None}

    if effective_qty <= 0:
        # qty_hint=0 이지만 실제 보유분이 있는 경우 → 실제 수량으로 매도
        effective_qty = int(actual_qty)

    last_err = None

    # 2) 시장가 매도 시도
    for attempt in range(SELL_MARKET_RETRY):
        try:
            if market == 'US':
                res = kis_client.sell_market_order(ticker, effective_qty, exchange or 'NAS')
            else:
                res = kis_client.sell_market_order(ticker, effective_qty)
            if res and res.get('rt_cd') == '0':
                logger.info(f"[{ticker}] ✅ {reason} 시장가 매도 성공 ({effective_qty}주)")
                _journal_record(market, ticker, reason,
                                exit_price=_lookup_price(kis_client, ticker, market, exchange),
                                sold_qty=effective_qty, phase='market',
                                monitor_data=monitor_data)
                return {'success': True, 'sold_qty': effective_qty,
                        'phase': 'market', 'error': None}
            last_err = (res or {}).get('msg1') or 'no response'
            logger.warning(f"[{ticker}] {reason} 시장가 시도 {attempt+1} 실패: {last_err}")
        except Exception as e:
            last_err = str(e)
            logger.warning(f"[{ticker}] {reason} 시장가 예외: {e}")
        time.sleep(1.5)

    # 3) 지정가 fallback (KR 전용 - US는 sell_market_order가 이미 limit 기반)
    if allow_limit_fallback and market == 'KR':
        try:
            curr = kis_client.get_current_price(ticker)
            if curr and curr > 0:
                limit_price = curr * (1.0 - SELL_LIMIT_DISCOUNT_PCT / 100.0)
                for attempt in range(SELL_LIMIT_RETRY):
                    res = kis_client.sell_limit_order(ticker, effective_qty, limit_price)
                    if res and res.get('rt_cd') == '0':
                        logger.info(
                            f"[{ticker}] ✅ {reason} 지정가 fallback 성공 "
                            f"({effective_qty}주 @ {int(limit_price):,})"
                        )
                        _journal_record(market, ticker, reason,
                                        exit_price=limit_price,
                                        sold_qty=effective_qty, phase='limit',
                                        monitor_data=monitor_data)
                        return {'success': True, 'sold_qty': effective_qty,
                                'phase': 'limit', 'error': None}
                    last_err = (res or {}).get('msg1') or 'no response'
                    logger.warning(
                        f"[{ticker}] {reason} 지정가 시도 {attempt+1} 실패: {last_err}"
                    )
                    time.sleep(1.5)
        except Exception as e:
            last_err = str(e)
            logger.warning(f"[{ticker}] {reason} 지정가 예외: {e}")

    return {'success': False, 'sold_qty': 0, 'phase': 'failed',
            'error': last_err or 'unknown'}

def job():
    # Reload config dynamically
    global IS_SAFE_MODE, STRATEGY_MODE, PERSONA, TARGET_TICKERS_US, TARGET_TICKERS_KR
    user_config = load_config()

    market = get_market_status()
    
    if market == 'CLOSED':
        logger.info("Market is closed. Sleeping.")
        return

    effective_config = get_effective_market_config(user_config, market)
    IS_SAFE_MODE = effective_config.get("trading_mode") == "safe"
    STRATEGY_MODE = effective_config.get("strategy", "day")
    PERSONA = effective_config.get("persona", "aggressive")

    # Update Target Tickers based on effective config
    # US 시장은 항상 3X + 1X 모두 사용 (레버리지 제한 없음)
    TARGET_TICKERS_US = TARGET_TICKERS_US_3X + [t for t in TARGET_TICKERS_US_1X if t not in TARGET_TICKERS_US_3X]
    # KR만 예탁금 기준 safe/risky 분리
    TARGET_TICKERS_KR = TARGET_TICKERS_KR_1X if IS_SAFE_MODE else TARGET_TICKERS_KR_2X

    logger.info(f"[{market}] Effective Config: Mode={effective_config.get('trading_mode')}, Strategy={STRATEGY_MODE}, Persona={PERSONA}")

    # --- [New] Buy Delay Logic for Market Stabilization ---
    buy_delay = DCA_SETTINGS.get("buy_delay_minutes", 0) if STRATEGY_MODE == 'dca' else 0
    if buy_delay > 0:
        kst = pytz.timezone('Asia/Seoul')
        now = datetime.datetime.now(kst)
        
        # Determine Market Open Time
        if market == 'US':
            # US Open: 23:30 KST
            market_open = now.replace(hour=23, minute=30, second=0, microsecond=0)
            if 0 <= now.hour < 9: # Early morning (next day in KST)
                market_open = market_open - datetime.timedelta(days=1)
        else:
            # KR Open: 09:00 KST
            market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
        
        target_time = market_open + datetime.timedelta(minutes=buy_delay)
        wait_seconds = (target_time - now).total_seconds()
        
        # Only wait if we are within the delay window (don't wait if we started late)
        # also check if wait_seconds is reasonable (e.g. < 2 hours)
        if 0 < wait_seconds <= (buy_delay * 60) + 60: 
            logger.info(f"⏳ Waiting {buy_delay} minutes for market stabilization... ({wait_seconds/60:.1f} min left)")
            time.sleep(wait_seconds)
            logger.info("⚡ Market stabilized. Starting analysis.")

    # Select Market Context
    if market == 'US':
        logger.info(f"🇺🇸 Starting US Trading Session ({STRATEGY_MODE.upper()}) for {TARGET_TICKERS_US}")
        kis = KisOverseas()
        tickers = TARGET_TICKERS_US
    else:
        kis = KisDomestic()
        # Merge Static and Dynamic Targets for KR
        # DYNAMIC_TARGETS are found by the scanner
        current_dynamic_targets = [t for t in DYNAMIC_TARGETS if t.get('exchange') == 'KR']
        tickers = TARGET_TICKERS_KR + current_dynamic_targets
        
        logger.info(f"🇰🇷 Starting KR Trading Session ({STRATEGY_MODE.upper()})")
        
        # Log Static Targets (handle both dict and string)
        static_log = [t['symbol'] if isinstance(t, dict) else t for t in TARGET_TICKERS_KR]
        logger.info(f"   - Static Targets: {static_log}")
        
        if current_dynamic_targets:
            logger.info(f"   - Dynamic Targets (Scanner): {[t['symbol'] for t in current_dynamic_targets]}")
            
    llm_consensus_cfg = user_config.get("llm_consensus", {})
    ai = MultiLLMAnalyst(consensus_config=llm_consensus_cfg)
    
    # Dictionary to store monitoring targets
    monitoring_targets = {}
    skipped_buy_reasons = {}

    def record_skip_reason(ticker, reason):
        """매수 보류/차단 사유 추적"""
        if not ticker or not reason:
            return
        skipped_buy_reasons.setdefault(ticker, [])
        if reason not in skipped_buy_reasons[ticker]:
            skipped_buy_reasons[ticker].append(reason)

    def prepare_dca_wait_target(ticker, exchange, current_price, ma20, ma5, ohlc):
        """DCA 장중 재평가를 위한 대기 타겟 등록/업데이트"""
        existing = monitoring_targets.get(ticker, {})
        is_us_leveraged_etf = (market == 'US' and ticker in US_LEVERAGED_ETF_SYMBOLS)
        monitoring_targets[ticker] = {
            'target': current_price,
            'status': existing.get('status', 'dca_wait') if existing.get('status') != 'bought' else 'bought',
            'buys': existing.get('buys', 0),
            'exchange': exchange,
            'ma20': ma20,
            'ma5': ma5,
            'ohlc': ohlc,
            'buy_price': existing.get('buy_price', 0),
            'highest_price': existing.get('highest_price', current_price),
            'dca_buys_this_session': existing.get('dca_buys_this_session', 0),
            'last_dca_attempt_at': existing.get('last_dca_attempt_at'),
            'dca_reentry_interval_min': (
                DCA_LEVERAGED_REENTRY_INTERVAL_MIN if is_us_leveraged_etf else DCA_REENTRY_INTERVAL_MIN
            )
        }

    def refresh_ticker_snapshot(ticker, exchange):
        """장중 재평가를 위한 최신 가격/일봉/이동평균 갱신"""
        try:
            if market == 'US':
                ohlc = kis.get_daily_ohlc(ticker, exchange)
                current_price = kis.get_current_price(ticker, exchange)
            else:
                ohlc = kis.get_daily_ohlc(ticker)
                current_price = kis.get_current_price(ticker)

            if not ohlc or not current_price:
                return None, None, None, None

            closes = [safe_float(x['clos']) for x in ohlc]
            closes.reverse()
            ma20 = calculate_ma(closes, 20)
            ma5 = calculate_short_ma(closes, 5)
            return current_price, ohlc, ma20, ma5
        except Exception as e:
            logger.error(f"[{ticker}] Snapshot refresh failed: {e}")
            return None, None, None, None

    def attempt_dca_buy(ticker, exchange, current_price, ma20, ma5, ohlc, eff_gap_down_threshold, num_targets):
        """DCA 후보를 재평가하여 조건 충족 시 1회 매수"""
        existing = monitoring_targets.get(ticker, {})

        # [v2.5] Losing-streak throttle: 오늘 누적 손실/손절 한도 초과 시 전 종목 매수 차단
        if is_losing_streak_pause():
            record_skip_reason(ticker, "losing-streak 차단 (일일 손실 한도 초과)")
            return False

        # [v2.5] Correlation cap: 같은 그룹 보유 종목 수 한도 초과 시 차단
        capped, grp = is_correlation_capped(ticker, monitoring_targets)
        if capped:
            record_skip_reason(ticker, f"상관그룹 한도 초과 (group={grp})")
            return False

        if existing.get('dca_buys_this_session', 0) >= DCA_MAX_BUYS_PER_SESSION:
            record_skip_reason(ticker, f"세션 매수 한도 ({DCA_MAX_BUYS_PER_SESSION})")
            return False

        is_uptrend = check_trend(current_price, ma20)
        is_short_uptrend = check_trend(current_price, ma5) if ma5 else True
        is_gap_down, gap_drop_pct = check_gap_down(current_price, ohlc, eff_gap_down_threshold)
        is_consecutive_decline, cum_drop_pct = check_consecutive_decline(
            ohlc, CONSECUTIVE_DECLINE_DAYS, CONSECUTIVE_DECLINE_PCT
        )

        buy_blocked = False
        block_reasons = []
        is_us_leveraged_etf = (market == 'US' and ticker in US_LEVERAGED_ETF_SYMBOLS)

        effective_gap_threshold = eff_gap_down_threshold * 2 if is_us_leveraged_etf else eff_gap_down_threshold
        if is_gap_down and gap_drop_pct >= effective_gap_threshold:
            buy_blocked = True
            block_reasons.append(f"갭다운 {gap_drop_pct:.1f}%")
        elif is_gap_down and is_us_leveraged_etf:
            block_reasons.append(f"갭다운 {gap_drop_pct:.1f}% (레버리지 ETF 완화 적용)")

        if is_consecutive_decline:
            if is_us_leveraged_etf:
                if CONSECUTIVE_DECLINE_DAYS >= 3 or cum_drop_pct >= 5.0:
                    buy_blocked = True
                    block_reasons.append(f"연속 {CONSECUTIVE_DECLINE_DAYS}일 하락 {cum_drop_pct:.1f}%")
                else:
                    block_reasons.append(f"연속 하락 {cum_drop_pct:.1f}% (레버리지 ETF 완화 적용)")
            else:
                buy_blocked = True
                block_reasons.append(f"연속 {CONSECUTIVE_DECLINE_DAYS}일 하락 {cum_drop_pct:.1f}%")

        # === [NEW] 변동성 반등 매수 (Oversold Rebound) ===
        # 큰 하락 후 당일 저점에서 반등 중인 경우, 차단을 무효화하고 추가 매수 트리거
        rebound_trigger = False
        if REBOUND_BUY_ENABLED and buy_blocked and ohlc and len(ohlc) >= 1:
            try:
                today = ohlc[0]
                today_low = safe_float(today.get('low', 0))
                today_high = safe_float(today.get('high', 0))
                # 오늘 저점에서 충분히 반등 + 당일 양봉 가능성
                bounce_pct = ((current_price - today_low) / today_low * 100) if today_low > 0 else 0
                large_drop = (
                    (is_consecutive_decline and cum_drop_pct >= REBOUND_DROP_THRESHOLD_PCT)
                    or (is_gap_down and gap_drop_pct >= REBOUND_DROP_THRESHOLD_PCT)
                )
                session_rebound_used = existing.get('rebound_buys_this_session', 0)
                # 추세는 5MA 위로 회복 중인지 (단기 반전 신호)
                short_recovering = bool(ma5 and current_price >= ma5)
                if (large_drop
                        and bounce_pct >= REBOUND_INTRADAY_BOUNCE_PCT
                        and short_recovering
                        and session_rebound_used < REBOUND_MAX_BUYS_PER_SESSION):
                    rebound_trigger = True
                    buy_blocked = False
                    block_reasons.append(
                        f"⚡반등매수 트리거 (드롭 {cum_drop_pct or gap_drop_pct:.1f}%, "
                        f"저점반등 +{bounce_pct:.1f}%, 5MA복귀)"
                    )
                    logger.info(
                        f"[{ticker}] ⚡ 오버솔드 반등 매수 트리거 발동 - 매수량 50% 적용"
                    )
            except Exception as _e:
                logger.debug(f"[{ticker}] rebound check skipped: {_e}")

        if is_us_leveraged_etf:
            ma10 = calculate_ma([safe_float(x['clos']) for x in reversed(ohlc)], 10) if len(ohlc) >= 10 else ma20
            is_uptrend_for_buy = check_trend(current_price, ma10) if ma10 else is_uptrend
            if not is_uptrend_for_buy:
                buy_blocked = True
                block_reasons.append("10MA 하회 (레버리지 ETF)")
        else:
            if not is_uptrend:
                buy_blocked = True
                block_reasons.append("20MA 하회")

        dca_reduce_qty = False
        if not is_short_uptrend and is_uptrend:
            dca_reduce_qty = True
            block_reasons.append("5MA 하회 (매수량 50% 축소)")
        if rebound_trigger:
            # 반등 매수는 위험 대비 보수적으로 수량을 50% 적용
            dca_reduce_qty = True

        if buy_blocked:
            logger.info(f"[{ticker}] DCA 매수 차단 - {', '.join(block_reasons)}")
            record_skip_reason(ticker, f"DCA 차단: {', '.join(block_reasons)}")
            return False

        if block_reasons:
            logger.info(f"[{ticker}] DCA 경고: {', '.join(block_reasons)}")
            record_skip_reason(ticker, f"DCA 경고: {', '.join(block_reasons)}")

        # 반등 매수 트리거 시 AI veto는 우회 (오버솔드 진입 기회 확보)
        if not rebound_trigger:
            news = ai.fetch_news()
            sentiment = ai.check_market_sentiment(news, persona=PERSONA)
            if sentiment.get('market_condition') == 'CRASH' or sentiment.get('risk_level') == 'HIGH':
                logger.warning(f"[{ticker}] DCA Paused - AI Risk HIGH: {sentiment.get('reason', 'N/A')}")
                send_alert(f"⚠️ [{ticker}] DCA 중단 - AI 위험 감지: {sentiment.get('reason', 'N/A')}")
                record_skip_reason(ticker, f"AI 위험: {sentiment.get('reason', 'N/A')}")
                return False
        else:
            logger.info(f"[{ticker}] ⚡ 반등 매수 - AI veto 우회")

        qty = calculate_dca_quantity(available_cash, current_price, num_targets, DCA_SETTINGS, market)
        if dca_reduce_qty:
            qty = max(1, qty // 2)
            logger.info(f"[{ticker}] 📉 5MA 하회로 매수량 축소: {qty}주")

        if qty <= 0:
            return False

        currency = "$" if market == 'US' else "₩"
        if market == 'US':
            logger.info(f"[{ticker}] DCA Buy: {qty} shares at ${current_price:.2f}")
            res = kis.buy_market_order(ticker, qty, exchange)
        else:
            logger.info(f"[{ticker}] DCA Buy: {qty} shares at {currency}{current_price:,.0f}")
            res = kis.buy_market_order(ticker, qty)

        if res and res.get('rt_cd') == '0':
            logger.info(f"[{ticker}] ✅ DCA Buy Success! {qty} shares")
            total_qty = existing.get('buys', 0) + qty
            old_buy_price = existing.get('buy_price', 0)
            old_buys = existing.get('buys', 0)
            if old_buy_price > 0 and old_buys > 0:
                blended_price = ((old_buy_price * old_buys) + (current_price * qty)) / total_qty
            else:
                blended_price = current_price

            monitoring_targets[ticker] = {
                'target': current_price,
                'status': 'bought',
                'buys': total_qty,
                'exchange': exchange,
                'buy_price': blended_price,
                'highest_price': max(current_price, existing.get('highest_price', current_price)),
                'ma20': ma20,
                'ma5': ma5,
                'ohlc': ohlc,
                'entry_time': existing.get('entry_time') or datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'dca_buys_this_session': existing.get('dca_buys_this_session', 0) + 1,
                'rebound_buys_this_session': (
                    existing.get('rebound_buys_this_session', 0) + (1 if rebound_trigger else 0)
                ),
                'last_dca_attempt_at': datetime.datetime.now(),
                'dca_reentry_interval_min': existing.get('dca_reentry_interval_min', DCA_REENTRY_INTERVAL_MIN)
            }
            return True

        logger.error(f"[{ticker}] DCA Buy Failed: {res}")
        record_skip_reason(ticker, f"주문 실패: {res.get('msg1', 'unknown') if res else 'unknown'}")
        return False
    
    # Track retry counts for this session
    retry_counts = {}

    # --- 0. Get Account Balance for Dynamic Quantity ---
    available_cash = 0
    total_asset_krw = 0
    try:
        balance = kis.get_balance()
        if market == 'US':
            # US 시장은 get_foreign_balance()로 정확한 USD 잔고 조회
            foreign_bal = kis.get_foreign_balance()
            if foreign_bal and 'deposit' in foreign_bal:
                available_cash = safe_float(foreign_bal['deposit'])
            # US 계좌는 환율 적용하여 원화 환산 (대략 1350원)
            total_asset_krw = available_cash * 1350
        else:
            if balance and 'output2' in balance and balance['output2']:
                if isinstance(balance['output2'], list) and len(balance['output2']) > 0:
                    available_cash = safe_float(balance['output2'][0].get('dnca_tot_amt', 0))
                    total_asset_krw = safe_float(balance['output2'][0].get('tot_evlu_amt', available_cash))
        logger.info(f"Available Cash: {available_cash:,.2f}, Total Asset (KRW): {total_asset_krw:,.0f}")
        
        # 자동 모드 전환 체크 (1000만원 달성 시 레버리지 모드로)
        if check_and_upgrade_mode(total_asset_krw):
            # 모드가 변경되었으면 KR 설정만 다시 로드 (US는 항상 3X)
            user_config = load_config()
            IS_SAFE_MODE = user_config.get("trading_mode") == "safe"
            TARGET_TICKERS_KR = TARGET_TICKERS_KR_1X if IS_SAFE_MODE else TARGET_TICKERS_KR_2X
            if market == 'KR':
                tickers = TARGET_TICKERS_KR
            logger.info(f"🔄 KR 모드 전환 완료! 새로운 KR 타겟: {TARGET_TICKERS_KR}")
            
    except Exception as e:
        logger.error(f"Failed to fetch balance: {e}")

    # --- 0. Check Holding Status (Swing Strategy) ---
    current_holdings = []
    try:
        if balance and 'output1' in balance:
            for h in balance['output1']:
                # US 종목은 ovrs_pdno 필드를 사용
                ticker_id = h.get('ovrs_pdno', '') or h.get('pdno', '')
                # hldg_qty > 0 체크: KIS API는 최근 매도한 종목도 qty=0으로 output1에 포함시킴
                qty_check = int(safe_float(h.get('hldg_qty', h.get('ovrs_cblc_qty', '0')) or '0'))
                if ticker_id and qty_check > 0:
                    current_holdings.append(ticker_id)
    except Exception as e:
        logger.error(f"Failed to parse holdings: {e}")

    logger.info(f"Current Holdings: {current_holdings}")

    # --- [NEW] 자동 전략 최적화 ---
    is_auto_strategy = user_config.get("auto_strategy", False)
    if is_auto_strategy:
        try:
            optimizer = AutoStrategyOptimizer()
            
            # AI 감성 분석 (자동 전략 결정에 활용)
            ai_for_strategy = ai
            auto_sentiment = None
            try:
                news_for_strategy = ai_for_strategy.fetch_news()
                if news_for_strategy:
                    auto_sentiment = ai_for_strategy.check_market_sentiment(news_for_strategy, persona=PERSONA)
                    logger.info(f"[AutoStrategy] AI 감성: condition={auto_sentiment.get('market_condition')}, "
                                f"risk={auto_sentiment.get('risk_level')}, buy={auto_sentiment.get('can_buy')}")
            except Exception as e:
                logger.warning(f"[AutoStrategy] AI 감성 분석 실패 (기술적 분석만 사용): {e}")
            
            # 자동 최적화 실행
            opt_result = optimizer.optimize(
                market=market,
                kis=kis,
                ai_sentiment=auto_sentiment,
                total_asset_krw=total_asset_krw,
                num_holdings=len(current_holdings),
                leverage_threshold=LEVERAGE_THRESHOLD_KRW
            )
            
            # 결과 적용 (globals 업데이트)
            new_cfg = opt_result.get('current', {})
            IS_SAFE_MODE = new_cfg.get('trading_mode', 'safe') == 'safe'
            STRATEGY_MODE = new_cfg.get('strategy', STRATEGY_MODE)
            PERSONA = new_cfg.get('persona', PERSONA)
            
            # KR 타겟 재설정
            TARGET_TICKERS_KR = TARGET_TICKERS_KR_1X if IS_SAFE_MODE else TARGET_TICKERS_KR_2X
            if market == 'KR':
                current_dynamic_targets = [t for t in DYNAMIC_TARGETS if t.get('exchange') == 'KR']
                tickers = TARGET_TICKERS_KR + current_dynamic_targets
            
            # 변경 알림
            if opt_result.get('changed'):
                change_msg = (f"🤖 [자동전략] 전략 변경!\n"
                              f"전략: {STRATEGY_MODE.upper()}\n"
                              f"모드: {'Safe' if IS_SAFE_MODE else 'Risky'}\n"
                              f"페르소나: {PERSONA}\n"
                              f"사유: {opt_result['decision'].get('reason', '')}\n"
                              f"신뢰도: {opt_result['decision'].get('confidence', 0):.0%}")
                send_alert(change_msg)
                logger.info(f"[AutoStrategy] 전략 변경 알림 발송 완료")
            else:
                logger.info(f"[AutoStrategy] 현재 전략 유지: {STRATEGY_MODE}/{('safe' if IS_SAFE_MODE else 'risky')}/{PERSONA}")
            
            # DCA 전략 선택 시 buy_delay 재적용
            if STRATEGY_MODE == 'dca':
                buy_delay = DCA_SETTINGS.get("buy_delay_minutes", 0)
                # (이미 위에서 delay를 처리했다면 중복 방지)
            
        except Exception as e:
            logger.error(f"[AutoStrategy] 자동 전략 최적화 실패 (기존 설정 유지): {e}")
    
    # 활성 타겟 수 (분산 투자 계산용)
    num_active_targets = len(tickers)

    # 1. Initialize Targets for each ticker
    for t_obj in tickers:
        if isinstance(t_obj, dict):
            ticker = t_obj['symbol']
            exchange = t_obj['exchange']
        else:
            ticker = t_obj
            exchange = None

        logger.info(f"Analyzing {ticker}...")
        
        # A. Trend Check (20MA)
        if market == 'US':
            ohlc = kis.get_daily_ohlc(ticker, exchange)
        else:
            ohlc = kis.get_daily_ohlc(ticker)
            
        if not ohlc:
            logger.error(f"[{ticker}] Failed to get OHLC. Skipping.")
            continue

        closes = [safe_float(x['clos']) for x in ohlc]
        closes.reverse() 
        
        ma20 = calculate_ma(closes, 20)
        
        if market == 'US':
            current_price = kis.get_current_price(ticker, exchange)
        else:
            current_price = kis.get_current_price(ticker)
        
        if not current_price:
             logger.error(f"[{ticker}] Failed to get Current Price. Skipping.")
             continue

        logger.info(f"[{ticker}] Current: {current_price}, MA20: {ma20}")
        
        # --- 시장/종목별 리스크 파라미터 결정 ---
        is_kr_stock = (market == 'KR' and ticker not in KR_ETF_CODES)
        if is_kr_stock:
            eff_stop_loss = KR_STOCK_STOP_LOSS_PCT
            eff_trailing_activation = KR_STOCK_TRAILING_ACTIVATION
            eff_trailing_drop = KR_STOCK_TRAILING_DROP
            eff_gap_down_threshold = KR_STOCK_GAP_DOWN_THRESHOLD
        else:
            eff_stop_loss = STOP_LOSS_PCT
            eff_trailing_activation = TRAILING_STOP_ACTIVATION
            eff_trailing_drop = TRAILING_STOP_DROP
            eff_gap_down_threshold = GAP_DOWN_THRESHOLD
        
        # --- STRATEGY BRANCHING ---
        is_uptrend = check_trend(current_price, ma20)
        
        # === [NEW] 단기 이동평균 (5MA) - 급락 조기 감지 ===
        ma5 = calculate_short_ma(closes, 5)
        is_short_uptrend = check_trend(current_price, ma5) if ma5 else True
        
        # === [NEW] 갭다운 감지 - 전일 종가 대비 급락 체크 ===
        is_gap_down, gap_drop_pct = check_gap_down(current_price, ohlc, eff_gap_down_threshold)
        if is_gap_down:
            logger.warning(f"[{ticker}] ⚠️ GAP DOWN detected! ({gap_drop_pct:.1f}% drop from prev close)")
            send_alert(f"⚠️ [{ticker}] 갭다운 감지! 전일 대비 {gap_drop_pct:.1f}% 급락")
        
        # === [NEW] 연속 하락 감지 ===
        is_consecutive_decline, cum_drop_pct = check_consecutive_decline(
            ohlc, CONSECUTIVE_DECLINE_DAYS, CONSECUTIVE_DECLINE_PCT
        )
        if is_consecutive_decline:
            logger.warning(f"[{ticker}] ⚠️ CONSECUTIVE DECLINE! ({cum_drop_pct:.1f}% over {CONSECUTIVE_DECLINE_DAYS} days)")
            send_alert(f"⚠️ [{ticker}] {CONSECUTIVE_DECLINE_DAYS}일 연속 하락! 누적 {cum_drop_pct:.1f}%")

        # DCA 전략
        if STRATEGY_MODE == 'dca':
            # === [NEW] Step 0: 기존 보유분 리스크 체크 (DCA도 반드시 실행) ===
            if ticker in current_holdings:
                holding_qty = 0
                holding_avg_price = 0
                try:
                    for h in balance.get('output1', []):
                        if h.get('pdno') == ticker or h.get('ovrs_pdno') == ticker:  # h.get() 사용 (KeyError 방지)
                            holding_qty = int(safe_float(h.get('hldg_qty', h.get('ovrs_cblc_qty', h.get('ord_psbl_qty', '0')))))
                            holding_avg_price = safe_float(h.get('pchs_avg_pric', h.get('avg_unpr3', 0)))
                            break
                except:
                    pass

                # 실제 보유 수량이 0이면 skip (KIS API가 매도 후에도 qty=0으로 반환하는 경우 대비)
                if holding_qty <= 0:
                    logger.info(f"[{ticker}] 보유 수량 0, 매도 스킵 (이미 매도된 종목)")
                    continue

                if holding_avg_price > 0 and current_price > 0:
                    pnl_pct = ((current_price - holding_avg_price) / holding_avg_price) * 100
                    logger.info(f"[{ticker}] 📊 보유현황: {holding_qty}주, 평단가: {holding_avg_price:.2f}, 현재가: {current_price:.2f}, 손익: {pnl_pct:.2f}%")
                    
                    # Stop Loss - 시장/종목별 차별화 (KR 개별주는 완화)
                    if pnl_pct <= eff_stop_loss:
                        logger.warning(f"[{ticker}] 🛑 DCA STOP LOSS! ({pnl_pct:.2f}% <= {eff_stop_loss}%). 즉시 매도 {holding_qty}주.")
                        send_alert(f"🛑 [{ticker}] DCA 손절매 발동! {pnl_pct:.2f}%. {holding_qty}주 매도.")
                        _dca_sl = safe_sell(kis, market, ticker, holding_qty, exchange,
                                            reason='DCA손절매',
                                            monitor_data=monitoring_targets.get(ticker))
                        if _dca_sl['phase'] == 'deferred':
                            logger.info(f"[{ticker}] DCA 손절매 보류 (장 마감)")
                            # 모니터링 상태는 그대로 두어 다음 사이클에서 재평가
                            continue
                        if _dca_sl['success'] or _dca_sl['phase'] == 'already_flat':
                            monitoring_targets[ticker] = {
                                'target': current_price, 'status': 'sold_sl',
                                'buys': 0, 'exchange': exchange
                            }
                        else:
                            logger.error(
                                f"[{ticker}] ❌ DCA 손절매 실패: {_dca_sl['error']}"
                            )
                            send_alert(
                                f"🚨 [{ticker}] DCA 손절매 실패! 수동 확인 필요.\n오류: {_dca_sl['error']}",
                                is_error=True
                            )
                            monitoring_targets[ticker] = {
                                'target': current_price, 'status': 'stop_loss_failed',
                                'buys': holding_qty, 'exchange': exchange,
                                'buy_price': holding_avg_price if holding_avg_price > 0 else current_price,
                                'stop_loss_fail_count': 1
                            }
                        continue  # 손절 후 추가 매수 안 함
                monitoring_targets[ticker] = {
                    'target': current_price,
                    'status': 'bought',
                    'buys': holding_qty,
                    'exchange': exchange,
                    'buy_price': holding_avg_price if holding_avg_price > 0 else current_price,
                    'highest_price': current_price,
                    'ma20': ma20,
                    'ma5': ma5,
                    'ohlc': ohlc,
                    'dca_buys_this_session': 0,
                    'last_dca_attempt_at': None,
                    'dca_reentry_interval_min': (
                        DCA_LEVERAGED_REENTRY_INTERVAL_MIN if (market == 'US' and ticker in US_LEVERAGED_ETF_SYMBOLS)
                        else DCA_REENTRY_INTERVAL_MIN
                    )
                }
                continue  # 이미 보유 중인 종목은 리스크 관리만 수행

            prepare_dca_wait_target(ticker, exchange, current_price, ma20, ma5, ohlc)
            attempt_dca_buy(ticker, exchange, current_price, ma20, ma5, ohlc, eff_gap_down_threshold, num_active_targets)
            continue  # DCA는 주기적 재평가 대상으로 전환
        
        # Case 1: Already Holding
        if ticker in current_holdings:
            # 보유 수량 조회
            holding_qty = 0
            holding_avg_price = 0
            try:
                for h in balance.get('output1', []):
                    if h.get('pdno') == ticker or h.get('ovrs_pdno') == ticker:
                        holding_qty = int(safe_float(h.get('hldg_qty', h.get('ovrs_cblc_qty', '0'))))
                        holding_avg_price = safe_float(h.get('pchs_avg_pric', h.get('avg_unpr3', 0)))
                        break
            except:
                pass

            # 실제 보유 수량이 0이면 skip (current_holdings 필터를 통과했더라도 재확인)
            if holding_qty <= 0:
                logger.info(f"[{ticker}] 보유 수량 0, 매도 스킵 (이미 매도된 종목)")
                continue

            if not is_uptrend and not is_short_uptrend:
                logger.info(f"[{ticker}] Trend Broken (Price < 20MA and < 5MA). Selling {holding_qty} shares immediately.")
                sell_res = None
                for _attempt in range(3):
                    if market == 'US':
                        sell_res = kis.sell_market_order(ticker, holding_qty, exchange)
                    else:
                        sell_res = kis.sell_market_order(ticker, holding_qty)
                    if sell_res and sell_res.get('rt_cd') == '0':
                        break
                    logger.warning(f"[{ticker}] Trend-break sell attempt {_attempt+1} failed: {sell_res}. Retrying...")
                    time.sleep(2)

                if sell_res and sell_res.get('rt_cd') == '0':
                    logger.info(f"[{ticker}] ✅ Trend-break sell success.")
                    today_open = safe_float(ohlc[0]['open'])
                    reentry_target = calculate_target_price(today_open, ohlc, K_VALUE)
                    monitoring_targets[ticker] = {
                        'target': reentry_target,
                        'status': 'reentry_watch',
                        'buys': 0,
                        'exchange': exchange,
                        'ma20': ma20,
                        'ma5': ma5,
                        'ohlc': ohlc,
                        'last_reentry_check_at': None,
                        'reentry_cooldown_sec': TREND_REENTRY_COOLDOWN_SEC
                    }
                    logger.info(f"[{ticker}] Re-entry watch enabled (cooldown {TREND_REENTRY_COOLDOWN_SEC}s).")
                else:
                    err_msg = sell_res.get('msg1', 'unknown') if sell_res else 'no response'
                    logger.error(f"[{ticker}] ❌ Trend-break sell FAILED after 3 attempts: {err_msg}")
                    send_alert(f"🚨 [{ticker}] 추세이탈 매도 실패! 수동 확인 필요.\n오류: {err_msg}", is_error=True)
                    # 포지션 유지 - 다음 루프에서 재시도
                    monitoring_targets[ticker] = {
                        'target': 9999999,
                        'status': 'bought',
                        'buys': holding_qty,
                        'exchange': exchange,
                        'buy_price': holding_avg_price if holding_avg_price > 0 else current_price,
                        'highest_price': current_price,
                        'ma20': ma20,
                        'ma5': ma5,
                        'ohlc': ohlc,
                    }
                continue
            else:
                logger.info(f"[{ticker}] Trend OK. Holding {holding_qty} shares.")
                # Mark as bought to prevent duplicate buy
                monitoring_targets[ticker] = {
                    'target': 9999999, # Dummy target
                    'status': 'bought',
                    'buys': holding_qty,
                    'exchange': exchange,
                    'buy_price': holding_avg_price if holding_avg_price > 0 else current_price,
                    'highest_price': current_price
                }
                continue

        # Case 2: New Entry (Trend Analysis)
        if not is_uptrend:
            logger.info(f"[{ticker}] Bear Market (Price < 20MA). Skipping (will recheck mid-session).")
            # MA20 하회 종목은 mid-session 재검토를 위해 downtrend_watch로 등록
            if market == 'US':
                monitoring_targets[ticker] = {
                    'target': 0,
                    'status': 'downtrend_watch',
                    'buys': 0,
                    'exchange': exchange,
                    'ma20': ma20,
                    'ma5': ma5,
                    'ohlc': ohlc,
                    'last_recheck_at': None,
                }
            continue

         # B. Calculate Target Price (Volatility Breakout)
        # 실제 오늘 시가(today_open): quote API에서 실시간 시가 조회, 없으면 ohlc 폴백
        today_open = 0.0
        if market == 'US':
            try:
                _q = kis.get_quote(ticker, exchange)
                today_open = safe_float(_q.get('open')) if _q else 0.0
            except Exception:
                pass
        if today_open <= 0:
            today_open = safe_float(ohlc[0]['open'])  # fallback: 전일 시가
        target_price = calculate_target_price(today_open, ohlc, K_VALUE)
        logger.info(f"[{ticker}] Bull Market! Target Price: {target_price} (Today Open: {today_open})")
        
        monitoring_targets[ticker] = {
            'target': target_price,
            'status': 'monitoring',  # monitoring, bought
            'buys': 0,
            'exchange': exchange,
            'ma20': ma20,
            'ohlc': ohlc  # 신호 강도 계산용
        }

    if not monitoring_targets:
        logger.info(f"[{market}] No targets found for today. Sleeping.")
        return

    # === [NEW] 포트폴리오 드로다운 체크 ===
    try:
        holdings_for_check = []
        if balance and 'output1' in balance:
            holdings_for_check = balance['output1']
        is_drawdown, total_loss_pct = check_portfolio_drawdown(holdings_for_check, PORTFOLIO_DRAWDOWN_PCT)
        if is_drawdown:
            logger.critical(f"🚨 PORTFOLIO DRAWDOWN ALERT! 전체 포트폴리오 손실 {total_loss_pct:.1f}% (기준: {PORTFOLIO_DRAWDOWN_PCT}%)")
            send_alert(f"🚨 포트폴리오 드로다운 경고! 전체 손실 {total_loss_pct:.1f}%. 신규 매수 중단.")
    except Exception as e:
        logger.error(f"Portfolio drawdown check failed: {e}")

    # DCA 모드: 매수 후에도 감시 루프 진입 (보유분 StopLoss/TrailingStop 모니터링)
    if STRATEGY_MODE == 'dca':
        bought_positions = {k: v for k, v in monitoring_targets.items() if v['status'] == 'bought'}
        waiting_positions = {k: v for k, v in monitoring_targets.items() if v['status'] == 'dca_wait'}
        logger.info(
            f"[{market}] DCA Mode - Monitoring bought={list(bought_positions.keys())}, "
            f"waiting={list(waiting_positions.keys())}, base_reentry={DCA_REENTRY_INTERVAL_MIN}min, "
            f"leveraged_reentry={DCA_LEVERAGED_REENTRY_INTERVAL_MIN}min"
        )

    # 활성 모니터링 대상 수 업데이트
    num_active_targets = len([t for t in monitoring_targets.values() if t['status'] == 'monitoring'])
    logger.info(f"[{market}] Watch List: {list(monitoring_targets.keys())} ({num_active_targets} active)")
    
    # 2. Watch Loop
    last_scan_time = datetime.datetime.now()

    while True:
        # Check if market closed
        current_market = get_market_status()
        if current_market != market:
            logger.info(f"[{market}] Market Closed. Ending Session.")
            break
            
        # --- [New] US Mid-Session Re-analysis: downtrend_watch 종목 재검토 ---
        # 5분(300초)마다 실행: 장 시작에 MA20 하회로 제외된 US 종목이 추세 회복 시 편입
        if market == 'US' and (datetime.datetime.now() - last_scan_time).total_seconds() > 300:
            last_scan_time = datetime.datetime.now()
            downtrend_tickers = [
                (t, d) for t, d in monitoring_targets.items() if d['status'] == 'downtrend_watch'
            ]
            if downtrend_tickers:
                logger.info(f"🔄 [US] Mid-Session Re-check: {[t for t, _ in downtrend_tickers]}")
            for ticker, data in downtrend_tickers:
                try:
                    ohlc = kis.get_daily_ohlc(ticker, data['exchange'])
                    if not ohlc:
                        continue
                    closes = [safe_float(x['clos']) for x in ohlc]
                    closes.reverse()
                    ma20 = calculate_ma(closes, 20)
                    curr_p = kis.get_current_price(ticker, data['exchange'])
                    if not curr_p:
                        continue
                    if not check_trend(curr_p, ma20):
                        logger.info(f"   [{ticker}] Still downtrend (Price {curr_p:.2f} < MA20 {ma20:.2f}). Skipping.")
                        data['last_recheck_at'] = datetime.datetime.now()
                        continue
                    # 추세 회복 → VBO 타겟 계산 후 monitoring으로 편입
                    _q = kis.get_quote(ticker, data['exchange'])
                    today_open = safe_float(_q.get('open')) if _q else 0.0
                    if today_open <= 0:
                        today_open = safe_float(ohlc[0]['open'])
                    target_price = calculate_target_price(today_open, ohlc, K_VALUE)
                    data.update({
                        'target': target_price,
                        'status': 'monitoring',
                        'ma20': ma20,
                        'ohlc': ohlc,
                    })
                    logger.info(
                        f"   ✨ [{ticker}] Trend Recovered! Added to Watch List. "
                        f"Target: {target_price:.2f} (Open: {today_open:.2f}, MA20: {ma20:.2f})"
                    )
                    send_alert(f"✨ [{ticker}] 추세 회복 → 감시 목록 편입! Target: {target_price:.2f}")
                except Exception as e:
                    logger.error(f"   [{ticker}] Mid-session recheck error: {e}")

        # --- [New] Dynamic Scanning during session (KR Only) ---
        # Run every 5 minutes (300 seconds)
        if market == 'KR' and (datetime.datetime.now() - last_scan_time).total_seconds() > 300:
            logger.info("🔄 Running Mid-Session Scanner...")
            last_scan_time = datetime.datetime.now()
            try:
                # Scan for opportunities
                # Lower threshold to capture more movement as per user request
                spikes = scanner.scan_volume_spikes(min_volume_increase_rate=200) 
                blue_chips = scanner.scan_blue_chip_surge(min_gain=2.0)
                found = spikes + blue_chips
                
                for item in found:
                    ticker = item['code']
                    # Skip if already monitoring or failed/sold or known bad
                    if ticker in monitoring_targets or ticker in FAILED_TICKERS: continue
                    
                    logger.info(f"✨ New Candidate Found: {ticker} ({item['name']}) - {item['reason']}")
                    
                    # Initialize Analysis for New Ticker
                    ohlc = kis.get_daily_ohlc(ticker)
                    if not ohlc: continue
                    
                    closes = [safe_float(x['clos']) for x in ohlc]
                    closes.reverse()
                    ma20 = calculate_ma(closes, 20)
                    
                    # Get fresh current price
                    curr_p = kis.get_current_price(ticker)
                    if not curr_p: curr_p = item['price']
                    
                    is_uptrend = check_trend(curr_p, ma20)
                    
                    # Apply Trend Filter (unless DCA mode which might be more lenient, but sticking to safe defaults)
                    if not is_uptrend:
                        logger.info(f"   -> [Skipped] Downtrend (Price {curr_p} < MA20 {ma20})")
                        continue
                        
                    today_open = safe_float(ohlc[0]['open'])
                    target_price = calculate_target_price(today_open, ohlc, K_VALUE)
                    
                    monitoring_targets[ticker] = {
                        'target': target_price,
                        'status': 'monitoring',
                        'buys': 0,
                        'exchange': None,
                        'ma20': ma20,
                        'ohlc': ohlc,
                        'source': 'scanner' 
                    }
                    # Update active count
                    num_active_targets = len([t for t in monitoring_targets.values() if t['status'] == 'monitoring'])
                    logger.info(f"   -> Added to Watch List. Target: {target_price}")
                    
            except Exception as e:
                logger.error(f"Mid-session scan failed: {e}")

        for ticker, data in monitoring_targets.items():
            # --- [Rule No.0] 이전 루프에서 실패한 손절매 재시도 ---
            if data['status'] == 'stop_loss_failed':
                try:
                    qty_hint = data.get('buys', 0)
                    fail_count = data.get('stop_loss_fail_count', 1)

                    # 최대 시도 한도 도달: 강제 정리 (실제 잔고는 다음 잔고 동기화 시 갱신)
                    if fail_count >= STOP_LOSS_MAX_RETRY_CYCLES:
                        logger.error(
                            f"[{ticker}] 손절매 {fail_count}회 한도 도달 → 모니터링 종료(sold_sl), 수동 확인 필요"
                        )
                        send_alert(
                            f"🚨 [{ticker}] 손절매 {fail_count}회 실패 후 모니터링 종료. 수동 확인 필요.",
                            is_error=True
                        )
                        data['status'] = 'sold_sl'
                        continue

                    _retry = safe_sell(
                        kis, market, ticker, qty_hint, data.get('exchange'),
                        reason='손절매-재시도',
                        monitor_data=data,
                    )

                    if _retry['phase'] == 'deferred':
                        # 장 마감 등으로 보류 → 알림 안 보냄, 다음 사이클에 자동 재시도
                        continue
                    if _retry['phase'] == 'already_flat':
                        logger.info(f"[{ticker}] 손절매 재시도 - 실제 잔고 0 (이미 청산). sold_sl 처리.")
                        data['status'] = 'sold_sl'
                        continue
                    if _retry['success']:
                        send_alert(
                            f"✅ [{ticker}] 손절매 재시도 성공 ({_retry['sold_qty']}주, {_retry['phase']})."
                        )
                        data['status'] = 'sold_sl'
                    else:
                        fail_count += 1
                        data['stop_loss_fail_count'] = fail_count
                        logger.error(
                            f"[{ticker}] ❌ 손절매 재시도 실패 (누적 {fail_count}회): {_retry['error']}"
                        )
                        # 첫 실패 직후 1회만 알림 (스팸 방지) - 한도 도달 시 위에서 별도 알림
                        if fail_count == 2:
                            send_alert(
                                f"🚨 [{ticker}] 손절매 연속 실패. 시장가/지정가 모두 거부됨.\n오류: {_retry['error']}",
                                is_error=True
                            )
                except Exception as _e:
                    logger.error(f"[{ticker}] 손절매 재시도 예외: {_e}")
                continue

            # --- [Rule No.1] Risk Management for Bought Positions ---
            if data['status'] == 'bought':
                try:
                    # Fetch current price
                    if market == 'US':
                        quote = kis.get_quote(ticker, data.get('exchange'))
                        curr = safe_float(quote.get('last')) if quote else 0
                    else:
                        curr = kis.get_current_price(ticker)
                        
                    if curr <= 0: continue

                    buy_price = data.get('buy_price', 0)
                    highest_price = data.get('highest_price', curr)
                    qty = data.get('buys', 0)

                    if buy_price <= 0:
                        logger.warning(f"[{ticker}] Missing buy_price in monitoring target. Skipping risk calculation.")
                        data['buy_price'] = curr
                        data['highest_price'] = max(highest_price, curr)
                        continue
                    
                    # Update Highest Price (for Trailing Stop)
                    if curr > highest_price:
                        monitoring_targets[ticker]['highest_price'] = curr
                        highest_price = curr
                        
                    # Calculate PnL
                    pnl_pct = ((curr - buy_price) / buy_price) * 100
                    
                    # 시장/종목별 리스크 파라미터
                    is_kr_stock_pos = (market == 'KR' and ticker not in KR_ETF_CODES)
                    pos_stop_loss = KR_STOCK_STOP_LOSS_PCT if is_kr_stock_pos else STOP_LOSS_PCT
                    pos_trailing_act = KR_STOCK_TRAILING_ACTIVATION if is_kr_stock_pos else TRAILING_STOP_ACTIVATION
                    pos_trailing_drop = KR_STOCK_TRAILING_DROP if is_kr_stock_pos else TRAILING_STOP_DROP

                    # [v2.5] ATR 동적 손절폭 적용 (보수적 = 절대값 큰 쪽 채택)
                    try:
                        pos_stop_loss = effective_stop_loss_pct(pos_stop_loss, data.get('ohlc'))
                    except Exception:
                        pass

                    # [v2.5] Breakeven Stop 활성화 판정
                    be_trigger = get_breakeven_trigger_pct(market, ticker)
                    if BREAKEVEN_ENABLED and be_trigger is not None and not data.get('breakeven_armed'):
                        # 고점 기준이 아닌 현재 pnl 기준으로 한 번이라도 +trigger% 를 찍었는지 본다
                        if pnl_pct >= be_trigger or ((highest_price - buy_price) / buy_price * 100) >= be_trigger:
                            data['breakeven_armed'] = True
                            data['breakeven_price'] = buy_price * (1.0 + BREAKEVEN_BUFFER_PCT / 100.0)
                            logger.info(
                                f"[{ticker}] 🛡️ Breakeven 활성화 (peak +{be_trigger}% 도달) → "
                                f"floor={data['breakeven_price']:.4f}"
                            )

                    # [v2.5] Breakeven floor 도달 시 즉시 청산 (손실 전환 차단)
                    if data.get('breakeven_armed') and curr <= float(data.get('breakeven_price', 0)):
                        logger.warning(
                            f"[{ticker}] 🛡️ Breakeven Stop! curr={curr:.4f} <= "
                            f"floor={data['breakeven_price']:.4f}. {qty}주 매도."
                        )
                        send_alert(f"🛡️ [{ticker}] 본전 스탑 발동! {qty}주 매도 (pnl≈{pnl_pct:.2f}%).")
                        _be = safe_sell(kis, market, ticker, qty, data.get('exchange'),
                                        reason='breakeven_stop', monitor_data=data)
                        if _be['phase'] == 'deferred':
                            continue
                        if _be['phase'] == 'already_flat':
                            data['status'] = 'sold_be'
                            continue
                        if _be['success']:
                            data['status'] = 'sold_be'
                            try:
                                _record_loss_event(pnl_pct, 'breakeven_stop')
                            except Exception:
                                pass
                        else:
                            logger.error(f"[{ticker}] ❌ Breakeven sell FAILED: {_be['error']}")
                            data['status'] = 'be_failed'
                        continue

                    # 1. Stop Loss - ABSOLUTE RULE (시장별 차별화)
                    if pnl_pct <= pos_stop_loss:
                        logger.warning(f"[{ticker}] 🛑 Stop Loss Triggered! ({pnl_pct:.2f}% <= {pos_stop_loss}%). Selling {qty} shares.")
                        send_alert(f"🛑 [{ticker}] 손절매 발동! {pnl_pct:.2f}%. {qty}주 매도.")
                        _sl = safe_sell(kis, market, ticker, qty, data.get('exchange'),
                                        reason='손절매',
                                        monitor_data=data)
                        if _sl['phase'] == 'deferred':
                            # 장 마감: 다음 KR 정규장 시작 시 자동 재평가됨
                            logger.info(f"[{ticker}] 손절매 보류 (장 마감) - 다음 장에서 재시도")
                            continue
                        if _sl['phase'] == 'already_flat':
                            logger.info(f"[{ticker}] 손절매 트리거됐지만 실제 보유 0 (외부 청산). sold_sl 처리.")
                            data['status'] = 'sold_sl'
                            continue
                        if _sl['success']:
                            data['status'] = 'sold_sl'
                            try:
                                _record_loss_event(pnl_pct, 'stop_loss')
                            except Exception:
                                pass
                        else:
                            logger.error(
                                f"[{ticker}] ❌ Stop-loss sell FAILED (시장가+지정가): {_sl['error']}"
                            )
                            send_alert(
                                f"🚨 [{ticker}] 손절매 주문 실패! 수동 확인 필요.\n오류: {_sl['error']}",
                                is_error=True
                            )
                            data['status'] = 'stop_loss_failed'
                            data['stop_loss_fail_count'] = 1
                        continue
                        
                    # 2. Trailing Stop (시장별 차별화)
                    peak_pnl_pct = ((highest_price - buy_price) / buy_price) * 100

                    # 2-A. Partial Take-Profit (1차 부분 익절, v2.4)
                    # peak_pnl_pct 가 trailing 활성가에 도달했고 아직 1차 청산을 안 했다면
                    # 보유의 PARTIAL_TP_RATIO 만 먼저 청산하고 나머지는 trailing 유지.
                    if (PARTIAL_TP_ENABLED
                            and peak_pnl_pct >= pos_trailing_act
                            and not data.get('partial_tp_done')
                            and qty >= PARTIAL_TP_MIN_QTY):
                        partial_qty = max(1, int(qty * PARTIAL_TP_RATIO))
                        # 최소 1주는 trailing 으로 남겨야 의미가 있음
                        if partial_qty >= qty:
                            partial_qty = qty - 1
                        if partial_qty >= 1:
                            logger.info(
                                f"[{ticker}] 🎯 Partial TP! Peak {peak_pnl_pct:.2f}% "
                                f"→ {partial_qty}/{qty}주 1차 청산 (나머지 trailing 유지)"
                            )
                            _ptp = safe_sell(
                                kis, market, ticker, partial_qty, data.get('exchange'),
                                reason='부분익절', monitor_data=data,
                            )
                            if _ptp['phase'] == 'deferred':
                                logger.info(f"[{ticker}] 부분익절 보류 (장 마감)")
                                continue
                            if _ptp['phase'] == 'already_flat':
                                # 이미 외부 청산됨 → 전량 sold_tp 처리
                                logger.info(f"[{ticker}] 부분익절 - 실제 보유 0. sold_tp 처리.")
                                data['status'] = 'sold_tp'
                                continue
                            if _ptp['success']:
                                # 남은 수량 갱신 + 마킹
                                remaining = max(0, qty - _ptp['sold_qty'])
                                data['buys'] = remaining
                                data['partial_tp_done'] = True
                                data['partial_tp_qty'] = _ptp['sold_qty']
                                data['partial_tp_price'] = curr
                                send_alert(
                                    f"🎯 [{ticker}] 1차 부분익절 {_ptp['sold_qty']}주 "
                                    f"@{curr:.2f} (peak {peak_pnl_pct:.2f}%). "
                                    f"잔여 {remaining}주 trailing 유지."
                                )
                                # 잔여 수량이 0 이면 trailing 검사 의미 없음
                                if remaining <= 0:
                                    data['status'] = 'sold_tp'
                                continue
                            else:
                                # 부분익절 실패 → trailing 로직 그대로 진행 (전량 청산 시도)
                                logger.warning(
                                    f"[{ticker}] 부분익절 실패({_ptp['error']}) → 전량 trailing 로 진행"
                                )

                    if peak_pnl_pct >= pos_trailing_act:
                        drop_from_peak = ((highest_price - curr) / highest_price) * 100
                        if drop_from_peak >= pos_trailing_drop:
                            logger.info(f"[{ticker}] 💰 Trailing Stop Triggered! (Peak: {peak_pnl_pct:.2f}%, Drop: {drop_from_peak:.2f}% >= {pos_trailing_drop}%). Selling {qty} shares.")
                            send_alert(f"💰 [{ticker}] 트레일링 스탑! 고점 대비 {drop_from_peak:.2f}% 하락. {qty}주 매도.")
                            _tp = safe_sell(kis, market, ticker, qty, data.get('exchange'),
                                            reason='트레일링스탑',
                                            monitor_data=data)
                            if _tp['phase'] == 'deferred':
                                logger.info(f"[{ticker}] 트레일링스탑 보류 (장 마감)")
                                continue
                            if _tp['phase'] == 'already_flat':
                                logger.info(f"[{ticker}] 트레일링스탑 - 실제 보유 0. sold_tp 처리.")
                                data['status'] = 'sold_tp'
                                continue
                            if _tp['success']:
                                data['status'] = 'sold_tp'
                            else:
                                logger.error(
                                    f"[{ticker}] ❌ Trailing-stop sell FAILED: {_tp['error']}"
                                )
                                send_alert(
                                    f"🚨 [{ticker}] 트레일링스탑 주문 실패! 수동 확인 필요.\n오류: {_tp['error']}",
                                    is_error=True
                                )
                                data['status'] = 'tp_failed'  # 반복 알람 방지
                            continue
                            
                except Exception as e:
                    logger.error(f"[{ticker}] Error in Risk Management: {e}")
                
                continue # Skip breakout check if already bought

            if STRATEGY_MODE == 'dca':
                if data['status'] != 'dca_wait':
                    continue

                now_dt = datetime.datetime.now()
                reentry_interval_min = data.get('dca_reentry_interval_min', DCA_REENTRY_INTERVAL_MIN)
                last_attempt_at = data.get('last_dca_attempt_at')
                if last_attempt_at and (now_dt - last_attempt_at).total_seconds() < (reentry_interval_min * 60):
                    continue

                data['last_dca_attempt_at'] = now_dt

                exchange = data.get('exchange')
                if market == 'US':
                    current_price = kis.get_current_price(ticker, exchange)
                else:
                    current_price = kis.get_current_price(ticker)

                if not current_price:
                    continue

                refreshed_ohlc = None
                refreshed_ma20 = data.get('ma20')
                refreshed_ma5 = data.get('ma5')
                try:
                    if market == 'US':
                        refreshed_ohlc = kis.get_daily_ohlc(ticker, exchange)
                    else:
                        refreshed_ohlc = kis.get_daily_ohlc(ticker)
                    if refreshed_ohlc:
                        closes = [safe_float(x['clos']) for x in refreshed_ohlc]
                        closes.reverse()
                        refreshed_ma20 = calculate_ma(closes, 20)
                        refreshed_ma5 = calculate_short_ma(closes, 5)
                        data['ohlc'] = refreshed_ohlc
                        data['ma20'] = refreshed_ma20
                        data['ma5'] = refreshed_ma5
                except Exception as e:
                    logger.warning(f"[{ticker}] DCA snapshot refresh failed: {e}")

                logger.info(f"[{ticker}] DCA 장중 재평가 ({reentry_interval_min}분 주기) - 현재가 {current_price}")
                attempt_dca_buy(
                    ticker,
                    exchange,
                    current_price,
                    refreshed_ma20,
                    refreshed_ma5,
                    refreshed_ohlc if refreshed_ohlc else data.get('ohlc', []),
                    KR_STOCK_GAP_DOWN_THRESHOLD if (market == 'KR' and ticker not in KR_ETF_CODES) else GAP_DOWN_THRESHOLD,
                    num_active_targets if num_active_targets > 0 else len(monitoring_targets)
                )
                continue

            if data['status'] == 'reentry_watch':
                now_dt = datetime.datetime.now()
                last_check = data.get('last_reentry_check_at')
                cooldown_sec = data.get('reentry_cooldown_sec', TREND_REENTRY_COOLDOWN_SEC)
                if last_check and (now_dt - last_check).total_seconds() < cooldown_sec:
                    continue

                data['last_reentry_check_at'] = now_dt
                exchange = data.get('exchange')
                current_price, refreshed_ohlc, refreshed_ma20, refreshed_ma5 = refresh_ticker_snapshot(ticker, exchange)
                if not current_price or not refreshed_ohlc:
                    continue

                is_reentry_uptrend = check_trend(current_price, refreshed_ma20)
                is_reentry_short_uptrend = check_trend(current_price, refreshed_ma5) if refreshed_ma5 else True

                if is_reentry_uptrend:
                    # MA20 위로 복귀 시 변동성돌파 대기 없이 즉시 재진입
                    # MA5는 hard block 대신 수량 50% 축소로 완화
                    reduce_qty = not is_reentry_short_uptrend

                    if ticker in FAILED_TICKERS:
                        logger.warning(f"[{ticker}] Re-entry blocked: account restriction")
                        continue

                    qty = calculate_order_quantity(available_cash, current_price, 0.6, num_active_targets)
                    if reduce_qty:
                        qty = max(1, qty // 2)
                        logger.info(f"[{ticker}] 🔄 Re-entry: MA20 복귀, MA5 미복귀 → 수량 50% 축소 {qty}주")
                    else:
                        logger.info(f"[{ticker}] 🔄 Re-entry: MA20+MA5 모두 복귀 → 전량 {qty}주")

                    if market == 'US':
                        res = kis.buy_market_order(ticker, qty, exchange)
                    else:
                        res = kis.buy_market_order(ticker, qty)

                    if res and res.get('rt_cd') == '0':
                        logger.info(f"[{ticker}] ✅ Re-entry Buy Success! {qty}주 @ {current_price}")
                        send_alert(
                            f"🔄 [{ticker}] 추세 복귀 재진입 성공!\n"
                            f"{qty}주 @ {current_price:.2f}"
                            f"{' (수량 50% 축소 - MA5 미복귀)' if reduce_qty else ''}"
                        )
                        monitoring_targets[ticker] = {
                            'target': current_price,
                            'status': 'bought',
                            'buys': qty,
                            'exchange': exchange,
                            'buy_price': current_price,
                            'highest_price': current_price,
                            'ma20': refreshed_ma20,
                            'ma5': refreshed_ma5,
                            'ohlc': refreshed_ohlc,
                        }
                    else:
                        logger.error(f"[{ticker}] Re-entry Buy Failed: {res}")
                        record_skip_reason(ticker, f"재진입 주문 실패: {res.get('msg1', 'unknown') if res else 'unknown'}")
                        if res and res.get('msg_cd') in ['APBK1680', 'APBK1681']:
                            monitoring_targets[ticker]['status'] = 'failed'
                            FAILED_TICKERS.add(ticker)
                else:
                    logger.info(f"[{ticker}] Re-entry pending (Price={current_price}, MA20={refreshed_ma20}, MA5={refreshed_ma5})")
                continue

            if data['status'] == 'ai_rejected':
                now_dt = datetime.datetime.now()
                last_rejected_at = data.get('last_ai_rejected_at')
                cooldown_sec = data.get('ai_retry_cooldown_sec', AI_RETRY_COOLDOWN_SEC)
                if last_rejected_at and (now_dt - last_rejected_at).total_seconds() < cooldown_sec:
                    continue
                data['status'] = 'monitoring'
                logger.info(f"[{ticker}] AI rejection cooldown elapsed. Retrying buy evaluation.")

            if data['status'] in ['failed', 'sold_sl', 'sold_tp', 'sl_failed', 'tp_failed']:
                continue
                
            target_price = data['target']
            exchange = data.get('exchange')
            
            # --- [Enhanced] Volume & Price Check ---
            # 1. Fetch Price & Volume
            if market == 'US':
                # Use get_quote to get real-time volume (tvol) + price (last)
                quote = kis.get_quote(ticker, exchange)
                current_price = safe_float(quote.get('last')) if quote else 0
                current_vol = safe_float(quote.get('tvol')) if quote else 0
            else:
                current_price = kis.get_current_price(ticker)
                # KR API might need separate call for volume if get_current_price doesn't return it
                # For simplicity/speed in KR, we might skip volume or need to impl get_quote for KR
                current_vol = 0 # KR Volume Check pending implementation
            
            # 2. Check Conditions
            if current_price and current_price >= target_price:
                logger.info(f"[{ticker}] Price Breakout! ({current_price} >= {target_price})")
                
                # Volume Spike Check (Only for US currently as we fetched quote)
                # 레버리지 ETF(TQQQ/SOXL 등)는 구조적으로 거래량이 낮아 임계값 완화 적용
                if market == 'US' and current_vol > 0:
                    ohlc = data.get('ohlc', [])
                    is_us_lev = ticker in US_LEVERAGED_ETF_SYMBOLS
                    vol_threshold = 1.2 if is_us_lev else 1.5
                    is_volume_spike = check_volume_spike(current_vol, ohlc, threshold=vol_threshold)
                    
                    if not is_volume_spike:
                        logger.warning(f"[{ticker}] ⚠️ Volume too low ({current_vol}, threshold={vol_threshold}x). Spike check failed.")
                        record_skip_reason(ticker, f"거래량 부족: {current_vol}")
                        continue
                    else:
                        logger.info(f"[{ticker}] ✅ Volume Spike Confirmed! ({current_vol}, threshold={vol_threshold}x)")

                logger.info("Checking AI Sentiment...")
                news = ai.fetch_news() # TODO: Improve AI news source for KR stocks later
                sentiment = ai.check_market_sentiment(news, persona=PERSONA)
                
                logger.info(f"AI Result: {sentiment}")
                
                if sentiment.get('can_buy', False):
                    # Skip if ticker is in global failed list (account restrictions)
                    if ticker in FAILED_TICKERS:
                        logger.warning(f"[{ticker}] Skipping - previously failed due to account restriction")
                        record_skip_reason(ticker, "계좌 제한 종목")
                        data['status'] = 'failed'
                        continue
                    
                    # Check retry count
                    if retry_counts.get(ticker, 0) >= MAX_RETRIES_PER_TICKER:
                        logger.warning(f"[{ticker}] Max retries ({MAX_RETRIES_PER_TICKER}) reached. Skipping for this session.")
                        record_skip_reason(ticker, f"재시도 초과 ({MAX_RETRIES_PER_TICKER})")
                        data['status'] = 'failed'
                        continue
                    
                    # 매수 신호 강도 계산
                    signal_strength = calculate_signal_strength(
                        current_price, 
                        target_price, 
                        data.get('ma20'),
                        data.get('ohlc')
                    )
                    logger.info(f"[{ticker}] Signal Strength: {signal_strength:.2f}")
                    
                    # 신호 강도에 따른 동적 수량 계산
                    qty = calculate_order_quantity(
                        available_cash, 
                        current_price, 
                        signal_strength,
                        num_active_targets
                    )
                    logger.info(f"[{ticker}] AI Approved. Buying {qty} shares (Signal: {signal_strength:.2f})...")
                    
                    if market == 'US':
                        res = kis.buy_market_order(ticker, qty, exchange)
                    else:
                        res = kis.buy_market_order(ticker, qty)
                        
                    # Result checking
                    is_success = False
                    if res:
                        if res.get('rt_cd') == '0': 
                            is_success = True
                        
                    if is_success:
                        data['status'] = 'bought'
                        data['buys'] = qty
                        data['buy_price'] = current_price # Store Buy Price
                        data['highest_price'] = current_price # Initialize Highest Price
                        logger.info(f"[{ticker}] Buy Success! Qty: {qty} @ {current_price}")
                    else:
                        logger.error(f"[{ticker}] Buy Failed: {res}")
                        record_skip_reason(ticker, f"주문 실패: {res.get('msg1', 'unknown') if res else 'unknown'}")
                        retry_counts[ticker] = retry_counts.get(ticker, 0) + 1
                        
                        # Prevent infinite loop on account errors
                        # APBK1680: ETF Education / APBK1681: Basic Deposit Requirement
                        if res and res.get('msg_cd') in ['APBK1680', 'APBK1681']:
                            err_msg = res.get('msg1')
                            logger.critical(f"[{ticker}] STOPPING: Account Restriction Detected ({res.get('msg_cd')}). Message: {err_msg}")
                            data['status'] = 'failed'
                            FAILED_TICKERS.add(ticker)  # Add to global failed list
                        # Generic backoff for other errors
                        else:
                            time.sleep(5)
                else:
                    logger.info(f"[{ticker}] AI Rejected buying due to risk.")
                    record_skip_reason(ticker, f"AI 거부: {sentiment.get('reason', 'N/A')}")
                    data['status'] = 'ai_rejected'
                    data['last_ai_rejected_at'] = datetime.datetime.now()
                    data['ai_retry_cooldown_sec'] = AI_RETRY_COOLDOWN_SEC

        # DCA 모드는 감시 간격을 넓힘 (API 호출 절약)
        if STRATEGY_MODE == 'dca':
            time.sleep(DCA_MONITOR_INTERVAL)
        else:
            time.sleep(1)  # VBO/Day 모드는 1초 간격
        
    # 3. Market Close Sell-off
    logger.info(f"[{market}] Session End. Checking Exit Rules...")
    
    # 모든 전략에서 종가 일괄청산 비활성화: 추세 보유 우선
    sold_sl = [t for t, d in monitoring_targets.items() if d['status'] == 'sold_sl']
    sold_tp = [t for t, d in monitoring_targets.items() if d['status'] == 'sold_tp']
    holding = [t for t, d in monitoring_targets.items() if d['status'] == 'bought']
    logger.info(f"[{market}] Session summary ({STRATEGY_MODE.upper()}):")
    logger.info(f"   - 손절매: {sold_sl}")
    logger.info(f"   - 트레일링 스탑: {sold_tp}")
    logger.info(f"   - 계속 보유: {holding}")
    if skipped_buy_reasons:
        logger.info(f"   - 매수 보류/차단: {skipped_buy_reasons}")
    if sold_sl or sold_tp or skipped_buy_reasons:
        send_alert(
            f"📊 [{market}] 세션 종료\n"
            f"손절: {sold_sl}\n"
            f"익절: {sold_tp}\n"
            f"보유: {holding}\n"
            f"매수보류: {skipped_buy_reasons}"
        )

def run_with_recovery():
    """Wrapper function to run job with automatic recovery.
    Uses KST timezone for all time comparisons.
    Uses range-based triggers with daily flags to prevent duplicate execution.
    """
    max_consecutive_errors = 5
    error_count = 0
    kr_triggered_today = False
    us_triggered_today = False
    opro_triggered_today = False  # 장 마감 후 백테스트+OPRO 자동 실행
    last_date = None
    
    # --- Startup Check (runs once at boot) ---
    kst = pytz.timezone('Asia/Seoul')
    now_kst = datetime.datetime.now(kst)
    ctx = get_market_status()
    last_date = now_kst.strftime("%Y%m%d")
    
    if ctx != 'CLOSED':
        logger.info(f"⚡ Bot started during {ctx} Trading Hours (KST: {now_kst.strftime('%H:%M:%S')}). Launching job immediately.")
        try:
            job()
            error_count = 0
            # Mark as triggered to prevent duplicate execution
            if ctx == 'KR':
                kr_triggered_today = True
            elif ctx == 'US':
                us_triggered_today = True
        except Exception as e:
            logger.critical(f"Startup Job Crashed: {e}", exc_info=True)
            send_alert(f"Startup Job Crashed: {e}", is_error=True)
    else:
        logger.info(f"😴 Bot started during CLOSED hours (KST: {now_kst.strftime('%H:%M:%S')}). Waiting for market open...")
    
    # --- Main Loop ---
    while True:
        try:
            schedule.run_pending()
            
            kst = pytz.timezone('Asia/Seoul')
            now = datetime.datetime.now(kst)
            t = int(now.strftime("%H%M"))
            today_str = now.strftime("%Y%m%d")
            
            # Reset daily triggers at date change
            if last_date != today_str:
                kr_triggered_today = False
                us_triggered_today = False
                opro_triggered_today = False
                last_date = today_str
                logger.info(f"📅 New day: {today_str} (KST). Daily triggers reset.")
            
            # KR 장 마감 후 16:00 KST — 백테스트 + OPRO 자동 최적화
            if 1600 <= t <= 1605 and not opro_triggered_today:
                opro_triggered_today = True
                logger.info(f"🤖 [OPRO] 장 마감 후 자동 최적화 시작 (KST {now.strftime('%H:%M')})")
                try:
                    from modules.backtest_runner import build_backtest_summary, _build_markdown, REPORT_DIR, LATEST_SUMMARY_FILE
                    import json as _json
                    from datetime import datetime as _dt
                    from pathlib import Path as _Path
                    _summary = build_backtest_summary()
                    REPORT_DIR.mkdir(parents=True, exist_ok=True)
                    _stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
                    with open(REPORT_DIR / f"backtest_{_stamp}.md", "w", encoding="utf-8") as _f:
                        _f.write(_build_markdown(_summary))
                    with open(LATEST_SUMMARY_FILE, "w", encoding="utf-8") as _f:
                        _json.dump(_summary, _f, indent=2, ensure_ascii=False)
                    logger.info(f"[Backtest] 생성 완료: status={_summary.get('status')}")

                    if _summary.get("status") == "ok":
                        from modules.opro_optimizer import run_opro_optimization
                        _opro = run_opro_optimization(notifier=notifier if 'notifier' in dir() else None)
                        if _opro.get("changed"):
                            send_alert(_opro["summary"])
                        else:
                            logger.info(f"[OPRO] 변경 없음: {_opro.get('summary')}")
                    else:
                        logger.info("[OPRO] 백테스트 데이터 부족 — 최적화 건너뜀")
                except Exception as _opro_e:
                    logger.error(f"[OPRO] 자동 최적화 실패: {_opro_e}", exc_info=True)
                    send_alert(f"⚠️ OPRO 자동 최적화 실패: {_opro_e}", is_error=True)

            # KR Market: Trigger between 09:00~09:05 KST (5-minute window)
            if 900 <= t <= 905 and not kr_triggered_today:
                kr_triggered_today = True
                logger.info(f"⏰ KR Market trigger at KST {now.strftime('%Y-%m-%d %H:%M:%S')}")
                try:
                    job()
                    error_count = 0
                except Exception as e:
                    error_count += 1
                    logger.critical(f"KR Job Crashed (Attempt {error_count}/{max_consecutive_errors}): {e}", exc_info=True)
                    send_alert(f"KR Job Crashed: {e}", is_error=True)
                    if error_count >= max_consecutive_errors:
                        logger.critical("Too many consecutive errors. Waiting 5 minutes before retry...")
                        send_alert("Too many consecutive KR errors! Waiting 5 minutes...", is_error=True)
                        time.sleep(300)
                        error_count = 0
                time.sleep(60)
                
            # US Market: Trigger between 23:30~23:35 KST (5-minute window)
            if 2330 <= t <= 2335 and not us_triggered_today:
                us_triggered_today = True
                logger.info(f"⏰ US Market trigger at KST {now.strftime('%Y-%m-%d %H:%M:%S')}")
                try:
                    job()
                    error_count = 0
                except Exception as e:
                    error_count += 1
                    logger.critical(f"US Job Crashed (Attempt {error_count}/{max_consecutive_errors}): {e}", exc_info=True)
                    send_alert(f"US Job Crashed: {e}", is_error=True)
                    if error_count >= max_consecutive_errors:
                        logger.critical("Too many consecutive errors. Waiting 5 minutes before retry...")
                        send_alert("Too many consecutive US errors! Waiting 5 minutes...", is_error=True)
                        time.sleep(300)
                        error_count = 0
                time.sleep(60)
                
            time.sleep(1)
            
        except KeyboardInterrupt:
            logger.info("Received shutdown signal. Exiting gracefully...")
            send_alert("Bot received shutdown signal. Exiting...")
            break
        except Exception as e:
            logger.critical(f"Unexpected error in main loop: {e}", exc_info=True)
            send_alert(f"Unexpected error in main loop: {e}", is_error=True)
            time.sleep(10)

if __name__ == "__main__":
    logger.info("=== Alpha Trader Bot Started ===")
    logger.info(f"US Targets: {TARGET_TICKERS_US}")
    logger.info(f"KR Targets: {TARGET_TICKERS_KR}")
    logger.info(f"Safe Mode: {IS_SAFE_MODE}, Strategy: {STRATEGY_MODE}, Auto: {user_config.get('auto_strategy', False)}")
    
    # Startup notification
    auto_label = "🤖 Auto" if user_config.get('auto_strategy', False) else "Manual"
    send_alert(f"🚀 Bot Started!\nMode: {'Safe' if IS_SAFE_MODE else 'Leverage'}\nStrategy: {STRATEGY_MODE.upper()}\n전략모드: {auto_label}")
    
    # Heartbeat & Data Collection
    def heartbeat():
        status = get_market_status()
        logger.info(f"Heartbeat: Bot is alive... Market Status: {status}")
        
        # Collect account data for dashboard every minute
        try:
            update_all_accounts()
        except Exception as e:
            logger.error(f"Background account update failed: {e}")
            
    def run_scanner():
        """Run Market Scanner for KR Stocks"""
        status = get_market_status()
        if status == 'KR': # Only scan during KR market hours
            try:
                global DYNAMIC_TARGETS
                logger.info("📡 Scanning for KR opportunities...")
                
                # Scan for volume spikes (300%+) and top gainers (10%+)
                spikes = scanner.scan_volume_spikes(min_volume_increase_rate=300)
                
                # New: Scan for Blue Chip Surges (High Trading Value)
                blue_chips = scanner.scan_blue_chip_surge(min_gain=2.0, max_rank=50)

                # found_tickers = spikes + gainers
                found_tickers = spikes + blue_chips # Combine both strategies
                
                current_dynamic_codes = [t['symbol'] for t in DYNAMIC_TARGETS]
                
                new_discoveries = []
                for item in found_tickers:
                    code = item['code']
                    if code not in current_dynamic_codes and code not in TARGET_TICKERS_KR:
                        # Add to dynamic targets
                        DYNAMIC_TARGETS.append({'symbol': code, 'exchange': 'KR', 'reason': item['reason']})
                        new_discoveries.append(f"{item['name']}({item['reason']})")
                
                if new_discoveries:
                    msg = f"🚀 [Scanner] Found {len(new_discoveries)} new opportunities:\n" + "\n".join(new_discoveries)
                    send_alert(msg)
                    logger.info(f"Updated Dynamic Targets: {[t['symbol'] for t in DYNAMIC_TARGETS]}")
                    
            except Exception as e:
                logger.error(f"Scanner failed: {e}")
        
    def update_dynamic_portfolio():
        logger.info("🔄 Running Scheduled Dynamic Portfolio Update...")
        try:
            from modules.portfolio_manager import PortfolioManager
            pm = PortfolioManager()
            pm.generate_and_save_portfolio(max_kr=10)
        except Exception as e:
            logger.error(f"Failed to update dynamic portfolio: {e}")

    schedule.every(1).minutes.do(heartbeat)
    schedule.every(5).minutes.do(run_scanner) # Scan every 5 minutes
    schedule.every().day.at("08:00").do(update_dynamic_portfolio) # Update before KR market opens
    # For US market testing and stability
    schedule.every().day.at("23:10").do(update_dynamic_portfolio)
    
    # Run main loop with automatic recovery (includes startup check)
    run_with_recovery()
