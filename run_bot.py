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
    check_gap_down, check_consecutive_decline, check_portfolio_drawdown
)
from strategies.volatility_breakout import calculate_target_price
from modules.account_manager import update_all_accounts
from modules.market_scanner import scanner  # New scanner module
from modules.multi_llm import MultiLLMAnalyst
from modules.auto_strategy import AutoStrategyOptimizer

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
        save_config(config)
        return True
    return False

# Load initial config
user_config = load_config()
IS_SAFE_MODE = True if user_config.get("trading_mode") == "safe" else False
STRATEGY_MODE = user_config.get("strategy", "day")  # 'day', 'swing', or 'dca'
PERSONA = user_config.get("persona", "aggressive") # 'aggressive', 'neutral', 'conservative'

DCA_SETTINGS = user_config.get("dca_settings", {
    "enabled": True,
    "daily_investment_pct": 5,
    "buy_delay_minutes": 30,
    "min_investment_usd": 10,
    "max_investment_usd": 100
})

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

logger.info(f"Loaded Config: Mode={user_config.get('trading_mode')}, Strategy={STRATEGY_MODE}, Persona={PERSONA}")

# 1. Leveraged Targets (Requires >10M KRW Deposit & Education)
TARGET_TICKERS_US_3X = [
    {'symbol': "NVDL", 'exchange': "NAS"},  # GraniteShares 2x Long NVDA
    {'symbol': "SOXL", 'exchange': "AMS"},  # Direxion Daily Semiconductor Bull 3X
    {'symbol': "TQQQ", 'exchange': "NAS"},  # ProShares UltraPro QQQ
    {'symbol': "TECL", 'exchange': "AMS"},  # Direxion Daily Technology Bull 3X
    {'symbol': "FNGU", 'exchange': "AMS"},  # MicroSectors FANG+ Index 3X
    {'symbol': "BITX", 'exchange': "AMS"},  # 2x Bitcoin Strategy ETF
    {'symbol': "CONL", 'exchange': "NAS"},  # 2x Coinbase
    {'symbol': "TSLA", 'exchange': "NAS"},  # Tesla (High Volatility Stock)
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
    "000660", # SK Hynix (SK하이닉스)
    "005930", # Samsung Electronics (삼성전자)
    "012450", # Hanwha Aerospace (한화에어로스페이스)
    "005380", # Hyundai Motor (현대차)
    "035420", # Naver (네이버)
]

# Select Tickers based on Mode
# US 시장은 3배 ETF 제한 없음 - 항상 3X + 1X 모두 사용
TARGET_TICKERS_US = TARGET_TICKERS_US_3X + [t for t in TARGET_TICKERS_US_1X if t not in TARGET_TICKERS_US_3X]
# KR 시장만 예탁금 기준으로 safe/risky 분리
TARGET_TICKERS_KR = TARGET_TICKERS_KR_1X if IS_SAFE_MODE else TARGET_TICKERS_KR_2X

# === KR 시장별 차별화된 리스크 관리 상수 ===
KR_STOCK_STOP_LOSS_PCT = -5.0       # KR 개별주 손절 (ETF 대비 완화)
KR_STOCK_TRAILING_ACTIVATION = 5.0  # KR 개별주 트레일링 활성화
KR_STOCK_TRAILING_DROP = 2.5        # KR 개별주 고점 대비 하락
KR_STOCK_GAP_DOWN_THRESHOLD = 4.0   # KR 개별주 갭다운 기준

# KR ETF 종목 코드 (ETF인지 개별주인지 구분용)
KR_ETF_CODES = {'122630', '233740', '449200', '069500', '229200', '114800'}

