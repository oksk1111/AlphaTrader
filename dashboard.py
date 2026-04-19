import streamlit as st
import pandas as pd
import os
import glob
import re
import time
import subprocess
import signal
import json
from datetime import datetime, timedelta
from modules.kis_api import KisOverseas
from modules.kis_domestic import KisDomestic
from modules.profit_tracker import (
    take_asset_snapshot, get_monthly_summary, get_asset_history,
    fetch_all_trades, fetch_kr_realized_profit, fetch_us_realized_profit
)
from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET

# ===== Google OAuth 인증 =====
def check_google_auth():
    """Google OAuth 로그인 확인. 설정되지 않았으면 건너뜁니다."""
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        # OAuth 미설정 시 인증 없이 진행 (개발/로컬 모드)
        return {"authenticated": True, "email": "local@dev", "name": "Local User"}
    
    # 세션 상태에서 인증 확인
    if "google_auth" not in st.session_state:
        st.session_state["google_auth"] = None
    
    if st.session_state["google_auth"]:
        return st.session_state["google_auth"]
    
    # Google OAuth 로그인 UI
    st.markdown("## 🔐 로그인이 필요합니다")
    st.markdown("Google 계정으로 로그인하세요.")
    st.markdown(f"""
    <a href="https://accounts.google.com/o/oauth2/v2/auth?client_id={GOOGLE_CLIENT_ID}&redirect_uri=http://localhost:8501&response_type=code&scope=email%20profile" target="_self">
        <button style="background-color:#4285F4;color:white;padding:12px 24px;border:none;border-radius:4px;font-size:16px;cursor:pointer;">
            🔑 Google 로그인
        </button>
    </a>
    """, unsafe_allow_html=True)
    
    # URL에서 인증 코드 확인 (OAuth redirect 후)
    query_params = st.query_params
    auth_code = query_params.get("code", None)
    
    if auth_code:
        try:
            import requests as req
            # 인증 코드로 토큰 교환
            token_resp = req.post("https://oauth2.googleapis.com/token", data={
                "code": auth_code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": "http://localhost:8501",
                "grant_type": "authorization_code"
            })
            token_data = token_resp.json()
            
            if "access_token" in token_data:
                # 사용자 정보 조회
                user_resp = req.get("https://www.googleapis.com/oauth2/v2/userinfo",
                    headers={"Authorization": f"Bearer {token_data['access_token']}"})
                user_info = user_resp.json()
                
                auth_result = {
                    "authenticated": True,
                    "email": user_info.get("email", "unknown"),
                    "name": user_info.get("name", "User"),
                    "picture": user_info.get("picture", "")
                }
                st.session_state["google_auth"] = auth_result
                st.query_params.clear()
                st.rerun()
        except Exception as e:
            st.error(f"인증 실패: {e}")
    
    return None  # 인증 안됨

# OAuth 체크
auth = check_google_auth()
if not auth:
    st.stop()  # 인증 안되면 여기서 멈춤

# Load Config
CONFIG_FILE = "user_config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {"trading_mode": "safe", "strategy": "day"}

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

st.set_page_config(
    page_title="US-ETF-Sniper Dashboard",
    page_icon="📈",
    layout="wide",
)

st.title("📈 US-ETF-Sniper Dashboard")

# --- Initialize API ---
@st.cache_resource
def get_kis_client_us():
    return KisOverseas()

@st.cache_resource
def get_kis_client_kr():
    return KisDomestic()

try:
    kis_us = get_kis_client_us()
    kis_kr = get_kis_client_kr()
    api_status = "🟢 API Connected (US & KR)"
except Exception as e:
    kis_us = None
    kis_kr = None
    api_status = f"🔴 API Error: {str(e)}"

# --- Sidebar ---
st.sidebar.header("Settings")

# User Info (Google OAuth)
if auth.get("name") and auth["name"] != "Local User":
    st.sidebar.markdown(f"👤 **{auth['name']}**")
    st.sidebar.markdown(f"📧 {auth['email']}")
    if st.sidebar.button("🚪 로그아웃"):
        st.session_state["google_auth"] = None
        st.rerun()
    st.sidebar.divider()

# 1. Config Management
config = load_config()

# --- 자동 전략 토글 ---
is_auto_strategy = st.sidebar.toggle(
    "🤖 자동 전략 (Auto Strategy)", 
    value=config.get("auto_strategy", False),
    help="시장 상황에 따라 전략/모드/페르소나를 자동으로 최적화합니다"
)

