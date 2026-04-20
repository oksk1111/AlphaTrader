
import json
import os
import time
from datetime import datetime
from pathlib import Path
from modules.kis_api import KisOverseas
from modules.kis_domestic import KisDomestic
from modules.logger import logger

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_FILE = BASE_DIR / "database/account_cache.json"
USD_KRW_RATE = 1450.0

def _safe_float(value, default=0.0):
    """빈 문자열이나 None을 안전하게 float로 변환"""
    try:
        if value is None or value == '':
            return default
        return float(value)
    except (ValueError, TypeError):
        return default

def load_cache():
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_cache(data):
    try:
        current = load_cache()
        # Merge deep
        for k, v in data.items():
            current[k] = v
            
        # Ensure database dir exists
        os.makedirs(CACHE_FILE.parent, exist_ok=True)
            
        with open(CACHE_FILE, "w") as f:
            json.dump(current, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Cache save failed: {e}")

def update_us_account():
    """US 계좌 정보 수집 및 캐싱"""
    try:
        kis = KisOverseas()
        foreign_bal = kis.get_foreign_balance()
        balance = kis.get_balance()
        
        # Check if we got valid data - don't overwrite cache with zeros
        if not foreign_bal and not balance:
            logger.warning("[US Account] API returned no data, keeping existing cache")
            return None
        
        result = {
            "deposit_usd": 0.0, "deposit_krw": 0,
            "profit_usd": 0.0, "profit_krw": 0,
            "total_asset_usd": 0.0, "total_asset_krw": 0,
            "exchange_rate": USD_KRW_RATE,
            "holdings": []
        }
        
        # 외화 예수금
        if foreign_bal and 'deposit' in foreign_bal:
            result["deposit_usd"] = _safe_float(foreign_bal['deposit'])
            result["deposit_krw"] = int(result["deposit_usd"] * USD_KRW_RATE)
        
        # 보유 종목 집계
        total_eval_usd = 0.0
        total_profit_usd = 0.0
        
        if balance and 'output1' in balance:
            for h in balance['output1']:
                ticker = h.get('ovrs_pdno', h.get('pdno', 'N/A'))
                name = h.get('ovrs_item_name', h.get('prdt_name', 'N/A'))
                
                qty_raw = h.get('ovrs_cblc_qty', h.get('ord_psbl_qty', h.get('cblc_qty13', '0')))
                qty = int(_safe_float(qty_raw or '0'))
                
                if qty <= 0: continue
                
                avg_price = _safe_float(h.get('pchs_avg_pric', h.get('avg_unpr3', '0')) or '0')
                cur_price = _safe_float(h.get('now_pric2', h.get('ovrs_now_pric1', '0')) or '0')
                profit = _safe_float(h.get('frcr_evlu_pfls_amt', h.get('evlu_pfls_amt', '0')) or '0')
                profit_pct = _safe_float(h.get('evlu_pfls_rt1', h.get('evlu_pfls_rt', '0')) or '0')
                eval_amt = _safe_float(h.get('ovrs_stck_evlu_amt', h.get('frcr_evlu_amt', '0')) or '0')
                
                # Fallback calculations
                if cur_price == 0 and qty > 0 and eval_amt > 0:
                    cur_price = eval_amt / qty
                if avg_price == 0 and qty > 0:
                    avg_price = (eval_amt - profit) / qty
                if profit_pct == 0 and avg_price > 0:
                    profit_pct = ((cur_price - avg_price) / avg_price) * 100

                total_eval_usd += eval_amt
                total_profit_usd += profit
                
                result["holdings"].append({
                    "ticker": ticker, "name": name, "qty": qty,
                    "avg_price": round(avg_price, 2),
                    "cur_price": round(cur_price, 2),
                    "eval_amt": round(eval_amt, 2),
                    "profit": round(profit, 2),
                    "profit_pct": round(profit_pct, 2)
                })
        
        result["profit_usd"] = round(total_profit_usd, 2)
        result["profit_krw"] = int(total_profit_usd * USD_KRW_RATE)
        result["total_asset_usd"] = round(result["deposit_usd"] + total_eval_usd, 2)
        result["total_asset_krw"] = int(result["total_asset_usd"] * USD_KRW_RATE)
        
        # Only save if we have meaningful data (deposit or holdings)
        if result["deposit_usd"] > 0 or len(result["holdings"]) > 0:
            save_cache({"us": {"data": result, "timestamp": datetime.now().timestamp()}})
            logger.info(f"✅ US Account Cache Updated: deposit=${result['deposit_usd']}, holdings={len(result['holdings'])}")
        else:
            logger.warning("[US Account] No valid data to cache (deposit=0, no holdings)")
        
        return result
    except Exception as e:
        logger.error(f"❌ US Account Update Failed: {e}")
        return None

def update_kr_account():
    """KR 계좌 정보 수집 및 캐싱"""
    try:
        kis = KisDomestic()
        balance = kis.get_balance()
        
        # Check if we got valid data
        if not balance:
            logger.warning("[KR Account] API returned no data, keeping existing cache")
            return None
        
        result = {
            "deposit_krw": "0", "profit_krw": "0", "total_asset_krw": "0", "holdings": []
        }
        
        if balance and 'output2' in balance and len(balance['output2']) > 0:
            summary = balance['output2'][0]
            result["deposit_krw"] = summary.get('dnca_tot_amt', '0')
            result["profit_krw"] = summary.get('evlu_pfls_smtl_amt', '0')
            result["total_asset_krw"] = summary.get('tot_evlu_amt', '0')
        
        if balance and 'output1' in balance:
            for h in balance['output1']:
                if int(h.get('hldg_qty', '0')) > 0:
                    result["holdings"].append({
                        "code": h.get('pdno', 'N/A'),
                        "name": h.get('prdt_name', 'N/A'),
                        "qty": h.get('hldg_qty', '0'),
                        "avg_price": h.get('pchs_avg_pric', '0'),
                        "cur_price": h.get('prpr', '0'),
                        "profit": h.get('evlu_pfls_amt', '0'),
                        "profit_pct": h.get('evlu_pfls_rt', '0')
                    })
        
        # Only save if we have meaningful data
        deposit_val = int(result["deposit_krw"]) if result["deposit_krw"] else 0
        total_val = int(result["total_asset_krw"]) if result["total_asset_krw"] else 0
        
        if deposit_val > 0 or total_val > 0 or len(result["holdings"]) > 0:
            save_cache({"kr": {"data": result, "timestamp": datetime.now().timestamp()}})
            logger.info(f"✅ KR Account Cache Updated: deposit={result['deposit_krw']}, holdings={len(result['holdings'])}")
        else:
            logger.warning("[KR Account] No valid data to cache")
        
        return result
    except Exception as e:
        logger.error(f"❌ KR Account Update Failed: {e}")
        return None

def update_all_accounts():
    update_us_account()
    time.sleep(1) # Rate limit safe
    update_kr_account()
    
    # 자산 스냅샷도 자동으로 기록 (일 1회)
    try:
        from modules.profit_tracker import take_asset_snapshot
        kis_us = KisOverseas()
        kis_kr = KisDomestic()
        time.sleep(1)
        take_asset_snapshot(kis_kr, kis_us)
    except Exception as e:
        logger.warning(f"[AccountManager] 자산 스냅샷 자동 기록 실패 (무시): {e}")

if __name__ == "__main__":
    update_all_accounts()
