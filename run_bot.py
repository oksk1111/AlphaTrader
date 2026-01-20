import time
import datetime
import schedule
import sys
import json
import os
import traceback
from modules.kis_api import KisOverseas
from modules.kis_domestic import KisDomestic
from modules.gemini_analyst import GeminiAnalyst
from modules.logger import logger
from modules.telegram_notifier import TelegramNotifier
from strategies.technical import calculate_ma, check_trend
from strategies.volatility_breakout import calculate_target_price

# Configuration
CONFIG_FILE = "user_config.json"
MAX_RETRIES_PER_TICKER = 3  # Maximum buy retries per ticker per session
FAILED_TICKERS = set()  # Track permanently failed tickers (account restrictions)
LEVERAGE_THRESHOLD_KRW = 10_000_000  # 1000만원 기준

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
    return {"trading_mode": "safe", "strategy": "day"}

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

def check_and_upgrade_mode(total_asset_krw):
    """
    예치금이 1000만원 이상이면 자동으로 레버리지 모드로 전환
    """
    config = load_config()
    current_mode = config.get("trading_mode", "safe")
    
    if total_asset_krw >= LEVERAGE_THRESHOLD_KRW and current_mode == "safe":
        logger.info(f"🎉 축하합니다! 총 자산이 {total_asset_krw:,.0f}원으로 1000만원을 달성했습니다!")
        logger.info("🚀 자동으로 레버리지 모드(risky)로 전환합니다.")
        config["trading_mode"] = "risky"
        save_config(config)
        return True
    return False

# Load initial config
user_config = load_config()
IS_SAFE_MODE = True if user_config.get("trading_mode") == "safe" else False
STRATEGY_MODE = user_config.get("strategy", "day")  # 'day', 'swing', or 'dca'
DCA_SETTINGS = user_config.get("dca_settings", {
    "enabled": True,
    "daily_investment_pct": 5,
    "buy_delay_minutes": 30,
    "min_investment_usd": 10,
    "max_investment_usd": 100
})

logger.info(f"Loaded Config: Mode={user_config.get('trading_mode')}, Strategy={STRATEGY_MODE}")

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

# 2. Non-Leveraged (1x) Targets (No Restrictions)
TARGET_TICKERS_US_1X = [
    {'symbol': "NVDA", 'exchange': "NAS"},  # NVIDIA (Stock)
    {'symbol': "SOXX", 'exchange': "NAS"},  # iShares Semiconductor ETF
    {'symbol': "QQQ",  'exchange': "NAS"},  # Invesco QQQ Trust
    {'symbol': "XLK",  'exchange': "AMS"},  # Technology Select Sector SPDR
    {'symbol': "MAGS", 'exchange': "AMS"},  # Roundhill Magnificent Seven ETF (Big Tech)
    {'symbol': "IBIT", 'exchange': "NAS"},  # iShares Bitcoin Trust (Spot Bitcoin ETF)
    {'symbol': "COIN", 'exchange': "NAS"},  # Coinbase (Stock)
    {'symbol': "TSLA", 'exchange': "NAS"},  # Tesla (Stock)
]

TARGET_TICKERS_KR_1X = [
    "069500", # KODEX 200 (KOSPI 200 1x)
    "229200", # KODEX KOSDAQ150
    "449200", # KODEX US Tech Top10
]

# Select Tickers based on Mode
TARGET_TICKERS_US = TARGET_TICKERS_US_1X if IS_SAFE_MODE else TARGET_TICKERS_US_3X
TARGET_TICKERS_KR = TARGET_TICKERS_KR_1X if IS_SAFE_MODE else TARGET_TICKERS_KR_2X

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
                high = float(ohlc_data[i]['high'])
                low = float(ohlc_data[i]['low'])
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

def calculate_dca_quantity(available_cash, current_price, num_targets=1, dca_settings=None):
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
    
    # 종목별 투자 금액 계산
    per_ticker_cash = available_cash / max(num_targets, 1)
    investment_amount = per_ticker_cash * daily_pct
    
    # 최소/최대 제한 적용
    investment_amount = max(min_investment, min(max_investment, investment_amount))
    
    qty = int(investment_amount / current_price)
    
    logger.info(f"📈 DCA 매수: ${investment_amount:.2f} → {qty}주 (가격: ${current_price:.2f})")
    
    return max(qty, 1)

K_VALUE = 0.5