if is_auto_strategy != config.get("auto_strategy", False):
    config["auto_strategy"] = is_auto_strategy
    save_config(config)
    st.sidebar.success("Auto Strategy " + ("ON ✅" if is_auto_strategy else "OFF"))

if is_auto_strategy:
    # 자동 모드: 현재 AI가 선택한 설정 표시 (읽기 전용)
    st.sidebar.info(
        f"📊 현재 자동 설정\n"
        f"- 모드: **{config.get('trading_mode', 'safe').upper()}**\n"
        f"- 전략: **{config.get('strategy', 'dca').upper()}**\n"
        f"- 페르소나: **{config.get('persona', 'neutral')}**"
    )
    
    # 전략 히스토리 표시
    try:
        history_file = "database/strategy_history.json"
        if os.path.exists(history_file):
            import json as _json
            with open(history_file, "r") as f:
                history = _json.load(f)
            recent = history.get("changes", [])[-3:]  # 최근 3건
            if recent:
                st.sidebar.markdown("**📋 최근 전략 변경:**")
                for ch in reversed(recent):
                    ts = ch.get('timestamp', '')[:16]
                    new = ch.get('new', {})
                    st.sidebar.caption(
                        f"📌 {ts} | {new.get('strategy','?').upper()} / "
                        f"{new.get('mode','?')} / {new.get('persona','?')}"
                    )
    except:
        pass
    
    # 자동 모드에서도 수동으로 끄려면 위 토글만 끄면 됨
    new_trading_mode = config.get("trading_mode", "safe")
    new_strategy_mode = config.get("strategy", "dca")
else:
    # 수동 모드: 기존 selectbox 유지
    new_trading_mode = st.sidebar.selectbox(
        "Target Mode (ETF Type)", 
        ["safe", "risky"], 
        index=0 if config.get("trading_mode") == "safe" else 1,
        format_func=lambda x: "Safe Mode (1x Stock/ETF)" if x == "safe" else "Risky Mode (3x Lev ETF)"
    )

    new_strategy_mode = st.sidebar.selectbox(
        "Strategy (Exit Rule)",
        ["day", "swing", "dca"],
        index=["day", "swing", "dca"].index(config.get("strategy", "day")) if config.get("strategy", "day") in ["day", "swing", "dca"] else 0,
        format_func=lambda x: {"day": "Day Trading (Sell at Close)", "swing": "Swing/Hold (Trend Following)", "dca": "DCA (분할매수)"}[x]
    )

    if new_trading_mode != config.get("trading_mode") or new_strategy_mode != config.get("strategy"):
        config["trading_mode"] = new_trading_mode
        config["strategy"] = new_strategy_mode
        save_config(config)
        st.sidebar.success("Settings Saved! Restart Bot to Apply.")
    
refresh_rate = st.sidebar.slider("Refresh Rate (sec)", 1, 60, 5)
auto_refresh = st.sidebar.checkbox("Auto Refresh", value=True)
st.sidebar.markdown(f"**API Status**: {api_status}")

# --- Helper Functions ---
def get_bot_pid():
    """Find the process ID of run_bot.py"""
    try:
        # pgrep -f run_bot.py checks for running python process
        pid_output = subprocess.check_output(["pgrep", "-f", "run_bot.py"]).strip()
        # Handle multiple PIDs - get the first one
        if isinstance(pid_output, bytes):
            pid_output = pid_output.decode('utf-8')
        
        pids = pid_output.strip().split('\n')
        if pids and pids[0]:
            return int(pids[0])
        return None
    except (subprocess.CalledProcessError, ValueError, IndexError):
        return None

def restart_bot_process():
    """Kills existing bot and starts a new one"""
    # 1. Kill Check
    old_pid = get_bot_pid()
    if old_pid:
        try:
            os.kill(old_pid, signal.SIGTERM)
            time.sleep(1)
            # Force kill if still alive
            if get_bot_pid():
               os.kill(old_pid, signal.SIGKILL) 
        except Exception as e:
            st.error(f"Failed to stop bot: {e}")
            return False
            
    # 2. Start New
    try:
        # Using nohup to keep it alive independent of this script
        # We need to use full path or ensure cwd is correct
        cmd = "nohup python3 run_bot.py > nohup.out 2>&1 &"
        subprocess.Popen(cmd, shell=True, cwd=os.getcwd())
        return True
    except Exception as e:
        st.error(f"Failed to start bot: {e}")
        return False

def get_recent_log_files(limit=3):
    log_files = glob.glob("database/trading_*.log")
    if not log_files:
        return []
    # Sort by filename (date) ascending, take last N
    return sorted(log_files)[-limit:]