# US 레버리지 ETF 심볼 목록 (3X ETF는 DCA 매수 조건 완화 적용)
US_LEVERAGED_ETF_SYMBOLS = {t['symbol'] for t in TARGET_TICKERS_US_3X}

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
    
    # 신호 강도에 따른 투자 비율 결정
    if signal_strength >= 0.8:
        position_pct = 0.30  # 강한 신호: 30%
        logger.info(f"📈 강한 매수 신호! (강도: {signal_strength:.2f}) → 포지션 30%")
    elif signal_strength >= 0.5:
        position_pct = 0.20  # 보통 신호: 20%
        logger.info(f"📊 보통 매수 신호 (강도: {signal_strength:.2f}) → 포지션 20%")
    elif signal_strength >= 0.3:
        position_pct = 0.10  # 약한 신호: 10%
        logger.info(f"📉 약한 매수 신호 (강도: {signal_strength:.2f}) → 포지션 10%")
    else:
        position_pct = 0.05  # 매우 약한 신호: 5% (최소)
        logger.info(f"⚠️ 매우 약한 신호 (강도: {signal_strength:.2f}) → 최소 포지션 5%")
    
    max_investment = per_ticker_cash * position_pct
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
    investment_amount = per_ticker_cash * daily_pct
    
    # 최소/최대 제한 적용
    investment_amount = max(min_investment, min(max_investment, investment_amount))
    
    qty = int(investment_amount / current_price)
    
    logger.info(f"📈 DCA 매수: {currency_symbol}{investment_amount:,.0f} → {qty}주 (가격: {currency_symbol}{current_price:,.0f})")
    
    return max(qty, 1)

K_VALUE = 0.5

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

