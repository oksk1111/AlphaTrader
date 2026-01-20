"""
US-ETF-Sniper 경량 대시보드 (FastAPI + Jinja2)
- Streamlit 대비 메모리 사용량 1/3~1/4 수준
"""

import os
import re
import glob
import json
import subprocess
import signal
import pytz
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# 프로젝트 루트 설정
BASE_DIR = Path(__file__).resolve().parent.parent
os.chdir(BASE_DIR)

# FastAPI 앱 초기화
app = FastAPI(title="US-ETF-Sniper Dashboard")

# 템플릿 설정
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))

# --- Config 관리 ---
CONFIG_FILE = BASE_DIR / "user_config.json"

def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {"trading_mode": "safe", "strategy": "day"}

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

# --- 유틸리티 함수 ---
def get_bot_pid():
    """봇 프로세스 ID 조회"""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "run_bot.py"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            pids = result.stdout.strip().split('\n')
            if pids and pids[0]:
                return int(pids[0])
    except Exception:
        pass
    return None

def get_latest_log_file():
    """최신 로그 파일 경로"""
    log_files = glob.glob(str(BASE_DIR / "database" / "trading_*.log"))
    if not log_files:
        return None
    return sorted(log_files)[-1]

def parse_log_line(line):
    """로그 라인 파싱"""
    match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} - (\w+) - (.*)", line)
    if match:
        return {
            "timestamp": match.group(1),
            "level": match.group(2),
            "message": match.group(3)
        }
    return None

def get_market_status():
    """현재 시장 상태 (KST 기준)"""
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.now(kst)
    t = int(now.strftime("%H%M"))
    
    if 2330 <= t <= 2400 or 0 <= t < 600:
        return 'US'
    if 900 <= t <= 1520:
        return 'KR'
    return 'CLOSED'

def parse_ticker_data(parsed_lines):
    """로그에서 티커별 데이터 추출"""
    ticker_data = {}
    
    for line in parsed_lines:
        msg = line['message']
        ticker_match = re.search(r"\[([A-Z0-9]+)\]", msg)
        if not ticker_match:
            continue
        
        ticker = ticker_match.group(1)
        if ticker not in ticker_data:
            ticker_data[ticker] = {
                "current": "N/A", "ma20": "N/A", 
                "target": "N/A", "trend": "Unknown"
            }
        
        # Current & MA20
        m1 = re.search(r"Current: ([^,]+), MA20: (.+)", msg)
        if m1:
            ticker_data[ticker]["current"] = m1.group(1).strip()
            ticker_data[ticker]["ma20"] = m1.group(2).strip()
        
        # Target Price (Bull)
        m2 = re.search(r"Target Price: ([^ ]+)", msg)
        if m2:
            ticker_data[ticker]["target"] = m2.group(1).strip()
            ticker_data[ticker]["trend"] = "Bull 🐂"
        
        # Bear Market
        if "Bear Market" in msg:
            ticker_data[ticker]["trend"] = "Bear 🐻"
            ticker_data[ticker]["target"] = "-"
    
    return ticker_data

def get_account_data():
    """US 계좌 정보 조회 (KIS API)"""
    try:
        from modules.kis_api import KisOverseas
        kis = KisOverseas()
        
        foreign_bal = kis.get_foreign_balance()
        balance = kis.get_balance()
        
        result = {
            "deposit_usd": "N/A",
            "holdings": []
        }
        
        if foreign_bal and 'deposit' in foreign_bal:
            result["deposit_usd"] = f"{foreign_bal['deposit']:.2f}"
        
        if balance and 'output1' in balance:
            for h in balance['output1']:
                result["holdings"].append({
                    "ticker": h.get('ovrs_pdno', h.get('pdno', 'N/A')),
                    "name": h.get('ovrs_item_name', h.get('prdt_name', 'N/A')),
                    "qty": h.get('ovrs_cblc_qty', h.get('ccld_qty_smtl1', '0')),
                    "profit": h.get('frcr_evlu_pfls_amt', h.get('evlu_pfls_amt', '0'))
                })
        
        return result
    except Exception as e:
        return {"deposit_usd": f"Error: {e}", "holdings": []}