def parse_log_line(line):
    # Simple parser to extract timestamp and message
    # Format: 2025-12-31 02:48:44,165 - INFO - Message
    match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} - (\w+) - (.*)", line)
    if match:
        return {
            "timestamp": match.group(1),
            "level": match.group(2),
            "message": match.group(3)
        }
    return None

def get_bot_status(last_log_time_str):
    if not last_log_time_str:
        return "Unknown"
    
    try:
        last_time = pd.to_datetime(last_log_time_str)
        now = pd.Timestamp.now()
        diff = (now - last_time).total_seconds()
        
        if diff < 300: # 5 minutes
            return "🟢 Running"
        else:
            return "🔴 Stopped / Idle"
    except:
        return "Unknown"

# --- Main Content ---
tab1, tab2, tab3, tab4, tab5 = st.tabs(["📊 Overview", "💰 Account & Portfolio", "💹 Performance", "📜 Logs & History", "📈 Analytics"])

recent_files = get_recent_log_files(3)
parsed_lines = []

for log_file in recent_files:
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        try:
            with open(log_file, "r", encoding="cp949") as f:
                lines = f.readlines()
        except:
            continue
            
    # Parse and append
    file_parsed = [parse_log_line(line) for line in lines]
    parsed_lines.extend([x for x in file_parsed if x is not None])

# --- Tab 1: Overview ---
with tab1:
    # --- Service Control Section ---
    st.subheader("🤖 Bot Control")
    col_status, col_btn = st.columns([2, 1])
    
    current_pid = get_bot_pid()
    status_text = f"🟢 Running (PID: {current_pid})" if current_pid else "🔴 Stopped"
    
    with col_status:
        st.info(f"**Service Status:** {status_text}")
        
    with col_btn:
        if st.button("🔄 Restart Service", type="primary"):
            with st.spinner("Restarting Bot Service..."):
                if restart_bot_process():
                    st.success("Service Restarted Successfully!")
                    time.sleep(2)
                    st.rerun()
                else:
                    st.error("Failed to restart service.")
    
    if parsed_lines:
        # 1. Status
        last_log = parsed_lines[-1]
        status = get_bot_status(last_log['timestamp'])
        
        st.metric("Bot Status", status)
        st.markdown(f"Last Update: `{last_log['timestamp']}`")
        
        # === Multi-LLM 합의 상태 ===
        llm_logs = [l for l in parsed_lines if '[MultiLLM]' in l['message']]
        if llm_logs:
            st.subheader("🧠 AI Multi-LLM 합의")
            last_consensus = None
            for l in reversed(llm_logs):
                if '합의 결과' in l['message']:
                    last_consensus = l
                    break
            if last_consensus:
                st.info(f"최근 판단: {last_consensus['message']}")
            
            # 활성 LLM 표시
            active_llm_log = [l for l in llm_logs if '활성 LLM' in l['message']]
            if active_llm_log:
                st.caption(active_llm_log[-1]['message'])
        
        # 2. Key Metrics by Ticker
        # We will scan lines to find the LATEST info for each ticker
        ticker_data = {}
        
        # Regex patterns
        # Log format: "[TQQQ] Current: 54.38, MA20: 54.31"
        # Log format: "[TQQQ] Bull Market! Target Price: 55.12 (Open: 54.0)"
        
        for line in parsed_lines:
            msg = line['message']
            
            # Extract Ticker from [TICKER]
            # Updated regex to support both US (Alphabets) and KR (Numbers) tickers
            ticker_match = re.search(r"\[([A-Z0-9]+)\]", msg)
            if not ticker_match:
                continue
            
            ticker = ticker_match.group(1)
            if ticker not in ticker_data:
                ticker_data[ticker] = {"Current": "N/A", "MA20": "N/A", "Target": "N/A", "Trend": "Unknown"}
            
            # Parse Current & MA20
            # [TQQQ] Current: 54.3839, MA20: 54.3137
            m1 = re.search(r"Current: ([^,]+), MA20: (.+)", msg)
            if m1:
                ticker_data[ticker]["Current"] = m1.group(1).strip()
                ticker_data[ticker]["MA20"] = m1.group(2).strip()
                # If we see Current/MA20, we don't know trend yet unless explicit
            
            # Parse Target Price
            # [TQQQ] Bull Market! Target Price: 55.12 ...
            m2 = re.search(r"Target Price: ([^ ]+)", msg)
            if m2:
                ticker_data[ticker]["Target"] = m2.group(1).strip()
                ticker_data[ticker]["Trend"] = "Bull 🐂"
            
            # Parse Bear Market
            if "Bear Market" in msg:
                ticker_data[ticker]["Trend"] = "Bear 🐻"
                ticker_data[ticker]["Target"] = "-"

            # Parse Stopped/Failed Status
            if "STOPPING" in msg or "Account Restriction" in msg:
                ticker_data[ticker]["Trend"] = "Stopped ⛔"
                ticker_data[ticker]["Price"] = "Error"

        # Convert to DataFrame for nice display
        if ticker_data:
            st.subheader("📡 Market Monitor")
            data_list = []
            for t, d in ticker_data.items():
                data_list.append({
                    "Ticker": t,
                    "Price": d["Current"],
                    "Target Price": d["Target"],
                    "20 MA": d["MA20"],
                    "Trend": d["Trend"]
                })
            
            df_monitor = pd.DataFrame(data_list)
            st.dataframe(
                df_monitor, 
                column_config={
                    "Ticker": st.column_config.TextColumn("Ticker", width="small"),
                    "Trend": st.column_config.TextColumn("Trend", width="small"),
                },
                hide_index=True,
                use_container_width=True
            )
        else:
            st.info("Waiting for ticker analysis logs...")

    else:
        st.warning("No logs found. Is the bot running?")

