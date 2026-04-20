def calculate_target_price(today_open, ohlc_data, k=0.5):
    """
    변동성 돌파 전략 목표가 계산
    Target = Today Open + (Yesterday Range * K)
    Yesterday Range = Yesterday High - Yesterday Low
    """
    if not ohlc_data or len(ohlc_data) < 1:
        return None
    
    # ohlc_data[0] is assumed to be Yesterday (most recent closed candle)
    # Keys: 'high', 'low' (Check if API returns 'high' or 'ovrs_nmix_hgpr')
    # Based on KIS Overseas Stock API: keys are 'high', 'low'
    
    try:
        yesterday = ohlc_data[0]
        prev_high = float(yesterday['high']) if yesterday.get('high') not in (None, '') else 0.0
        prev_low = float(yesterday['low']) if yesterday.get('low') not in (None, '') else 0.0
        
        rng = prev_high - prev_low
        target_price = today_open + (rng * k)
        
        return target_price
    except Exception as e:
        print(f"[Strategy] Error calculating target: {e}")
        return None