def get_kr_account_data():
    """KR 계좌 정보 조회 (KIS API)"""
    try:
        from modules.kis_domestic import KisDomestic
        kis = KisDomestic()
        
        balance = kis.get_balance()
        
        result = {
            "deposit_krw": "0",
            "profit_krw": "0",
            "total_asset_krw": "0",
            "holdings": []
        }
        
        if balance and 'output2' in balance and balance['output2']:
            summary = balance['output2'][0]
            result["deposit_krw"] = summary.get('dnca_tot_amt', '0')
            result["profit_krw"] = summary.get('evlu_pfls_smtl_amt', '0')
            result["total_asset_krw"] = summary.get('tot_evlu_amt', '0')
        
        if balance and 'output1' in balance:
            for h in balance['output1']:
                result["holdings"].append({
                    "code": h.get('pdno', 'N/A'),
                    "name": h.get('prdt_name', 'N/A'),
                    "qty": h.get('hldg_qty', '0'),
                    "avg_price": h.get('pchs_avg_pric', '0'),
                    "cur_price": h.get('prpr', '0'),
                    "profit": h.get('evlu_pfls_amt', '0'),
                    "profit_pct": h.get('evlu_pfls_rt', '0')
                })
        
        return result
    except Exception as e:
        return {"deposit_krw": "Error", "profit_krw": "0", "total_asset_krw": "0", "holdings": [], "error": str(e)}

# --- API 엔드포인트 ---
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """메인 대시보드 페이지"""
    config = load_config()
    bot_pid = get_bot_pid()
    market_status = get_market_status()
    
    # 로그 파싱
    log_file = get_latest_log_file()
    parsed_lines = []
    last_update = "N/A"
    
    if log_file:
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()[-200:]  # 최근 200줄만
        except UnicodeDecodeError:
            with open(log_file, "r", encoding="cp949") as f:
                lines = f.readlines()[-200:]
        
        parsed_lines = [parse_log_line(line) for line in lines]
        parsed_lines = [x for x in parsed_lines if x is not None]
        
        if parsed_lines:
            last_update = parsed_lines[-1]['timestamp']
    
    ticker_data = parse_ticker_data(parsed_lines)
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "config": config,
        "bot_pid": bot_pid,
        "bot_status": "🟢 Running" if bot_pid else "🔴 Stopped",
        "market_status": market_status,
        "last_update": last_update,
        "ticker_data": ticker_data,
        "recent_logs": parsed_lines[-30:][::-1],  # 최근 30개 역순
    })

@app.get("/api/status")
async def api_status():
    """봇 상태 API (AJAX 갱신용)"""
    bot_pid = get_bot_pid()
    market_status = get_market_status()
    
    log_file = get_latest_log_file()
    parsed_lines = []
    last_update = "N/A"
    
    if log_file:
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()[-200:]
        except:
            lines = []
        
        parsed_lines = [parse_log_line(line) for line in lines]
        parsed_lines = [x for x in parsed_lines if x is not None]
        
        if parsed_lines:
            last_update = parsed_lines[-1]['timestamp']
    
    ticker_data = parse_ticker_data(parsed_lines)
    
    return JSONResponse({
        "bot_pid": bot_pid,
        "bot_status": "🟢 Running" if bot_pid else "🔴 Stopped",
        "market_status": market_status,
        "last_update": last_update,
        "ticker_data": ticker_data,
        "recent_logs": parsed_lines[-20:][::-1],
    })

@app.get("/api/account")
async def api_account():
    """US 계좌 정보 API"""
    return JSONResponse(get_account_data())

@app.get("/api/account/kr")
async def api_account_kr():
    """KR 계좌 정보 API"""
    return JSONResponse(get_kr_account_data())

@app.post("/api/config")
async def update_config(request: Request):
    """설정 업데이트"""
    data = await request.json()
    config = load_config()
    
    if "trading_mode" in data:
        config["trading_mode"] = data["trading_mode"]
    if "strategy" in data:
        config["strategy"] = data["strategy"]
    
    save_config(config)
    return JSONResponse({"success": True, "config": config})

@app.post("/api/restart")
async def restart_bot():
    """봇 재시작"""
    old_pid = get_bot_pid()
    
    # 기존 프로세스 종료
    if old_pid:
        try:
            os.kill(old_pid, signal.SIGTERM)
            import time
            time.sleep(1)
            if get_bot_pid():
                os.kill(old_pid, signal.SIGKILL)
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)})
    
    # 새 프로세스 시작
    try:
        cmd = f"cd {BASE_DIR} && source venv/bin/activate && nohup python run_bot.py >> database/bot_stdout.log 2>&1 &"
        subprocess.Popen(cmd, shell=True, executable="/bin/bash")
        return JSONResponse({"success": True, "message": "Bot restarted"})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})

# 서버 실행 (uvicorn 직접 호출 시)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8501)