# --- Tab 2: Account ---
with tab2:
    if st.button("Refresh Account Info"):
        # Just to trigger rerun
        pass

    # --- US Account ---
    st.subheader("🇺🇸 US Account")
    if kis_us:
        balance_us = kis_us.get_balance()
        foreign_balance = kis_us.get_foreign_balance()
        
        # --- Prepare Data ---
        deposit_usd = "N/A"
        if foreign_balance:
            if 'debug_raw' in foreign_balance:
                st.error(f"⚠️ USD Balance not found! Raw API Data: {foreign_balance['debug_raw']}")
            else:
                deposit_usd = str(foreign_balance['deposit'])
        
        buying_power = "N/A"
        total_profit = "0"
        total_return = "0"
        
        has_stock_balance = False
        if balance_us and 'output2' in balance_us and isinstance(balance_us['output2'], list) and len(balance_us['output2']) > 0:
            has_stock_balance = True
            summary = balance_us['output2'][0]
            buying_power = summary.get('ovrs_ord_psbl_amt', '0')
            total_profit = summary.get('tot_evlu_pfls_amt', '0')
            total_return = summary.get('ovrs_tot_pfls', '0')
            
            # Fallback buying power logic: if 0, use deposit
            if float(buying_power) == 0 and deposit_usd != "N/A":
                buying_power = deposit_usd + " (Est)"
        elif deposit_usd != "N/A":
             buying_power = deposit_usd + " (Est)"

        # --- Display Asset Status ---
        ac_col1, ac_col2, ac_col3, ac_col4 = st.columns(4)
        ac_col1.metric("Cash (USD)", f"${deposit_usd}")
        ac_col2.metric("Buying Power (USD)", f"${buying_power}")
        ac_col3.metric("Total Profit", f"${total_profit}")
        ac_col4.metric("Total Return", f"{total_return}%")

        # --- Display Holdings ---
        if balance_us and 'output1' in balance_us and balance_us['output1']:
            st.write("Current Holdings (US)")
            holdings = balance_us['output1']
            df_h = pd.DataFrame(holdings)
            
            # Column Mapping
            col_map = {
                'pdno': 'Ticker',
                'prdt_name': 'Name',
                'ccld_qty_smtl1': 'Qty',
                'frcr_pchs_amt1': 'Avg Price',
                'ovrs_now_pric1': 'Cur Price',
                'evlu_pfls_rt': 'Return(%)',
                'evlu_pfls_amt': 'Profit($)'
            }
            
            # Filter & Rename
            valid_cols = [c for c in col_map.keys() if c in df_h.columns]
            df_h = df_h[valid_cols].rename(columns=col_map)
            st.dataframe(df_h)
            
    st.divider()

    # --- KR Account ---
    st.subheader("🇰🇷 KR Account")
    if kis_kr:
        balance_kr = kis_kr.get_balance()
        
        if balance_kr and 'output2' in balance_kr:
            # KR Balance Summary
            # output2[0] contains summary
            summary_kr = balance_kr['output2'][0]
            
            deposit_krw = summary_kr.get('dnca_tot_amt', '0') # 예수금총금액
            profit_krw = summary_kr.get('evlu_pfls_smtl_amt', '0') # 평가손익합계금액
            total_asset_krw = summary_kr.get('tot_evlu_amt', '0') # 총평가금액
            
            k_col1, k_col2, k_col3 = st.columns(3)
            k_col1.metric("Deposit (KRW)", f"{int(deposit_krw):,}")
            k_col2.metric("Total Profit (KRW)", f"{int(profit_krw):,}")
            k_col3.metric("Total Asset (KRW)", f"{int(total_asset_krw):,}")
            
        if balance_kr and 'output1' in balance_kr and balance_kr['output1']:
            st.write("Current Holdings (KR)")
            holdings_kr = balance_kr['output1']
            df_k = pd.DataFrame(holdings_kr)
            
            # Column Mapping for KR
            col_map_kr = {
                'pdno': 'Ticker',
                'prdt_name': 'Name',
                'hldg_qty': 'Qty',
                'pchs_avg_pric': 'Avg Price',
                'prpr': 'Cur Price',
                'evlu_pfls_rt': 'Return(%)',
                'evlu_pfls_amt': 'Profit(KRW)'
            }
            
            valid_cols_kr = [c for c in col_map_kr.keys() if c in df_k.columns]
            df_k = df_k[valid_cols_kr].rename(columns=col_map_kr)
            st.dataframe(df_k)
        else:
            st.info("No KR Holdings found.")
    else:
        st.warning("KR API not connected.")

