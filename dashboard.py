import streamlit as st
import pandas as pd
import os
import glob
import re
import time
import subprocess
import signal
import json
from modules.kis_api import KisOverseas
from modules.kis_domestic import KisDomestic

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

# 1. Config Management
config = load_config()

new_trading_mode = st.sidebar.selectbox(
    "Target Mode (ETF Type)", 
    ["safe", "risky"], 
    index=0 if config.get("trading_mode") == "safe" else 1,
    format_func=lambda x: "Safe Mode (1x Stock/ETF)" if x == "safe" else "Risky Mode (3x Lev ETF)"
)

new_strategy_mode = st.sidebar.selectbox(
    "Strategy (Exit Rule)",
    ["day", "swing"],
    index=0 if config.get("strategy", "day") == "day" else 1,
    format_func=lambda x: "Day Trading (Sell at Close)" if x == "day" else "Swing/Hold (Trend Following)"
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
        pid = subprocess.check_output(["pgrep", "-f", "run_bot.py"]).strip()
        return int(pid)
    except subprocess.CalledProcessError:
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

def get_latest_log_file():
    log_files = glob.glob("database/trading_*.log")
    if not log_files:
        return None
    # Sort by filename (date) descending
    return sorted(log_files)[-1]

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
tab1, tab2, tab3, tab4 = st.tabs(["📊 Overview", "💰 Account & Portfolio", "📜 Logs & History", "📈 Analytics"])

log_file = get_latest_log_file()
parsed_lines = []

if log_file:
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        # Fallback to system encoding (cp949/euc-kr)
        with open(log_file, "r", encoding="cp949") as f:
            lines = f.readlines()
        
    parsed_lines = [parse_log_line(line) for line in lines]
    parsed_lines = [x for x in parsed_lines if x is not None]

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

# --- Tab 3: Logs ---
with tab3:
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

# --- Tab 4: Analytics ---
with tab4:
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

# Auto Refresh logic
if auto_refresh:
    time.sleep(refresh_rate)
    st.rerun()
