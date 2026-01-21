import pandas as pd

def calculate_ma(prices, window=20):
    """
    Calculate Moving Average
    prices: list of close prices
    window: period (default 20)
    """
    if len(prices) < window:
        return None
    
    series = pd.Series(prices)
    ma = series.rolling(window=window).mean()
    return ma.iloc[-1]

def check_trend(current_price, ma_value):
    """
    Returns True if Bull Market (Price > MA)
    """
    if ma_value is None:
        return False
    return current_price > ma_value

def check_volume_spike(current_vol, ohlc_data, window=5, threshold=1.5):
    """
    Checks if current volume is significantly higher than recent average.
    
    Args:
        current_vol (float): Real-time volume
        ohlc_data (list): List of daily OHLC dictionaries from KIS API
        window (int): Lookback period (default 5 days)
        threshold (float): Factor to determine spike (1.5 = 150% of average)
    """
    if not ohlc_data or len(ohlc_data) < window + 1:
        return False
        
    # KIS OHLC is typically descending (Index 0 comes first? No, verify with run_bot)
    # run_bot reversed closes: closes = [x['clos'] for x in ohlc]; closes.reverse()
    # This implies ohlc[0] is the LATEST (Today/Yesterday).
    
    # Extract volumes from past 'window' days, excluding index 0 (current incomplete day)
    # Actually, KIS returns [Today, Yesterday, D-2...]
    # We want average of [Yesterday...D-5]
    
    try:
        past_volumes = [float(x['tvol']) for x in ohlc_data[1:window+1]]
        if not past_volumes:
            return False
            
        avg_vol = sum(past_volumes) / len(past_volumes)
        
        if avg_vol == 0:
            return True # Avoid division by zero, assume spike if historical is 0
            
        return current_vol >= (avg_vol * threshold)
    except Exception as e:
        print(f"Volume check error: {e}")
        return False