# --- Tab 3: Performance (수익 현황) ---
with tab3:
    st.subheader("💹 수익 현황 (Performance)")
    st.caption("실현손익(매도 완료) + 평가손익(보유 중)을 통합하여 실제 수익률을 표시합니다.")
    
    # --- 자산 스냅샷 ---
    if kis_kr and kis_us:
        # 스냅샷 저장 버튼
        snap_col1, snap_col2 = st.columns([1, 3])
        with snap_col1:
            if st.button("📸 자산 스냅샷 저장", help="현재 자산 현황을 기록합니다. 일 1회 권장."):
                with st.spinner("자산 현황 조회 중..."):
                    snap = take_asset_snapshot(kis_kr, kis_us)
                    if snap:
                        st.success(f"스냅샷 저장 완료! 총자산: {snap.get('total_krw', 0):,}원")
        
        # --- 현재 수익 현황 (실시간) ---
        st.subheader("📊 현재 수익 현황")
        
        # KR 현재 상태
        kr_unrealized = 0
        kr_deposit = 0
        kr_total_asset = 0
        try:
            kr_bal = kis_kr.get_balance()
            if kr_bal and kr_bal.get('rt_cd') == '0' and 'output2' in kr_bal and kr_bal['output2']:
                kr_summary = kr_bal['output2'][0]
                kr_deposit = int(kr_summary.get('dnca_tot_amt', '0') or '0')
                kr_unrealized = int(kr_summary.get('evlu_pfls_smtl_amt', '0') or '0')
                kr_total_asset = int(kr_summary.get('tot_evlu_amt', '0') or '0')
        except Exception as e:
            st.error(f"KR 잔고 조회 실패: {e}")
        
        # US 현재 상태
        us_unrealized = 0.0
        us_deposit = 0.0
        us_total_asset = 0.0
        try:
            us_fb = kis_us.get_foreign_balance()
            if us_fb and 'deposit' in us_fb:
                us_deposit = float(us_fb['deposit'])
            
            us_bal = kis_us.get_balance()
            if us_bal and 'output1' in us_bal:
                for h in us_bal['output1']:
                    qty = int(float(h.get('ovrs_cblc_qty', h.get('ord_psbl_qty', '0')) or '0'))
                    if qty <= 0:
                        continue
                    us_unrealized += float(h.get('frcr_evlu_pfls_amt', h.get('evlu_pfls_amt', '0')) or '0')
                    us_total_asset += float(h.get('ovrs_stck_evlu_amt', h.get('frcr_evlu_amt', '0')) or '0')
            us_total_asset += us_deposit
        except Exception as e:
            st.error(f"US 잔고 조회 실패: {e}")
        
        # 평가손익 표시
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("##### 🇰🇷 KR 시장")
            kc1, kc2, kc3 = st.columns(3)
            kc1.metric("예수금", f"{kr_deposit:,}원")
            kr_profit_color = "normal" if kr_unrealized >= 0 else "inverse"
            kc2.metric("평가손익 (보유중)", f"{kr_unrealized:,}원", 
                       delta=f"{kr_unrealized:,}원", delta_color=kr_profit_color)
            kc3.metric("총 자산", f"{kr_total_asset:,}원")
        
        with col2:
            st.markdown("##### 🇺🇸 US 시장")
            uc1, uc2, uc3 = st.columns(3)
            uc1.metric("예수금", f"${us_deposit:,.2f}")
            us_profit_color = "normal" if us_unrealized >= 0 else "inverse"
            uc2.metric("평가손익 (보유중)", f"${us_unrealized:,.2f}", 
                       delta=f"${us_unrealized:,.2f}", delta_color=us_profit_color)
            uc3.metric("총 자산", f"${us_total_asset:,.2f}")
        
        st.divider()
        
        # --- 체결 내역 (실현손익 추적) ---
        st.subheader("📋 체결 내역 (Trade History)")
        
        period_col1, period_col2 = st.columns(2)
        with period_col1:
            period_option = st.selectbox("조회 기간", [
                "최근 7일", "최근 30일", "최근 90일", "이번 달", "지난 달", "직접 입력"
            ])
        
        today = datetime.now()
        
        if period_option == "최근 7일":
            start_dt = today - timedelta(days=7)
            end_dt = today
        elif period_option == "최근 30일":
            start_dt = today - timedelta(days=30)
            end_dt = today
        elif period_option == "최근 90일":
            start_dt = today - timedelta(days=90)
            end_dt = today
        elif period_option == "이번 달":
            start_dt = today.replace(day=1)
            end_dt = today
        elif period_option == "지난 달":
            first_this_month = today.replace(day=1)
            end_dt = first_this_month - timedelta(days=1)
            start_dt = end_dt.replace(day=1)
        else:
            with period_col2:
                date_range = st.date_input("기간 선택", 
                    value=(today - timedelta(days=30), today),
                    max_value=today)
                if isinstance(date_range, tuple) and len(date_range) == 2:
                    start_dt, end_dt = date_range[0], date_range[1]
                else:
                    start_dt = today - timedelta(days=30)
                    end_dt = today
        
        start_str = start_dt.strftime("%Y%m%d") if hasattr(start_dt, 'strftime') else start_dt.strftime("%Y%m%d")
        end_str = end_dt.strftime("%Y%m%d") if hasattr(end_dt, 'strftime') else end_dt.strftime("%Y%m%d")
        
        if st.button("🔍 체결 내역 조회"):
            with st.spinner(f"체결 내역 조회 중... ({start_str} ~ {end_str})"):
                trades_data = fetch_all_trades(kis_kr, kis_us, start_str, end_str)
                
                # KR 체결 내역
                if trades_data["kr_trades"]:
                    st.markdown("##### 🇰🇷 KR 체결 내역")
                    df_kr_trades = pd.DataFrame(trades_data["kr_trades"])
                    df_kr_trades.columns = ["날짜", "종목코드", "종목명", "매매", "수량", "체결가", "체결금액"]
                    
                    # 매도/매수 색상 구분
                    sell_count = len(df_kr_trades[df_kr_trades["매매"] == "매도"])
                    buy_count = len(df_kr_trades[df_kr_trades["매매"] == "매수"])
                    st.caption(f"매수 {buy_count}건 / 매도 {sell_count}건")
                    st.dataframe(df_kr_trades, hide_index=True, use_container_width=True)
                else:
                    st.info("KR 체결 내역이 없습니다.")
                
                # US 체결 내역
                if trades_data["us_trades"]:
                    st.markdown("##### 🇺🇸 US 체결 내역")
                    df_us_trades = pd.DataFrame(trades_data["us_trades"])
                    df_us_trades.columns = ["날짜", "종목코드", "종목명", "매매", "수량", "체결가($)", "체결금액($)"]
                    
                    sell_count = len(df_us_trades[df_us_trades["매매"] == "매도"])
                    buy_count = len(df_us_trades[df_us_trades["매매"] == "매수"])
                    st.caption(f"매수 {buy_count}건 / 매도 {sell_count}건")
                    st.dataframe(df_us_trades, hide_index=True, use_container_width=True)
                else:
                    st.info("US 체결 내역이 없습니다.")
                
                if not trades_data["kr_trades"] and not trades_data["us_trades"]:
                    st.warning("해당 기간에 체결 내역이 없습니다.")
        
        st.divider()
        
        # --- 월별 자산 추이 (스냅샷 기반) ---
        st.subheader("📈 자산 추이 (Asset Trend)")
        
        history = get_asset_history()
        if history:
            df_history = pd.DataFrame(history)
            df_history['date'] = pd.to_datetime(df_history['date'])
            
            # 총 자산 추이 차트
            st.markdown("##### 총 자산 추이 (KRW 환산)")
            st.line_chart(df_history.set_index('date')['total_krw'], use_container_width=True)
            
            # KR/US 손익 추이
            col_chart1, col_chart2 = st.columns(2)
            with col_chart1:
                st.markdown("##### 🇰🇷 KR 평가손익")
                st.bar_chart(df_history.set_index('date')['kr_profit'], use_container_width=True)
            with col_chart2:
                st.markdown("##### 🇺🇸 US 평가손익 (USD)")
                st.bar_chart(df_history.set_index('date')['us_profit_usd'], use_container_width=True)
            
            # 월별 요약 테이블
            monthly = get_monthly_summary()
            if monthly:
                st.markdown("##### 월별 요약")
                df_monthly = pd.DataFrame(monthly)
                display_cols = {
                    "month": "월",
                    "kr_deposit": "KR 예수금",
                    "kr_eval_profit": "KR 평가손익",
                    "us_deposit_usd": "US 예수금($)",
                    "us_eval_profit_usd": "US 평가손익($)",
                    "total_krw": "총자산(KRW)"
                }
                df_display = df_monthly[[c for c in display_cols.keys() if c in df_monthly.columns]]
                df_display = df_display.rename(columns=display_cols)
                st.dataframe(df_display, hide_index=True, use_container_width=True)
        else:
            st.info("📸 아직 자산 스냅샷이 없습니다. 위의 '자산 스냅샷 저장' 버튼을 눌러 기록을 시작하세요.")
            st.caption("매일 1회 스냅샷을 저장하면 자산 추이 차트가 표시됩니다.")
    else:
        st.warning("API가 연결되지 않았습니다.")

