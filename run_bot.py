import time
import datetime
import schedule
import sys
from modules.kis_api import KisOverseas
from modules.kis_domestic import KisDomestic
from modules.gemini_analyst import GeminiAnalyst
from modules.logger import logger
from strategies.technical import calculate_ma, check_trend
from strategies.volatility_breakout import calculate_target_price

# Configuration
# "Universe" of Hot ETFs/Stocks to monitor
# The Sniper will watch ALL of these and only attack the ones triggering the strategy.

# --- Trading Mode Settings ---
# If True, trades non-leveraged (1x) ETFs/Stocks only.
# Use this if you don't meet the deposit requirement (10M KRW) for leveraged ETFs.
IS_SAFE_MODE = True 

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

QTY = 1 # Quantity per trade (Adjust based on portfolio size!)
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
    market = get_market_status()
    
    if market == 'CLOSED':
        logger.info("Market is closed. Sleeping.")
        return

    # Select Market Context
    if market == 'US':
        logger.info(f"🇺🇸 Starting US Trading Session for {TARGET_TICKERS_US}")
        kis = KisOverseas()
        tickers = TARGET_TICKERS_US
    else:
        logger.info(f"🇰🇷 Starting KR Trading Session for {TARGET_TICKERS_KR}")
        kis = KisDomestic()
        tickers = TARGET_TICKERS_KR
    
    ai = GeminiAnalyst()
    
    # Dictionary to store monitoring targets
    monitoring_targets = {}

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
        
        if not check_trend(current_price, ma20):
            logger.info(f"[{ticker}] Bear Market (Price < 20MA). Skipping.")
            continue

        # B. Calculate Target Price (Common Logic)
        # Note: KR API OHLC structure is adapted to match US one in kis_domestic.py
        # Need Open price for today.
        
        # Get Quote/Current for Open Price
        # For simplicity, we use OHLC[0] if available or fetch current quote
        # OHLC[0] from API is today's data usually in KIS, but let's be safe.
        today_open = float(ohlc[0]['open']) # API returns latest first usually?
        # Actually kis_domestic returns list daily daily.
        # Let's rely on OHLC data we just got.
        # KIS Domestic OHLC[0] is most recent day.
        
        target_price = calculate_target_price(today_open, ohlc, K_VALUE)
        logger.info(f"[{ticker}] Bull Market! Target Price: {target_price} (Open: {today_open})")
        
        monitoring_targets[ticker] = {
            'target': target_price,
            'status': 'monitoring',  # monitoring, bought
            'buys': 0,
            'exchange': exchange
        }

    if not monitoring_targets:
        logger.info(f"[{market}] No targets found for today. Sleeping.")
        return

    logger.info(f"[{market}] Watch List: {list(monitoring_targets.keys())}")
    
    # 2. Watch Loop
    while True:
        # Check if market closed
        current_market = get_market_status()
        if current_market != market:
            logger.info(f"[{market}] Market Closed. Ending Session.")
            break
            
        for ticker, data in monitoring_targets.items():
            if data['status'] in ['bought', 'failed']:
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
                    logger.info(f"[{ticker}] AI Approved. Buying...")
                    
                    if market == 'US':
                        res = kis.buy_market_order(ticker, QTY, exchange)
                    else:
                        res = kis.buy_market_order(ticker, QTY)
                        
                    # Result checking
                    is_success = False
                    if res:
                        if market == 'US' and res.get('rt_cd') == '0': is_success = True
                        if market == 'KR' and res.get('rt_cd') == '0': is_success = True
                        
                    if is_success:
                        data['status'] = 'bought'
                        data['buys'] += 1
                        logger.info(f"[{ticker}] Buy Success!")
                    else:
                        logger.error(f"[{ticker}] Buy Failed: {res}")
                        # Prevent infinite loop on account errors
                        # APBK1680: ETF Education / APBK1681: Basic Deposit Requirement
                        if res.get('msg_cd') in ['APBK1680', 'APBK1681']:
                            err_msg = res.get('msg1')
                            logger.critical(f"[{ticker}] STOPPING: Account Restriction Detected ({res.get('msg_cd')}). Message: {err_msg}")
                            data['status'] = 'failed'
                        # Generic backoff for other errors
                        else:
                            time.sleep(5)
                else:
                    logger.info(f"[{ticker}] AI Rejected buying due to risk.")
                    time.sleep(10) 

        time.sleep(0.1)
        
    # 3. Market Close Sell-off
    logger.info(f"[{market}] Session End. Selling All Holdings.")
    for ticker, data in monitoring_targets.items():
        if data['status'] == 'bought':
            logger.info(f"[{ticker}] Selling Market Order...")
            if market == 'US':
                kis.sell_market_order(ticker, QTY, data.get('exchange'))
            else:
                kis.sell_market_order(ticker, QTY)

if __name__ == "__main__":
    logger.info("=== Global ETF Sniper Bot Started ===")
    logger.info(f"US Targets: {TARGET_TICKERS_US}")
    logger.info(f"KR Targets: {TARGET_TICKERS_KR}")
    
    # Schedule - Check every 1 minute to trigger job if market is open
    # We replaced the fixed "at 23:30" with a continuous check loop below
    # because we now have two market sessions.
    
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

    while True:
        schedule.run_pending()
        
        # Poll for market start times
        # If we are not in a job (job blocks execution), this loop runs.
        # We need to trigger job() when time is right.
        now = datetime.datetime.now()
        t = int(now.strftime("%H%M"))
        
        # Trigger at 09:00 for KR
        if t == 900:
            try:
                job()
            except Exception as e:
                logger.critical(f"Job Crashed: {e}", exc_info=True)
            time.sleep(60) # Avoid double trigger
            
        # Trigger at 23:30 for US
        if t == 2330:
            try:
                job()
            except Exception as e:
                logger.critical(f"Job Crashed: {e}", exc_info=True)
            time.sleep(60)
            
        time.sleep(1)