def job():
    # Reload config dynamically
    global IS_SAFE_MODE, STRATEGY_MODE, PERSONA, TARGET_TICKERS_US, TARGET_TICKERS_KR
    user_config = load_config()
    IS_SAFE_MODE = True if user_config.get("trading_mode") == "safe" else False
    STRATEGY_MODE = user_config.get("strategy", "day")
    PERSONA = user_config.get("persona", "aggressive")
    
    # Update Target Tickers based on new config
    # US 시장은 항상 3X + 1X 모두 사용 (레버리지 제한 없음)
    TARGET_TICKERS_US = TARGET_TICKERS_US_3X + [t for t in TARGET_TICKERS_US_1X if t not in TARGET_TICKERS_US_3X]
    # KR만 예탁금 기준 safe/risky 분리
    TARGET_TICKERS_KR = TARGET_TICKERS_KR_1X if IS_SAFE_MODE else TARGET_TICKERS_KR_2X

    market = get_market_status()
    
    if market == 'CLOSED':
        logger.info("Market is closed. Sleeping.")
        return

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
            
    ai = MultiLLMAnalyst()
    
    # Dictionary to store monitoring targets
    monitoring_targets = {}
    
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
                if ticker_id:
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
                holding_qty = 1
                holding_avg_price = 0
                try:
                    for h in balance.get('output1', []):
                        if h['pdno'] == ticker or h.get('ovrs_pdno') == ticker:
                            holding_qty = int(safe_float(h.get('hldg_qty', h.get('ovrs_cblc_qty', h.get('ord_psbl_qty', 1)))))
                            holding_avg_price = safe_float(h.get('pchs_avg_pric', h.get('avg_unpr3', 0)))
                            break
                except:
                    pass
                
                if holding_avg_price > 0 and current_price > 0:
                    pnl_pct = ((current_price - holding_avg_price) / holding_avg_price) * 100
                    logger.info(f"[{ticker}] 📊 보유현황: {holding_qty}주, 평단가: {holding_avg_price:.2f}, 현재가: {current_price:.2f}, 손익: {pnl_pct:.2f}%")
                    
                    # Stop Loss - 시장/종목별 차별화 (KR 개별주는 완화)
                    if pnl_pct <= eff_stop_loss:
                        logger.warning(f"[{ticker}] 🛑 DCA STOP LOSS! ({pnl_pct:.2f}% <= {eff_stop_loss}%). 즉시 매도 {holding_qty}주.")
                        send_alert(f"🛑 [{ticker}] DCA 손절매 발동! {pnl_pct:.2f}%. {holding_qty}주 매도.")
                        if market == 'US':
                            kis.sell_market_order(ticker, holding_qty, exchange)
                        else:
                            kis.sell_market_order(ticker, holding_qty)
                        monitoring_targets[ticker] = {
                            'target': current_price, 'status': 'sold_sl',
                            'buys': 0, 'exchange': exchange
                        }
                        continue  # 손절 후 추가 매수 안 함
                else:
                    # 평단가 정보 없으면 감시 대상에 추가 (감시 루프에서 처리)
                    monitoring_targets[ticker] = {
                        'target': current_price, 'status': 'bought',
                        'buys': holding_qty, 'exchange': exchange,
                        'buy_price': holding_avg_price if holding_avg_price > 0 else current_price,
                        'highest_price': current_price
                    }
            
            # === [NEW] Step 1: 매수 차단 조건 (Circuit Breaker) ===
            buy_blocked = False
            block_reasons = []
            
            # US 레버리지 ETF 여부 확인 (3X ETF는 매수 조건 완화)
            is_us_leveraged_etf = (market == 'US' and ticker in US_LEVERAGED_ETF_SYMBOLS)
            
            # 1-a. 갭다운 시 매수 차단 (US 레버리지 ETF는 임계치 2배로 완화)
            effective_gap_threshold = eff_gap_down_threshold * 2 if is_us_leveraged_etf else eff_gap_down_threshold
            if is_gap_down and gap_drop_pct >= effective_gap_threshold:
                buy_blocked = True
                block_reasons.append(f"갭다운 {gap_drop_pct:.1f}%")
            elif is_gap_down and is_us_leveraged_etf:
                block_reasons.append(f"갭다운 {gap_drop_pct:.1f}% (레버리지 ETF 완화 적용)")
            
            # 1-b. 연속 하락 시 매수 차단 (US 레버리지 ETF는 조건 완화: 3일 연속 또는 5% 이상만 차단)
            if is_consecutive_decline:
                if is_us_leveraged_etf:
                    # US 레버리지 ETF: 3일 연속 또는 누적 5% 이상만 차단
                    if CONSECUTIVE_DECLINE_DAYS >= 3 or cum_drop_pct >= 5.0:
                        buy_blocked = True
                        block_reasons.append(f"연속 {CONSECUTIVE_DECLINE_DAYS}일 하락 {cum_drop_pct:.1f}%")
                    else:
                        block_reasons.append(f"연속 하락 {cum_drop_pct:.1f}% (레버리지 ETF 완화 적용)")
                else:
                    buy_blocked = True
                    block_reasons.append(f"연속 {CONSECUTIVE_DECLINE_DAYS}일 하락 {cum_drop_pct:.1f}%")
            
            # 1-c. 20MA 하회 시 매수 차단 (US 레버리지 ETF는 10MA로 완화)
            if is_us_leveraged_etf:
                # US 레버리지 ETF: 10MA 기준 사용 (더 단기 추세)
                ma10 = calculate_ma(closes, 10) if len(closes) >= 10 else ma20
                is_uptrend_for_buy = check_trend(current_price, ma10) if ma10 else is_uptrend
                if not is_uptrend_for_buy:
                    buy_blocked = True
                    block_reasons.append(f"10MA 하회 (레버리지 ETF)")
            else:
                if not is_uptrend:
                    buy_blocked = True
                    block_reasons.append(f"20MA 하회")
            
            # 1-d. 5MA 하회 시 매수량 축소 (완전 차단은 아닌 경고)
            dca_reduce_qty = False
            if not is_short_uptrend and is_uptrend:
                dca_reduce_qty = True
                block_reasons.append(f"5MA 하회 (매수량 50% 축소)")
            
            if buy_blocked:
                logger.info(f"[{ticker}] DCA 매수 차단 - {', '.join(block_reasons)}")
                # 보유분은 감시 대상에 등록 (이미 위에서 처리)
                continue
            
            if block_reasons:
                logger.info(f"[{ticker}] DCA 경고: {', '.join(block_reasons)}")

            # AI 체크 (DCA도 극단적 하락장은 피함)
            news = ai.fetch_news()
            sentiment = ai.check_market_sentiment(news, persona=PERSONA)
            
            if sentiment.get('market_condition') == 'CRASH' or sentiment.get('risk_level') == 'HIGH':
                logger.warning(f"[{ticker}] DCA Paused - AI Risk HIGH: {sentiment.get('reason', 'N/A')}")
                send_alert(f"⚠️ [{ticker}] DCA 중단 - AI 위험 감지: {sentiment.get('reason', 'N/A')}")
                continue
            
            # DCA 수량 계산
            qty = calculate_dca_quantity(available_cash, current_price, num_active_targets, DCA_SETTINGS, market)
            
            # 5MA 하회 시 매수량 50% 축소
            if dca_reduce_qty:
                qty = max(1, qty // 2)
                logger.info(f"[{ticker}] 📉 5MA 하회로 매수량 축소: {qty}주")
            
            if qty > 0:
                currency = "$" if market == 'US' else "₩"
                if market == 'US':
                     logger.info(f"[{ticker}] DCA Buy: {qty} shares at ${current_price:.2f}")
                else:
                     logger.info(f"[{ticker}] DCA Buy: {qty} shares at {currency}{current_price:,.0f}")
                
                if market == 'US':
                    res = kis.buy_market_order(ticker, qty, exchange)
                else:
                    res = kis.buy_market_order(ticker, qty)
                
                if res and res.get('rt_cd') == '0':
                    logger.info(f"[{ticker}] ✅ DCA Buy Success! {qty} shares")
                    
                    # === [NEW] 감시 대상에 등록 (buy_price, highest_price 포함) ===
                    existing = monitoring_targets.get(ticker, {})
                    total_qty = existing.get('buys', 0) + qty
                    # 기존 매수가와 블렌딩
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
                        'highest_price': max(current_price, existing.get('highest_price', current_price))
                    }
                else:
                    logger.error(f"[{ticker}] DCA Buy Failed: {res}")
            continue  # DCA는 바로 다음 종목으로
        
        # Case 1: Already Holding
        if ticker in current_holdings:
            # 보유 수량 조회
            holding_qty = 1
            try:
                for h in balance.get('output1', []):
                    if h['pdno'] == ticker:
                        holding_qty = int(h.get('hldg_qty', h.get('ccld_qty_smtl1', 1)))
                        break
            except:
                pass
            
            if not is_uptrend:
                logger.info(f"[{ticker}] Trend Broken (Price < 20MA). Selling {holding_qty} shares immediately.")
                if market == 'US':
                    kis.sell_market_order(ticker, holding_qty, exchange)
                else:
                    kis.sell_market_order(ticker, holding_qty)
                # Don't add to monitoring list
                continue
            else:
                logger.info(f"[{ticker}] Trend OK. Holding {holding_qty} shares.")
                # Mark as bought to prevent duplicate buy
                monitoring_targets[ticker] = {
                    'target': 9999999, # Dummy target
                    'status': 'bought',
                    'buys': holding_qty,
                    'exchange': exchange
                }
                continue

        # Case 2: New Entry (Trend Analysis)
        if not is_uptrend:
            logger.info(f"[{ticker}] Bear Market (Price < 20MA). Skipping.")
            continue

         # B. Calculate Target Price (Volatility Breakout)
        today_open = safe_float(ohlc[0]['open']) 
        target_price = calculate_target_price(today_open, ohlc, K_VALUE)
        logger.info(f"[{ticker}] Bull Market! Target Price: {target_price} (Open: {today_open})")
        
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
        if bought_positions:
            logger.info(f"[{market}] DCA Mode - Entering monitoring loop for {len(bought_positions)} positions: {list(bought_positions.keys())}")
        else:
            logger.info(f"[{market}] DCA Mode - No positions to monitor. Session complete.")
            return

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
                    
                    # 1. Stop Loss - ABSOLUTE RULE (시장별 차별화)
                    if pnl_pct <= pos_stop_loss:
                        logger.warning(f"[{ticker}] 🛑 Stop Loss Triggered! ({pnl_pct:.2f}% <= {pos_stop_loss}%). Selling {qty} shares.")
                        send_alert(f"🛑 [{ticker}] 손절매 발동! {pnl_pct:.2f}%. {qty}주 매도.")
                        if market == 'US':
                            kis.sell_market_order(ticker, qty, data.get('exchange'))
                        else:
                            kis.sell_market_order(ticker, qty)
                        data['status'] = 'sold_sl' # Mark as Sold (Stop Loss)
                        continue
                        
                    # 2. Trailing Stop (시장별 차별화)
                    peak_pnl_pct = ((highest_price - buy_price) / buy_price) * 100
                    
                    if peak_pnl_pct >= pos_trailing_act:
                        drop_from_peak = ((highest_price - curr) / highest_price) * 100
                        if drop_from_peak >= pos_trailing_drop:
                            logger.info(f"[{ticker}] 💰 Trailing Stop Triggered! (Peak: {peak_pnl_pct:.2f}%, Drop: {drop_from_peak:.2f}% >= {pos_trailing_drop}%). Selling {qty} shares.")
                            send_alert(f"💰 [{ticker}] 트레일링 스탑! 고점 대비 {drop_from_peak:.2f}% 하락. {qty}주 매도.")
                            if market == 'US':
                                kis.sell_market_order(ticker, qty, data.get('exchange'))
                            else:
                                kis.sell_market_order(ticker, qty)
                            data['status'] = 'sold_tp' # Mark as Sold (Take Profit)
                            continue
                            
                except Exception as e:
                    logger.error(f"[{ticker}] Error in Risk Management: {e}")
                
                continue # Skip breakout check if already bought

            if data['status'] in ['failed', 'ai_rejected', 'sold_sl', 'sold_tp']:
                continue
                
            target_price = data['target']
            
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
                if market == 'US' and current_vol > 0:
                    ohlc = data.get('ohlc', [])
                    is_volume_spike = check_volume_spike(current_vol, ohlc)
                    
                    if not is_volume_spike:
                        # Optional: Log warning but maybe don't block fully if user wants aggressive
                        # BUT user asked for filters. Let's make it a Soft Warning or Strong Filter?
                        # User asked to "apply new logic". Let's apply it.
                        logger.warning(f"[{ticker}] ⚠️ Volume too low ({current_vol}). Spike check failed. Waiting for volume support.")
                        continue
                    else:
                        logger.info(f"[{ticker}] ✅ Volume Spike Confirmed! ({current_vol})")

                logger.info("Checking AI Sentiment...")
                news = ai.fetch_news() # TODO: Improve AI news source for KR stocks later
                sentiment = ai.check_market_sentiment(news, persona=PERSONA)
                
                logger.info(f"AI Result: {sentiment}")
                
                if sentiment.get('can_buy', False):
                    # Skip if ticker is in global failed list (account restrictions)
                    if ticker in FAILED_TICKERS:
                        logger.warning(f"[{ticker}] Skipping - previously failed due to account restriction")
                        data['status'] = 'failed'
                        continue
                    
                    # Check retry count
                    if retry_counts.get(ticker, 0) >= MAX_RETRIES_PER_TICKER:
                        logger.warning(f"[{ticker}] Max retries ({MAX_RETRIES_PER_TICKER}) reached. Skipping for this session.")
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
                    # Don't retry AI rejection immediately
                    data['status'] = 'ai_rejected'

        # DCA 모드는 감시 간격을 넓힘 (API 호출 절약)
        if STRATEGY_MODE == 'dca':
            time.sleep(DCA_MONITOR_INTERVAL)
        else:
            time.sleep(1)  # VBO/Day 모드는 1초 간격
        
    # 3. Market Close Sell-off
    logger.info(f"[{market}] Session End. Checking Exit Rules...")
    
    if STRATEGY_MODE in ('swing', 'dca'):
        # DCA/Swing: 종가 청산하지 않음 (장기 보유 전략)
        # 단, 손절매가 발동된 종목은 이미 매도됨
        sold_sl = [t for t, d in monitoring_targets.items() if d['status'] == 'sold_sl']
        sold_tp = [t for t, d in monitoring_targets.items() if d['status'] == 'sold_tp']
        holding = [t for t, d in monitoring_targets.items() if d['status'] == 'bought']
        logger.info(f"[{market}] Strategy is {STRATEGY_MODE.upper()}. Session summary:")
        logger.info(f"   - 손절매: {sold_sl}")
        logger.info(f"   - 트레일링 스탑: {sold_tp}")
        logger.info(f"   - 계속 보유: {holding}")
        if sold_sl or sold_tp:
            send_alert(f"📊 [{market}] 세션 종료\n손절: {sold_sl}\n익절: {sold_tp}\n보유: {holding}")
    else:
        logger.info(f"[{market}] Strategy is DAY. Selling All Holdings.")
        for ticker, data in monitoring_targets.items():
            if data['status'] == 'bought':
                qty = data['buys']
                logger.info(f"[{ticker}] Selling {qty} shares...")
                if market == 'US':
                    kis.sell_market_order(ticker, qty, data.get('exchange'))
                else:
                    kis.sell_market_order(ticker, qty)

def run_with_recovery():
    """Wrapper function to run job with automatic recovery.
    Uses KST timezone for all time comparisons.
    Uses range-based triggers with daily flags to prevent duplicate execution.
    """
    max_consecutive_errors = 5
    error_count = 0
    kr_triggered_today = False
    us_triggered_today = False
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
                last_date = today_str
                logger.info(f"📅 New day: {today_str} (KST). Daily triggers reset.")
            
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
    logger.info("=== Global ETF Sniper Bot Started ===")
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
        
    schedule.every(1).minutes.do(heartbeat)
    schedule.every(5).minutes.do(run_scanner) # Scan every 5 minutes
    
    # Run main loop with automatic recovery (includes startup check)
    run_with_recovery()