# --- Tab 4: Logs ---
with tab4:
    st.subheader("Recent Trades")
    if parsed_lines:
        trades = []
        for line in parsed_lines:
            msg = line['message']
            if any(k in msg for k in ["Buy Order", "Sell Order", "Selling All", "Stop Loss"]):
                trades.append(line)
        
        if trades:
            st.dataframe(pd.DataFrame(trades)[['timestamp', 'message']], hide_index=True)
        else:
            st.info("No trade events found in logs.")
    
    st.subheader("System Logs")
    if parsed_lines:
        df = pd.DataFrame(parsed_lines)
        st.dataframe(df.iloc[::-1], hide_index=True) # Show newest first

# --- Tab 5: Analytics ---
with tab5:
    st.subheader("📊 Log Analytics")
    
    # Get all log files
    all_log_files = sorted(glob.glob("database/trading_*.log"))
    
    if all_log_files:
        # Log file selector
        selected_logs = st.multiselect(
            "Select Log Files to Analyze",
            all_log_files,
            default=all_log_files[-3:] if len(all_log_files) >= 3 else all_log_files
        )
        
        # Parse all selected logs
        all_parsed = []
        for lf in selected_logs:
            try:
                with open(lf, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except UnicodeDecodeError:
                with open(lf, "r", encoding="cp949") as f:
                    lines = f.readlines()
            
            for line in lines:
                parsed = parse_log_line(line)
                if parsed:
                    parsed['file'] = os.path.basename(lf)
                    all_parsed.append(parsed)
        
        if all_parsed:
            df_all = pd.DataFrame(all_parsed)
            
            # --- Error Summary ---
            st.subheader("⚠️ Error Summary")
            errors = df_all[df_all['level'].isin(['ERROR', 'CRITICAL'])]
            
            if not errors.empty:
                # Count errors by type
                error_patterns = {
                    'APBK1680 (ETF Education)': 'APBK1680',
                    'APBK1681 (Deposit Req)': 'APBK1681',
                    'Token Error': 'Token',
                    'API Error': 'API Error|Request Exception',
                    'OHLC Failed': 'Failed to get OHLC',
                    'Buy Failed': 'Buy Failed',
                    'Other Errors': ''
                }
                
                error_counts = {}
                for name, pattern in error_patterns.items():
                    if pattern:
                        count = errors[errors['message'].str.contains(pattern, na=False, regex=True)].shape[0]
                    else:
                        count = 0
                    error_counts[name] = count
                
                # Display error metrics
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Total Errors", len(errors))
                col2.metric("CRITICAL", len(errors[errors['level'] == 'CRITICAL']))
                col3.metric("Account Errors", error_counts['APBK1680 (ETF Education)'] + error_counts['APBK1681 (Deposit Req)'])
                col4.metric("API Errors", error_counts['Token Error'] + error_counts['API Error'])
                
                # Show error details
                with st.expander("View Error Details"):
                    st.dataframe(errors[['timestamp', 'file', 'message']].iloc[::-1], hide_index=True)
            else:
                st.success("✅ No errors found in selected logs!")
            
            # --- Trade Statistics ---
            st.subheader("📈 Trade Statistics")
            
            # Count trades
            buy_success = df_all[df_all['message'].str.contains('Buy Success', na=False)]
            buy_failed = df_all[df_all['message'].str.contains('Buy Failed', na=False)]
            sell_orders = df_all[df_all['message'].str.contains('Selling|Sell Order', na=False, regex=True)]
            
            col1, col2, col3 = st.columns(3)
            col1.metric("Successful Buys", len(buy_success))
            col2.metric("Failed Buys", len(buy_failed))
            col3.metric("Sell Orders", len(sell_orders))
            
            # --- Ticker Analysis ---
            st.subheader("🏷️ Ticker Analysis")
            
            # Extract tickers from logs
            ticker_pattern = r"\[([A-Z0-9]+)\]"
            df_all['ticker'] = df_all['message'].str.extract(ticker_pattern)
            
            ticker_stats = []
            for ticker in df_all['ticker'].dropna().unique():
                ticker_logs = df_all[df_all['ticker'] == ticker]
                ticker_stats.append({
                    'Ticker': ticker,
                    'Breakouts': ticker_logs[ticker_logs['message'].str.contains('Breakout', na=False)].shape[0],
                    'Buy Success': ticker_logs[ticker_logs['message'].str.contains('Buy Success', na=False)].shape[0],
                    'Buy Failed': ticker_logs[ticker_logs['message'].str.contains('Buy Failed', na=False)].shape[0],
                    'Trend': 'Bull 🐂' if ticker_logs[ticker_logs['message'].str.contains('Bull Market', na=False)].shape[0] > 0 else 'Bear 🐻'
                })
            
            if ticker_stats:
                df_ticker = pd.DataFrame(ticker_stats)
                df_ticker = df_ticker.sort_values('Breakouts', ascending=False)
                st.dataframe(df_ticker, hide_index=True, use_container_width=True)
            
            # --- Session Summary ---
            st.subheader("📅 Session Summary")
            sessions = df_all[df_all['message'].str.contains('Starting.*Trading Session', na=False, regex=True)]
            if not sessions.empty:
                st.write(f"Total Sessions: {len(sessions)}")
                for _, row in sessions.iterrows():
                    market = 'US 🇺🇸' if 'US' in row['message'] else 'KR 🇰🇷'
                    st.text(f"  • {row['timestamp']} - {market}")
    else:
        st.warning("No log files found.")

# === 광고 영역 (Ad Revenue) ===
st.divider()
st.markdown("### 📢 Sponsored")

ad_col1, ad_col2, ad_col3 = st.columns(3)

with ad_col1:
    st.markdown("""
    <div style="border:1px solid #ddd;padding:16px;border-radius:8px;text-align:center;min-height:120px;background:#f9f9f9;">
        <p style="color:#888;font-size:12px;">광고 영역 1</p>
        <p style="color:#aaa;font-size:10px;">Google AdSense / 제휴 광고</p>
        <!-- AD_SLOT_1: Google AdSense 코드 삽입 위치 -->
    </div>
    """, unsafe_allow_html=True)

with ad_col2:
    st.markdown("""
    <div style="border:1px solid #ddd;padding:16px;border-radius:8px;text-align:center;min-height:120px;background:#f9f9f9;">
        <p style="color:#888;font-size:12px;">광고 영역 2</p>
        <p style="color:#aaa;font-size:10px;">증권사 제휴 / 투자 교육</p>
        <!-- AD_SLOT_2: 증권사 제휴 배너 위치 -->
    </div>
    """, unsafe_allow_html=True)

with ad_col3:
    st.markdown("""
    <div style="border:1px solid #ddd;padding:16px;border-radius:8px;text-align:center;min-height:120px;background:#f9f9f9;">
        <p style="color:#888;font-size:12px;">광고 영역 3</p>
        <p style="color:#aaa;font-size:10px;">프리미엄 기능 안내</p>
        <!-- AD_SLOT_3: 프리미엄 업그레이드 안내 위치 -->
    </div>
    """, unsafe_allow_html=True)

st.caption("투자는 무료입니다. 광고 수익으로 서비스를 운영합니다.")

# Auto Refresh logic
if auto_refresh:
    time.sleep(refresh_rate)
    st.rerun()
