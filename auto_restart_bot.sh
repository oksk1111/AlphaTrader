#!/bin/bash

# ==============================================
# US-ETF-Sniper Auto Restart Script
# Monitors and auto-restarts both bot and dashboard
# Only activates during market hours (weekdays)
# ==============================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LOG_DIR="$SCRIPT_DIR/database"
RESTART_LOG="$LOG_DIR/restart.log"

# Ensure log directory exists
mkdir -p "$LOG_DIR"

log_message() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$RESTART_LOG"
}

is_market_hours() {
    # Get current day of week (1=Monday, 7=Sunday)
    local day_of_week=$(date +%u)
    
    # Weekend check (Saturday=6, Sunday=7)
    if [ "$day_of_week" -ge 6 ]; then
        return 1  # false - weekend
    fi
    
    # Get current time in HHMM format
    local current_time=$(date +%H%M)
    
    # KR Market: 08:50 ~ 15:30 (buffer included)
    # US Market: 23:20 ~ 06:10 (buffer included)
    
    # Check if within KR market hours
    if [ "$current_time" -ge 850 ] && [ "$current_time" -le 1530 ]; then
        return 0  # true - KR market hours
    fi
    
    # Check if within US market hours (spans midnight)
    if [ "$current_time" -ge 2320 ] || [ "$current_time" -le 610 ]; then
        # Additional check: Friday night US session ends Saturday morning
        # So if it's Saturday morning (after midnight), check if we came from Friday
        if [ "$day_of_week" -eq 6 ] && [ "$current_time" -le 610 ]; then
            return 0  # true - still Friday's US session
        elif [ "$day_of_week" -lt 6 ]; then
            return 0  # true - US market hours on weekday
        fi
    fi
    
    return 1  # false - outside market hours
}

get_market_status() {
    local day_of_week=$(date +%u)
    local current_time=$(date +%H%M)
    
    if [ "$day_of_week" -ge 6 ]; then
        if [ "$day_of_week" -eq 6 ] && [ "$current_time" -le 610 ]; then
            echo "US (Fri→Sat)"
        else
            echo "WEEKEND"
        fi
    elif [ "$current_time" -ge 850 ] && [ "$current_time" -le 1530 ]; then
        echo "KR"
    elif [ "$current_time" -ge 2320 ] || [ "$current_time" -le 610 ]; then
        echo "US"
    else
        echo "CLOSED"
    fi
}

check_and_restart_bot() {
    if ! pgrep -f "python.*run_bot.py" > /dev/null; then
        log_message "⚠️ Bot process not found. Restarting..."
        cd "$SCRIPT_DIR"
        nohup venv/bin/python run_bot.py >> "$LOG_DIR/bot_stdout.log" 2>&1 &
        sleep 2
        if pgrep -f "python.*run_bot.py" > /dev/null; then
            log_message "✅ Bot restarted successfully (PID: $(pgrep -f 'python.*run_bot.py'))"
        else
            log_message "❌ Failed to restart bot!"
        fi
    fi
}

check_and_restart_dashboard() {
    if ! pgrep -f "streamlit.*dashboard.py" > /dev/null; then
        log_message "⚠️ Dashboard process not found. Restarting..."
        cd "$SCRIPT_DIR"
        nohup venv/bin/streamlit run dashboard.py --server.port 8501 --server.address 0.0.0.0 >> "$LOG_DIR/dashboard_stdout.log" 2>&1 &
        sleep 3
        if pgrep -f "streamlit.*dashboard.py" > /dev/null; then
            log_message "✅ Dashboard restarted successfully"
        else
            log_message "❌ Failed to restart dashboard!"
        fi
    fi
}

# Main monitoring loop
log_message "🚀 Starting Auto-Restart Monitor..."

LAST_STATUS=""

while true; do
    MARKET_STATUS=$(get_market_status)
    
    # 대시보드는 항상 유지 (모니터링 용도)
    check_and_restart_dashboard
    
    if is_market_hours; then
        # 장 운영 시간: 봇 재시작 활성화
        if [ "$LAST_STATUS" != "ACTIVE" ]; then
            log_message "📈 Market is OPEN ($MARKET_STATUS). Bot monitoring ACTIVE."
            LAST_STATUS="ACTIVE"
        fi
        check_and_restart_bot
        sleep 30  # Check every 30 seconds during market hours
    else
        # 장 운영 외 시간: 봇 재시작 비활성화 (토큰 발행 방지)
        if [ "$LAST_STATUS" != "IDLE" ]; then
            log_message "😴 Market is CLOSED ($MARKET_STATUS). Bot monitoring IDLE. Skipping token refresh."
            LAST_STATUS="IDLE"
        fi
        sleep 300  # Check every 5 minutes during off-hours
    fi
done
