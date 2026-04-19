"""
수익 추적 모듈 (Profit Tracker)

실현손익 + 평가손익을 통합 추적합니다.
- 실현손익: 체결(매도) 내역에서 계산 (이미 청산한 포지션의 손익)
- 평가손익: 현재 보유 중인 종목의 미실현 손익
- 월별 스냅샷: 매월 자산 현황을 기록하여 누적 수익률 추적

KIS API 사용:
- 국내: TTTC8001R (주식일별주문체결조회)
- 해외: TTTS3035R (해외주식 주문체결내역)
"""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from modules.logger import logger

BASE_DIR = Path(__file__).resolve().parent.parent
PROFIT_DB_FILE = BASE_DIR / "database/profit_history.json"
SNAPSHOT_DB_FILE = BASE_DIR / "database/asset_snapshots.json"


def _load_json(filepath):
    """JSON 파일 로드 (없으면 빈 dict 반환)"""
    if filepath.exists():
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_json(filepath, data):
    """JSON 파일 저장"""
    os.makedirs(filepath.parent, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def fetch_kr_realized_profit(kis_kr, start_date, end_date):
    """
    KR 시장 실현손익 조회
    체결된 매도 주문에서 손익을 계산합니다.
    
    Args:
        kis_kr: KisDomestic 인스턴스
        start_date: YYYYMMDD
        end_date: YYYYMMDD
    Returns:
        dict: {realized_profit, trades: [...]}
    """
    try:
        # 매도 체결 내역만 조회
        res = kis_kr.get_executed_orders(start_date, end_date, sell_buy="01")
        
        if not res or res.get('rt_cd') != '0':
            error_msg = res.get('msg1', 'Unknown error') if res else 'No response'
            logger.warning(f"[ProfitTracker] KR 체결내역 조회 실패: {error_msg}")
            return {"realized_profit": 0, "trades": [], "error": error_msg}
        
        trades = []
        total_realized = 0
        
        for item in res.get('output1', []):
            trade_qty = int(item.get('tot_ccld_qty', '0') or '0')
            if trade_qty <= 0:
                continue
            
            ticker = item.get('pdno', 'N/A')
            name = item.get('prdt_name', 'N/A')
            sell_price = float(item.get('avg_prvs', '0') or '0')  # 체결평균가
            sell_amt = float(item.get('tot_ccld_amt', '0') or '0')  # 총체결금액
            order_date = item.get('ord_dt', '')
            sll_buy = item.get('sll_buy_dvsn_cd_name', item.get('sll_buy_dvsn_cd', ''))
            
            trades.append({
                "date": order_date,
                "ticker": ticker,
                "name": name,
                "type": sll_buy,
                "qty": trade_qty,
                "price": sell_price,
                "amount": sell_amt
            })
        
        return {"realized_profit": total_realized, "trades": trades}
        
    except Exception as e:
        logger.error(f"[ProfitTracker] KR 실현손익 조회 에러: {e}")
        return {"realized_profit": 0, "trades": [], "error": str(e)}


def fetch_us_realized_profit(kis_us, start_date, end_date):
    """
    US 시장 실현손익 조회
    체결된 매도 주문에서 손익을 계산합니다.
    
    Args:
        kis_us: KisOverseas 인스턴스
        start_date: YYYYMMDD
        end_date: YYYYMMDD
    Returns:
        dict: {realized_profit, trades: [...]}
    """
    try:
        # 매도 체결 내역만 조회
        res = kis_us.get_executed_orders(start_date, end_date, sell_buy="01")
        
        if not res or res.get('rt_cd') != '0':
            error_msg = res.get('msg1', 'Unknown error') if res else 'No response'
            logger.warning(f"[ProfitTracker] US 체결내역 조회 실패: {error_msg}")
            return {"realized_profit": 0, "trades": [], "error": error_msg}
        
        trades = []
        
        for item in res.get('output', res.get('output1', [])):
            trade_qty = int(float(item.get('ft_ccld_qty', item.get('ccld_qty', '0')) or '0'))
            if trade_qty <= 0:
                continue
            
            ticker = item.get('pdno', item.get('ovrs_pdno', 'N/A'))
            name = item.get('prdt_name', item.get('ovrs_item_name', 'N/A'))
            sell_price = float(item.get('ft_ccld_unpr3', item.get('ccld_unpr', '0')) or '0')
            sell_amt = float(item.get('ft_ccld_amt', item.get('ccld_amt', '0')) or '0')
            order_date = item.get('ord_dt', '')
            sll_buy = item.get('sll_buy_dvsn_cd_name', item.get('sll_buy_dvsn_cd', ''))
            
            trades.append({
                "date": order_date,
                "ticker": ticker,
                "name": name,
                "type": sll_buy,
                "qty": trade_qty,
                "price": sell_price,
                "amount": sell_amt
            })
        
        return {"realized_profit": 0, "trades": trades}
        
    except Exception as e:
        logger.error(f"[ProfitTracker] US 실현손익 조회 에러: {e}")
        return {"realized_profit": 0, "trades": [], "error": str(e)}


def fetch_all_trades(kis_kr, kis_us, start_date, end_date):
    """
    전체 체결내역 조회 (매수 + 매도)
    
    Args:
        kis_kr: KisDomestic 인스턴스
        kis_us: KisOverseas 인스턴스
        start_date: YYYYMMDD
        end_date: YYYYMMDD
    Returns:
        dict: {kr_trades: [...], us_trades: [...]}
    """
    kr_trades = []
    us_trades = []
    
    try:
        kr_res = kis_kr.get_executed_orders(start_date, end_date, sell_buy="00")
        if kr_res and kr_res.get('rt_cd') == '0':
            for item in kr_res.get('output1', []):
                qty = int(item.get('tot_ccld_qty', '0') or '0')
                if qty <= 0:
                    continue
                kr_trades.append({
                    "date": item.get('ord_dt', ''),
                    "ticker": item.get('pdno', 'N/A'),
                    "name": item.get('prdt_name', 'N/A'),
                    "type": "매도" if item.get('sll_buy_dvsn_cd') == '01' else "매수",
                    "qty": qty,
                    "price": float(item.get('avg_prvs', '0') or '0'),
                    "amount": float(item.get('tot_ccld_amt', '0') or '0')
                })
    except Exception as e:
        logger.error(f"[ProfitTracker] KR 전체 체결 조회 에러: {e}")
    
    time.sleep(0.5)  # Rate limit
    
    try:
        us_res = kis_us.get_executed_orders(start_date, end_date, sell_buy="00")
        if us_res and us_res.get('rt_cd') == '0':
            for item in us_res.get('output', us_res.get('output1', [])):
                qty = int(float(item.get('ft_ccld_qty', item.get('ccld_qty', '0')) or '0'))
                if qty <= 0:
                    continue
                sll_buy = item.get('sll_buy_dvsn_cd', '02')
                us_trades.append({
                    "date": item.get('ord_dt', ''),
                    "ticker": item.get('pdno', item.get('ovrs_pdno', 'N/A')),
                    "name": item.get('prdt_name', item.get('ovrs_item_name', 'N/A')),
                    "type": "매도" if sll_buy == '01' else "매수",
                    "qty": qty,
                    "price": float(item.get('ft_ccld_unpr3', item.get('ccld_unpr', '0')) or '0'),
                    "amount": float(item.get('ft_ccld_amt', item.get('ccld_amt', '0')) or '0')
                })
    except Exception as e:
        logger.error(f"[ProfitTracker] US 전체 체결 조회 에러: {e}")
    
    return {"kr_trades": kr_trades, "us_trades": us_trades}


def take_asset_snapshot(kis_kr, kis_us):
    """
    현재 자산 현황 스냅샷 저장 (일별)
    
    기록 항목:
    - 날짜, KR 예수금, KR 평가금액, KR 평가손익
    - US 예수금(USD), US 평가금액, US 평가손익
    - 총 자산 (KRW 환산)
    """
    today = datetime.now().strftime("%Y-%m-%d")
    snapshots = _load_json(SNAPSHOT_DB_FILE)
    
    # 오늘 이미 기록했으면 스킵
    if today in snapshots:
        logger.info(f"[ProfitTracker] 오늘({today}) 스냅샷 이미 존재, 스킵")
        return snapshots[today]
    
    snapshot = {
        "date": today,
        "timestamp": time.time(),
        "kr": {"deposit": 0, "eval_total": 0, "eval_profit": 0, "holdings_count": 0},
        "us": {"deposit_usd": 0, "eval_total_usd": 0, "eval_profit_usd": 0, "holdings_count": 0},
        "total_krw": 0
    }
    
    usd_krw = 1450.0  # 기본 환율
    
    # KR 자산
    try:
        kr_bal = kis_kr.get_balance()
        if kr_bal and kr_bal.get('rt_cd') == '0' and 'output2' in kr_bal:
            summary = kr_bal['output2'][0] if kr_bal['output2'] else {}
            snapshot["kr"]["deposit"] = int(summary.get('dnca_tot_amt', '0') or '0')
            snapshot["kr"]["eval_total"] = int(summary.get('tot_evlu_amt', '0') or '0')
            snapshot["kr"]["eval_profit"] = int(summary.get('evlu_pfls_smtl_amt', '0') or '0')
            snapshot["kr"]["holdings_count"] = len([
                h for h in kr_bal.get('output1', [])
                if int(h.get('hldg_qty', '0') or '0') > 0
            ])
    except Exception as e:
        logger.error(f"[ProfitTracker] KR 스냅샷 에러: {e}")
    
    time.sleep(0.5)
    
    # US 자산
    try:
        us_fb = kis_us.get_foreign_balance()
        if us_fb and 'deposit' in us_fb:
            snapshot["us"]["deposit_usd"] = float(us_fb['deposit'])
        
        us_bal = kis_us.get_balance()
        if us_bal and 'output1' in us_bal:
            total_eval = 0
            total_profit = 0
            count = 0
            for h in us_bal['output1']:
                qty = int(float(h.get('ovrs_cblc_qty', h.get('ord_psbl_qty', '0')) or '0'))
                if qty <= 0:
                    continue
                count += 1
                eval_amt = float(h.get('ovrs_stck_evlu_amt', h.get('frcr_evlu_amt', '0')) or '0')
                profit = float(h.get('frcr_evlu_pfls_amt', h.get('evlu_pfls_amt', '0')) or '0')
                total_eval += eval_amt
                total_profit += profit
            
            snapshot["us"]["eval_total_usd"] = round(total_eval, 2)
            snapshot["us"]["eval_profit_usd"] = round(total_profit, 2)
            snapshot["us"]["holdings_count"] = count
    except Exception as e:
        logger.error(f"[ProfitTracker] US 스냅샷 에러: {e}")
    
    # 총 자산 (KRW 환산)
    kr_total = snapshot["kr"]["deposit"] + snapshot["kr"]["eval_total"]
    us_total_krw = (snapshot["us"]["deposit_usd"] + snapshot["us"]["eval_total_usd"]) * usd_krw
    snapshot["total_krw"] = int(kr_total + us_total_krw)
    
    # 저장
    snapshots[today] = snapshot
    _save_json(SNAPSHOT_DB_FILE, snapshots)
    logger.info(f"[ProfitTracker] 자산 스냅샷 저장: KR={kr_total:,}원, US=${snapshot['us']['deposit_usd'] + snapshot['us']['eval_total_usd']:.2f}")
    
    return snapshot


def get_monthly_summary():
    """
    월별 자산 요약 (스냅샷 데이터 기반)
    
    Returns:
        list: [{month, kr_total, us_total_usd, total_krw, kr_profit, us_profit_usd}, ...]
    """
    snapshots = _load_json(SNAPSHOT_DB_FILE)
    
    if not snapshots:
        return []
    
    # 월별 마지막 스냅샷 그룹핑
    monthly = {}
    for date_str, snap in sorted(snapshots.items()):
        month_key = date_str[:7]  # "2026-04"
        monthly[month_key] = snap  # 마지막 날짜가 최종값
    
    result = []
    for month, snap in sorted(monthly.items()):
        kr = snap.get("kr", {})
        us = snap.get("us", {})
        result.append({
            "month": month,
            "kr_deposit": kr.get("deposit", 0),
            "kr_eval_total": kr.get("eval_total", 0),
            "kr_eval_profit": kr.get("eval_profit", 0),
            "us_deposit_usd": us.get("deposit_usd", 0),
            "us_eval_total_usd": us.get("eval_total_usd", 0),
            "us_eval_profit_usd": us.get("eval_profit_usd", 0),
            "total_krw": snap.get("total_krw", 0),
            "date": snap.get("date", month)
        })
    
    return result


def get_asset_history():
    """
    일별 자산 이력 (차트용)
    
    Returns:
        list: [{date, total_krw, kr_total, us_total_usd}, ...]
    """
    snapshots = _load_json(SNAPSHOT_DB_FILE)
    
    if not snapshots:
        return []
    
    result = []
    for date_str, snap in sorted(snapshots.items()):
        kr = snap.get("kr", {})
        us = snap.get("us", {})
        result.append({
            "date": date_str,
            "total_krw": snap.get("total_krw", 0),
            "kr_total": kr.get("deposit", 0) + kr.get("eval_total", 0),
            "kr_profit": kr.get("eval_profit", 0),
            "us_total_usd": us.get("deposit_usd", 0) + us.get("eval_total_usd", 0),
            "us_profit_usd": us.get("eval_profit_usd", 0)
        })
    
    return result


if __name__ == "__main__":
    from modules.kis_api import KisOverseas
    from modules.kis_domestic import KisDomestic
    
    kis_us = KisOverseas()
    kis_kr = KisDomestic()
    
    # 자산 스냅샷 테스트
    print("=== 자산 스냅샷 ===")
    snap = take_asset_snapshot(kis_kr, kis_us)
    print(json.dumps(snap, indent=2, ensure_ascii=False))
    
    # 최근 30일 체결내역 테스트
    print("\n=== 최근 30일 체결내역 ===")
    today = datetime.now().strftime("%Y%m%d")
    thirty_ago = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
    trades = fetch_all_trades(kis_kr, kis_us, thirty_ago, today)
    print(f"KR trades: {len(trades['kr_trades'])}")
    print(f"US trades: {len(trades['us_trades'])}")
    for t in trades['kr_trades']:
        print(f"  KR: {t['date']} {t['type']} {t['ticker']} {t['name']} x{t['qty']} @ {t['price']}")
    for t in trades['us_trades']:
        print(f"  US: {t['date']} {t['type']} {t['ticker']} {t['name']} x{t['qty']} @ {t['price']}")