def get_market_status():
    """
    Returns 'US', 'KR', or 'CLOSED' based on current KST time.
    """
    now = datetime.datetime.now()
    t = int(now.strftime("%H%M"))
    
    # US Market: 23:30 ~ 06:00
    if 2330 <= t <= 2400 or 0 <= t < 600:
        return 'US'
    
    # KR Market: 09:00 ~ 15:20 (Leave 10 mins for closing auction safely)
    if 900 <= t <= 1520:
        return 'KR'
        
    return 'CLOSED'

def job():
    # Reload config dynamically
    global IS_SAFE_MODE, STRATEGY_MODE, TARGET_TICKERS_US, TARGET_TICKERS_KR
    user_config = load_config()
    IS_SAFE_MODE = True if user_config.get("trading_mode") == "safe" else False
    STRATEGY_MODE = user_config.get("strategy", "day")
    
    # Update Target Tickers based on new config
    TARGET_TICKERS_US = TARGET_TICKERS_US_1X if IS_SAFE_MODE else TARGET_TICKERS_US_3X
    TARGET_TICKERS_KR = TARGET_TICKERS_KR_1X if IS_SAFE_MODE else TARGET_TICKERS_KR_2X

    market = get_market_status()
    
    if market == 'CLOSED':
        logger.info("Market is closed. Sleeping.")
        return

    # Select Market Context
    if market == 'US':
        logger.info(f"🇺🇸 Starting US Trading Session ({STRATEGY_MODE.upper()}) for {TARGET_TICKERS_US}")
        kis = KisOverseas()
        tickers = TARGET_TICKERS_US
    else:
        logger.info(f"🇰🇷 Starting KR Trading Session ({STRATEGY_MODE.upper()}) for {TARGET_TICKERS_KR}")
        kis = KisDomestic()
        tickers = TARGET_TICKERS_KR
    
    ai = GeminiAnalyst()
    
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
                available_cash = float(foreign_bal['deposit'])
            # US 계좌는 환율 적용하여 원화 환산 (대략 1350원)
            total_asset_krw = available_cash * 1350
        else:
            if balance and 'output2' in balance and balance['output2']:
                if isinstance(balance['output2'], list) and len(balance['output2']) > 0:
                    available_cash = float(balance['output2'][0].get('dnca_tot_amt', 0))
                    total_asset_krw = float(balance['output2'][0].get('tot_evlu_amt', available_cash))
        logger.info(f"Available Cash: {available_cash:,.2f}, Total Asset (KRW): {total_asset_krw:,.0f}")
        
        # 자동 모드 전환 체크 (1000만원 달성 시 레버리지 모드로)
        if check_and_upgrade_mode(total_asset_krw):
            # 모드가 변경되었으면 설정 다시 로드
            user_config = load_config()
            IS_SAFE_MODE = user_config.get("trading_mode") == "safe"
            TARGET_TICKERS_US = TARGET_TICKERS_US_1X if IS_SAFE_MODE else TARGET_TICKERS_US_3X
            TARGET_TICKERS_KR = TARGET_TICKERS_KR_1X if IS_SAFE_MODE else TARGET_TICKERS_KR_2X
            tickers = TARGET_TICKERS_US if market == 'US' else TARGET_TICKERS_KR
            logger.info(f"🔄 모드 전환 완료! 새로운 타겟: {tickers}")
            
    except Exception as e:
        logger.error(f"Failed to fetch balance: {e}")

    # --- 0. Check Holding Status (Swing Strategy) ---
    current_holdings = []
    try:
        if balance and 'output1' in balance:
            current_holdings = [h['pdno'] for h in balance['output1']]
    except Exception as e:
        logger.error(f"Failed to parse holdings: {e}")

    logger.info(f"Current Holdings: {current_holdings}")
    
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

        closes = [float(x['clos']) for x in ohlc]
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
        
        # --- STRATEGY BRANCHING ---
        is_uptrend = check_trend(current_price, ma20)
        
        # DCA 전략: 추세와 관계없이 매일 일정 금액 매수
        if STRATEGY_MODE == 'dca':
            # AI 체크 (DCA도 극단적 하락장은 피함)
            news = ai.fetch_news()
            sentiment = ai.check_market_sentiment(news)
            
            if sentiment.get('market_condition') == 'CRASH':
                logger.warning(f"[{ticker}] DCA Paused - Market Crash Detected")
                continue
            
            # DCA 수량 계산
            qty = calculate_dca_quantity(available_cash, current_price, num_active_targets, DCA_SETTINGS)
            
            if qty > 0:
                logger.info(f"[{ticker}] DCA Buy: {qty} shares at ${current_price:.2f}")
                
                if market == 'US':
                    res = kis.buy_market_order(ticker, qty, exchange)
                else:
                    res = kis.buy_market_order(ticker, qty)
                
                if res and res.get('rt_cd') == '0':
                    logger.info(f"[{ticker}] ✅ DCA Buy Success! {qty} shares")
                    monitoring_targets[ticker] = {
                        'target': current_price,
                        'status': 'bought',
                        'buys': qty,
                        'exchange': exchange
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
        today_open = float(ohlc[0]['open']) 
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

    # DCA 모드는 Watch Loop 없이 바로 종료 (이미 매수 완료)
    if STRATEGY_MODE == 'dca':
        logger.info(f"[{market}] DCA Mode - Daily purchases complete. Skipping watch loop.")
        return

    # 활성 모니터링 대상 수 업데이트
    num_active_targets = len([t for t in monitoring_targets.values() if t['status'] == 'monitoring'])
    logger.info(f"[{market}] Watch List: {list(monitoring_targets.keys())} ({num_active_targets} active)")
    
    # 2. Watch Loop
    while True:
        # Check if market closed
        current_market = get_market_status()
        if current_market != market:
            logger.info(f"[{market}] Market Closed. Ending Session.")
            break
            
        for ticker, data in monitoring_targets.items():
            if data['status'] in ['bought', 'failed', 'ai_rejected']:
                continue
                
            exchange = data.get('exchange')
            
            if market == 'US':
                current_price = kis.get_current_price(ticker, exchange)
            else:
                current_price = kis.get_current_price(ticker)
                
            target_price = data['target']
            
            if current_price and current_price >= target_price:
                logger.info(f"[{ticker}] Breakout Detected! ({current_price} >= {target_price})")
                
                logger.info("Checking AI Sentiment...")
                news = ai.fetch_news() # TODO: Improve AI news source for KR stocks later
                sentiment = ai.check_market_sentiment(news)
                
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
                        logger.info(f"[{ticker}] Buy Success! Qty: {qty}")
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

        time.sleep(1)  # Reduced polling frequency to avoid API overload
        
    # 3. Market Close Sell-off
    logger.info(f"[{market}] Session End. Checking Exit Rules...")
    
    if STRATEGY_MODE == 'swing':
        logger.info(f"[{market}] Strategy is SWING. Skipping daily sell-off relative to Trend 20MA Check.")
        # Actual selling happens at the START of next session if Trend Broken.
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
    """Wrapper function to run job with automatic recovery"""
    max_consecutive_errors = 5
    error_count = 0
    
    while True:
        try:
            schedule.run_pending()
            
            now = datetime.datetime.now()
            t = int(now.strftime("%H%M"))
            
            # Trigger at 09:00 for KR
            if t == 900:
                try:
                    job()
                    error_count = 0  # Reset on success
                except Exception as e:
                    error_count += 1
                    logger.critical(f"KR Job Crashed (Attempt {error_count}/{max_consecutive_errors}): {e}", exc_info=True)
                    if error_count >= max_consecutive_errors:
                        logger.critical("Too many consecutive errors. Waiting 5 minutes before retry...")
                        time.sleep(300)
                        error_count = 0
                time.sleep(60)
                
            # Trigger at 23:30 for US
            if t == 2330:
                try:
                    job()
                    error_count = 0
                except Exception as e:
                    error_count += 1
                    logger.critical(f"US Job Crashed (Attempt {error_count}/{max_consecutive_errors}): {e}", exc_info=True)
                    send_alert(f"US Job Crashed (Attempt {error_count}/{max_consecutive_errors}): {e}", is_error=True)
                    if error_count >= max_consecutive_errors:
                        logger.critical("Too many consecutive errors. Waiting 5 minutes before retry...")
                        send_alert("Too many consecutive errors! Waiting 5 minutes...", is_error=True)
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
    logger.info(f"Safe Mode: {IS_SAFE_MODE}, Strategy: {STRATEGY_MODE}")
    
    # Startup notification
    send_alert(f"🚀 Bot Started!\nMode: {'Safe' if IS_SAFE_MODE else 'Leverage'}\nStrategy: {STRATEGY_MODE.upper()}")
    
    # Heartbeat
    def heartbeat():
        status = get_market_status()
        logger.info(f"Heartbeat: Bot is alive... Market Status: {status}")
        
    schedule.every(1).minutes.do(heartbeat)
    
    # Startup Check
    ctx = get_market_status()
    if ctx != 'CLOSED':
        logger.info(f"Bot started during {ctx} Trading Hours. Launching job immediately.")
        try:
            job()
        except Exception as e:
            logger.critical(f"Startup Job Crashed: {e}", exc_info=True)
            send_alert(f"Startup Job Crashed: {e}", is_error=True)

    # Run main loop with automatic recovery
    run_with_recovery()
