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
        past_volumes = [float(x['tvol']) if x.get('tvol') not in (None, '') else 0.0 for x in ohlc_data[1:window+1]]
        if not past_volumes:
            return False
            
        avg_vol = sum(past_volumes) / len(past_volumes)
        
        if avg_vol == 0:
            return True # Avoid division by zero, assume spike if historical is 0
            
        return current_vol >= (avg_vol * threshold)
    except Exception as e:
        print(f"Volume check error: {e}")
        return False


def check_gap_down(current_price, ohlc_data, threshold_pct=3.0):
    """
    갭다운(Gap Down) 감지: 전일 종가 대비 급락 여부 확인.
    급락장 진입을 차단하기 위한 방어 필터.
    
    Args:
        current_price (float): 현재가
        ohlc_data (list): KIS OHLC 데이터 (index 0 = 최근)
        threshold_pct (float): 갭다운 기준 (기본 3%)
    
    Returns:
        tuple: (is_gap_down: bool, drop_pct: float)
    """
    if not current_price or not ohlc_data or len(ohlc_data) < 1:
        return False, 0.0
    
    try:
        prev_close = float(ohlc_data[0]['clos']) if ohlc_data[0].get('clos') not in (None, '') else 0.0
        if prev_close <= 0:
            return False, 0.0
        
        drop_pct = ((prev_close - current_price) / prev_close) * 100
        return drop_pct >= threshold_pct, round(drop_pct, 2)
    except Exception as e:
        print(f"Gap down check error: {e}")
        return False, 0.0


def check_consecutive_decline(ohlc_data, days=2, threshold_pct=3.0):
    """
    연속 하락 감지: 최근 N일간 연속 하락하고 누적 하락률이 기준 이상인지 확인.
    
    Args:
        ohlc_data (list): KIS OHLC 데이터 (index 0 = 최근)
        days (int): 확인할 연속 일수 (기본 2일)
        threshold_pct (float): 누적 하락률 기준 (기본 3%)
    
    Returns:
        tuple: (is_declining: bool, cumulative_drop_pct: float)
    """
    if not ohlc_data or len(ohlc_data) < days + 1:
        return False, 0.0
    
    try:
        # Check each day was a decline (close < open or close < prev_close)
        consecutive_drops = 0
        for i in range(days):
            close = float(ohlc_data[i]['clos']) if ohlc_data[i].get('clos') not in (None, '') else 0.0
            open_price = float(ohlc_data[i]['open']) if ohlc_data[i].get('open') not in (None, '') else 0.0
            if close < open_price:
                consecutive_drops += 1
        
        # Calculate cumulative drop from N days ago to latest
        latest_close = float(ohlc_data[0]['clos']) if ohlc_data[0].get('clos') not in (None, '') else 0.0
        oldest_close = float(ohlc_data[days]['clos']) if ohlc_data[days].get('clos') not in (None, '') else 0.0
        
        if oldest_close <= 0:
            return False, 0.0
        
        cumulative_drop = ((oldest_close - latest_close) / oldest_close) * 100
        
        is_declining = consecutive_drops >= days and cumulative_drop >= threshold_pct
        return is_declining, round(cumulative_drop, 2)
    except Exception as e:
        print(f"Consecutive decline check error: {e}")
        return False, 0.0


def calculate_short_ma(prices, window=5):
    """
    단기 이동평균 계산 (급락 대응용).
    20MA보다 빠르게 추세 전환을 감지.
    
    Args:
        prices (list): 종가 리스트
        window (int): 기간 (기본 5일)
    
    Returns:
        float or None
    """
    if len(prices) < window:
        return None
    series = pd.Series(prices)
    ma = series.rolling(window=window).mean()
    return ma.iloc[-1]


def check_portfolio_drawdown(holdings, threshold_pct=5.0):
    """
    포트폴리오 전체 드로다운 체크: 총 평가손익률이 기준 이상 손실이면 경고.
    
    Args:
        holdings (list): 보유 종목 리스트 (각 항목에 profit_pct 또는 evlu_pfls_rt 포함)
        threshold_pct (float): 전체 포트폴리오 손실 기준 (기본 5%)
    
    Returns:
        tuple: (is_drawdown: bool, total_loss_pct: float)
    """
    if not holdings:
        return False, 0.0
    
    try:
        total_invest = 0.0
        total_eval = 0.0
        
        for h in holdings:
            # US holdings format
            if 'avg_price' in h and 'cur_price' in h:
                qty = float(h.get('qty', 0)) if h.get('qty') not in (None, '') else 0.0
                avg = float(h.get('avg_price', 0)) if h.get('avg_price') not in (None, '') else 0.0
                cur = float(h.get('cur_price', 0)) if h.get('cur_price') not in (None, '') else 0.0
            # KR holdings format
            elif 'pchs_avg_pric' in h:
                qty = float(h.get('hldg_qty', h.get('ord_psbl_qty', 0))) if h.get('hldg_qty', h.get('ord_psbl_qty', '')) not in (None, '') else 0.0
                avg = float(h.get('pchs_avg_pric', 0)) if h.get('pchs_avg_pric') not in (None, '') else 0.0
                cur = float(h.get('prpr', h.get('now_pric2', 0))) if h.get('prpr', h.get('now_pric2', '')) not in (None, '') else 0.0
            else:
                continue
            
            if qty > 0 and avg > 0:
                total_invest += qty * avg
                total_eval += qty * cur
        
        if total_invest <= 0:
            return False, 0.0
        
        loss_pct = ((total_invest - total_eval) / total_invest) * 100
        return loss_pct >= threshold_pct, round(loss_pct, 2)
    except Exception as e:
        print(f"Portfolio drawdown check error: {e}")
        return False, 0.0

