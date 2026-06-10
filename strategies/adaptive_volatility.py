"""Adaptive Volatility Strategy v2.7 — Dynamic K, ATR-based stops, Scaled Entry.

Key improvements:
1. Adaptive K value based on recent volatility (ATR)
2. Multi-timeframe trend confirmation
3. Scaled entry (3-stage position building)
4. Better leverage ETF handling
5. Momentum filter for stronger breakouts
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple


def calculate_atr(ohlc_data: List[Dict], period: int = 14) -> float:
    """Calculate Average True Range from OHLC data.
    
    Args:
        ohlc_data: List of OHLC dicts with 'high', 'low', 'clos' keys
        period: ATR period (default 14)
    
    Returns:
        ATR value or 0.0 if insufficient data
    """
    if not ohlc_data or len(ohlc_data) < period + 1:
        return 0.0
    
    true_ranges = []
    for i in range(1, min(len(ohlc_data), period + 1)):
        try:
            high = float(ohlc_data[i].get('high', 0) or 0)
            low = float(ohlc_data[i].get('low', 0) or 0)
            prev_close = float(ohlc_data[i-1].get('clos', 0) or 0)
            
            if high <= 0 or low <= 0 or prev_close <= 0:
                continue
                
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
            true_ranges.append(tr)
        except (ValueError, TypeError):
            continue
    
    if not true_ranges:
        return 0.0
    
    return sum(true_ranges) / len(true_ranges)


def calculate_adaptive_k(ohlc_data: List[Dict], base_k: float = 0.5) -> float:
    """Calculate adaptive K value based on recent volatility.
    
    Higher volatility → Lower K (tighter target, faster entry)
    Lower volatility → Higher K (wider target, wait for stronger breakout)
    
    Args:
        ohlc_data: List of OHLC data
        base_k: Base K value (default 0.5)
    
    Returns:
        Adaptive K value (0.3 ~ 0.7 range)
    """
    if not ohlc_data or len(ohlc_data) < 10:
        return base_k
    
    try:
        # Calculate recent volatility (10-day ATR as % of price)
        atr_10 = calculate_atr(ohlc_data, 10)
        current_price = float(ohlc_data[0].get('clos', 0) or 0)
        
        if current_price <= 0 or atr_10 <= 0:
            return base_k
        
        atr_pct = (atr_10 / current_price) * 100
        
        # Adaptive K logic:
        # - High volatility (ATR% > 3%): K = 0.3 (tight target, quick entry)
        # - Medium volatility (2% ~ 3%): K = 0.4 (balanced)
        # - Low volatility (1% ~ 2%): K = 0.5 (standard)
        # - Very low volatility (< 1%): K = 0.6 (wait for stronger signal)
        
        if atr_pct > 3.0:
            adaptive_k = 0.3
        elif atr_pct > 2.0:
            adaptive_k = 0.35
        elif atr_pct > 1.5:
            adaptive_k = 0.4
        elif atr_pct > 1.0:
            adaptive_k = 0.5
        else:
            adaptive_k = 0.6
        
        return adaptive_k
        
    except Exception:
        return base_k


def calculate_target_price_v2(
    today_open: float,
    ohlc_data: List[Dict],
    k: float = None,
    use_adaptive_k: bool = True,
    use_atr_range: bool = True,
) -> Tuple[Optional[float], float, Dict]:
    """
    Enhanced volatility breakout target price calculation.
    
    Args:
        today_open: Today's opening price
        ohlc_data: Historical OHLC data (index 0 = most recent)
        k: Optional fixed K value. If None and use_adaptive_k=True, auto-calculated.
        use_adaptive_k: Use adaptive K based on volatility
        use_atr_range: Use ATR instead of single-day range
    
    Returns:
        Tuple of (target_price, effective_k, metrics_dict)
    """
    if not ohlc_data or len(ohlc_data) < 1 or not today_open or today_open <= 0:
        return None, 0.5, {}
    
    metrics = {}
    
    try:
        # 1. Determine K value
        if k is not None:
            effective_k = k
        elif use_adaptive_k:
            effective_k = calculate_adaptive_k(ohlc_data)
        else:
            effective_k = 0.5
        
        metrics['k'] = round(effective_k, 2)
        
        # 2. Calculate range
        if use_atr_range and len(ohlc_data) >= 5:
            # Use 5-day ATR for more stable range
            atr = calculate_atr(ohlc_data, 5)
            range_value = atr
            metrics['range_type'] = 'ATR5'
        else:
            # Fallback to previous day range
            yesterday = ohlc_data[0]
            prev_high = float(yesterday.get('high', 0) or 0)
            prev_low = float(yesterday.get('low', 0) or 0)
            range_value = prev_high - prev_low if prev_high > prev_low else 0
            metrics['range_type'] = 'prev_day'
        
        if range_value <= 0:
            return None, effective_k, metrics
        
        metrics['range'] = round(range_value, 4)
        
        # 3. Calculate target price
        target_price = today_open + (range_value * effective_k)
        metrics['target'] = round(target_price, 2)
        metrics['breakout_pct'] = round((target_price - today_open) / today_open * 100, 2)
        
        return target_price, effective_k, metrics
        
    except Exception as e:
        return None, 0.5, {'error': str(e)}


def check_multi_timeframe_trend(
    ohlc_data: List[Dict],
    current_price: float,
) -> Tuple[bool, Dict]:
    """
    Multi-timeframe trend confirmation.
    
    Checks:
    - 5-day trend (short-term)
    - 20-day trend (medium-term)
    - Price vs 5/20 MA
    
    Returns:
        Tuple of (is_bullish, metrics_dict)
    """
    if not ohlc_data or len(ohlc_data) < 20 or not current_price:
        return True, {}  # Default to bullish if insufficient data
    
    try:
        closes = []
        for d in ohlc_data[:20]:
            c = float(d.get('clos', 0) or 0)
            if c > 0:
                closes.append(c)
        
        if len(closes) < 20:
            return True, {}
        
        ma5 = sum(closes[:5]) / 5
        ma20 = sum(closes[:20]) / 20
        
        # Trend conditions
        price_above_ma5 = current_price > ma5
        price_above_ma20 = current_price > ma20
        ma5_above_ma20 = ma5 > ma20
        
        # 5-day momentum (closes ascending)
        short_momentum = closes[0] > closes[4] if len(closes) >= 5 else True
        
        # Score
        bullish_count = sum([
            price_above_ma5,
            price_above_ma20,
            ma5_above_ma20,
            short_momentum,
        ])
        
        # Need at least 2/4 conditions for bullish
        is_bullish = bullish_count >= 2
        
        metrics = {
            'ma5': round(ma5, 2),
            'ma20': round(ma20, 2),
            'price_above_ma5': price_above_ma5,
            'price_above_ma20': price_above_ma20,
            'ma5_above_ma20': ma5_above_ma20,
            'short_momentum': short_momentum,
            'bullish_score': f"{bullish_count}/4",
        }
        
        return is_bullish, metrics
        
    except Exception:
        return True, {}


def calculate_scaled_entry(
    available_cash: float,
    current_price: float,
    target_price: float,
    signal_strength: float = 0.5,
    num_targets: int = 1,
    weight: float = 1.0,
    stage: int = 1,
    total_stages: int = 3,
) -> Tuple[int, Dict]:
    """
    Calculate scaled entry position size.
    
    Stage 1: 40% of target position (initial breakout)
    Stage 2: 35% of target position (confirmation)
    Stage 3: 25% of target position (trend continuation)
    
    Args:
        available_cash: Available cash for trading
        current_price: Current price
        target_price: Breakout target price
        signal_strength: Signal strength (0-1)
        num_targets: Number of target tickers
        weight: Dynamic portfolio weight
        stage: Current entry stage (1, 2, or 3)
        total_stages: Total number of stages
    
    Returns:
        Tuple of (quantity, metrics_dict)
    """
    if not available_cash or not current_price or current_price <= 0:
        return 1, {}
    
    # Stage allocation
    stage_ratios = {1: 0.40, 2: 0.35, 3: 0.25}
    stage_ratio = stage_ratios.get(stage, 0.40)
    
    # Calculate per-ticker allocation
    per_ticker_cash = available_cash / max(num_targets, 1)
    
    # Apply signal strength multiplier
    if signal_strength >= 0.8:
        strength_mult = 1.0
    elif signal_strength >= 0.6:
        strength_mult = 0.8
    elif signal_strength >= 0.4:
        strength_mult = 0.6
    else:
        strength_mult = 0.4
    
    # Apply weight
    effective_weight = max(0.1, min(1.0, float(weight or 1.0)))
    
    # Calculate position size for this stage
    max_position = per_ticker_cash * strength_mult * effective_weight
    stage_position = max_position * stage_ratio
    
    quantity = int(stage_position / current_price)
    
    metrics = {
        'stage': stage,
        'stage_ratio': stage_ratio,
        'strength_mult': strength_mult,
        'weight': effective_weight,
        'stage_position': round(stage_position, 2),
        'quantity': max(quantity, 1),
    }
    
    return max(quantity, 1), metrics


def calculate_dynamic_stop_loss(
    entry_price: float,
    current_price: float,
    ohlc_data: List[Dict],
    base_stop_pct: float = -5.0,
    leverage_factor: float = 1.0,
    use_atr_stop: bool = True,
    atr_multiplier: float = 2.0,
) -> Tuple[float, float, Dict]:
    """
    Calculate dynamic stop loss based on ATR and leverage.
    
    For leveraged ETFs, the stop is adjusted to account for
    amplified moves while protecting capital.
    
    Args:
        entry_price: Entry/buy price
        current_price: Current price
        ohlc_data: Historical OHLC data
        base_stop_pct: Base stop loss percentage (negative, e.g., -5.0)
        leverage_factor: Leverage multiplier (1, 2, or 3)
        use_atr_stop: Use ATR-based dynamic stop
        atr_multiplier: ATR multiplier for stop distance
    
    Returns:
        Tuple of (stop_price, stop_pct, metrics_dict)
    """
    if not entry_price or entry_price <= 0:
        return 0.0, base_stop_pct, {}
    
    metrics = {}
    
    try:
        # 1. Calculate base stop
        # For leveraged ETFs, use conservative stop factor
        # 3X ETF: (1+3)/2 = 2.0 → base -5% becomes -10%
        # 2X ETF: (1+2)/2 = 1.5 → base -5% becomes -7.5%
        if leverage_factor > 1:
            lev_stop_factor = (1 + leverage_factor) / 2
        else:
            lev_stop_factor = 1.0
        
        adjusted_base_pct = base_stop_pct * lev_stop_factor
        metrics['base_stop_pct'] = base_stop_pct
        metrics['lev_factor'] = leverage_factor
        metrics['adjusted_base_pct'] = round(adjusted_base_pct, 2)
        
        # 2. Calculate ATR-based stop
        if use_atr_stop and ohlc_data:
            atr = calculate_atr(ohlc_data, 14)
            if atr > 0:
                atr_stop_distance = atr * atr_multiplier
                atr_stop_pct = -(atr_stop_distance / entry_price) * 100
                
                # Use the more lenient stop (smaller absolute value)
                # but cap at -10% for leverage protection
                max_stop_pct = -10.0 * lev_stop_factor
                
                # Choose whichever is less severe (closer to 0)
                if atr_stop_pct > adjusted_base_pct:
                    # ATR suggests tighter stop - use base
                    final_stop_pct = adjusted_base_pct
                else:
                    # ATR suggests wider stop - use ATR but cap
                    final_stop_pct = max(atr_stop_pct, max_stop_pct)
                
                metrics['atr'] = round(atr, 4)
                metrics['atr_stop_pct'] = round(atr_stop_pct, 2)
            else:
                final_stop_pct = adjusted_base_pct
        else:
            final_stop_pct = adjusted_base_pct
        
        # 3. Calculate stop price
        stop_price = entry_price * (1 + final_stop_pct / 100)
        
        metrics['final_stop_pct'] = round(final_stop_pct, 2)
        metrics['stop_price'] = round(stop_price, 4)
        
        return stop_price, final_stop_pct, metrics
        
    except Exception as e:
        # Fallback to base stop
        stop_price = entry_price * (1 + base_stop_pct / 100)
        return stop_price, base_stop_pct, {'error': str(e)}


def calculate_adaptive_trailing(
    entry_price: float,
    current_price: float,
    highest_price: float,
    ohlc_data: List[Dict],
    leverage_factor: float = 1.0,
    base_activation_pct: float = 5.0,
    base_drop_pct: float = 3.0,
) -> Tuple[bool, float, Dict]:
    """
    Calculate adaptive trailing stop parameters.
    
    For leveraged ETFs, adjusts activation and drop thresholds
    to account for amplified moves.
    
    Args:
        entry_price: Entry price
        current_price: Current price
        highest_price: Highest price since entry
        ohlc_data: Historical OHLC data
        leverage_factor: Leverage multiplier
        base_activation_pct: Base trailing activation (e.g., +5%)
        base_drop_pct: Base drop from high to trigger (e.g., 3%)
    
    Returns:
        Tuple of (should_sell, trailing_stop_price, metrics_dict)
    """
    if not entry_price or not current_price or not highest_price:
        return False, 0.0, {}
    
    metrics = {}
    
    try:
        # Scale activation for leverage (more room for leveraged ETFs)
        # 3X ETF: 5% * 3 = 15% activation
        # 2X ETF: 5% * 2 = 10% activation
        scaled_activation_pct = base_activation_pct * leverage_factor
        
        # Scale drop for leverage (conservative - use (1+lev)/2)
        # 3X ETF: 3% * 2 = 6% drop
        # 2X ETF: 3% * 1.5 = 4.5% drop
        lev_stop_factor = (1 + leverage_factor) / 2 if leverage_factor > 1 else 1.0
        scaled_drop_pct = base_drop_pct * lev_stop_factor
        
        metrics['leverage_factor'] = leverage_factor
        metrics['scaled_activation_pct'] = round(scaled_activation_pct, 2)
        metrics['scaled_drop_pct'] = round(scaled_drop_pct, 2)
        
        # Calculate current gain from entry
        gain_pct = ((highest_price - entry_price) / entry_price) * 100
        current_gain_pct = ((current_price - entry_price) / entry_price) * 100
        
        metrics['gain_from_entry'] = round(gain_pct, 2)
        metrics['current_gain'] = round(current_gain_pct, 2)
        
        # Check if trailing is activated
        is_activated = gain_pct >= scaled_activation_pct
        metrics['trailing_activated'] = is_activated
        
        if not is_activated:
            return False, 0.0, metrics
        
        # Calculate trailing stop price
        trailing_stop_price = highest_price * (1 - scaled_drop_pct / 100)
        metrics['trailing_stop_price'] = round(trailing_stop_price, 4)
        
        # Check if should sell
        drop_from_high = ((highest_price - current_price) / highest_price) * 100
        metrics['drop_from_high'] = round(drop_from_high, 2)
        
        should_sell = current_price <= trailing_stop_price
        
        return should_sell, trailing_stop_price, metrics
        
    except Exception as e:
        return False, 0.0, {'error': str(e)}


def check_momentum_filter(
    ohlc_data: List[Dict],
    current_price: float,
    current_volume: float,
    volume_threshold: float = 1.5,
) -> Tuple[bool, float, Dict]:
    """
    Check momentum conditions for breakout confirmation.
    
    Conditions:
    1. Volume above average (1.5x threshold)
    2. Price momentum positive (current > 5-day SMA)
    3. Not overbought (RSI < 80)
    
    Returns:
        Tuple of (passes_filter, momentum_score, metrics_dict)
    """
    if not ohlc_data or len(ohlc_data) < 15:
        return True, 0.5, {}  # Default pass if insufficient data
    
    metrics = {}
    score = 0.0
    conditions_met = 0
    total_conditions = 3
    
    try:
        closes = [float(d.get('clos', 0) or 0) for d in ohlc_data[:15] if float(d.get('clos', 0) or 0) > 0]
        volumes = [float(d.get('tvol', 0) or 0) for d in ohlc_data[1:6] if float(d.get('tvol', 0) or 0) > 0]
        
        # 1. Volume check
        if volumes:
            avg_volume = sum(volumes) / len(volumes)
            volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0
            volume_pass = volume_ratio >= volume_threshold
            metrics['volume_ratio'] = round(volume_ratio, 2)
            metrics['volume_pass'] = volume_pass
            if volume_pass:
                conditions_met += 1
                score += 0.35
        
        # 2. Price momentum (current > 5-day SMA)
        if len(closes) >= 5:
            sma5 = sum(closes[:5]) / 5
            momentum_pass = current_price > sma5
            metrics['sma5'] = round(sma5, 2)
            metrics['momentum_pass'] = momentum_pass
            if momentum_pass:
                conditions_met += 1
                score += 0.35
        
        # 3. RSI check (not overbought)
        if len(closes) >= 14:
            gains = []
            losses = []
            for i in range(1, 14):
                diff = closes[i-1] - closes[i]  # reversed order
                if diff > 0:
                    gains.append(diff)
                    losses.append(0)
                else:
                    gains.append(0)
                    losses.append(-diff)
            
            avg_gain = sum(gains) / 14
            avg_loss = sum(losses) / 14
            
            if avg_loss == 0:
                rsi = 100
            else:
                rs = avg_gain / avg_loss
                rsi = 100 - (100 / (1 + rs))
            
            rsi_pass = rsi < 80
            metrics['rsi'] = round(rsi, 1)
            metrics['rsi_pass'] = rsi_pass
            if rsi_pass:
                conditions_met += 1
                score += 0.30
        
        passes_filter = conditions_met >= 2  # Need 2/3 conditions
        metrics['conditions_met'] = f"{conditions_met}/{total_conditions}"
        
        return passes_filter, score, metrics
        
    except Exception as e:
        return True, 0.5, {'error': str(e)}
