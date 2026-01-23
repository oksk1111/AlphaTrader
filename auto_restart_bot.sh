#!/bin/bash

# ==============================================
# US-ETF-Sniper Auto Restart Script
# Monitors and auto-restarts both bot and dashboard
# Only activates during market hours (weekdays)
# Includes Telegram notifications
# ==============================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LOG_DIR="$SCRIPT_DIR/database"
RESTART_LOG="$LOG_DIR/restart.log"
DAILY_REPORT_FLAG="$LOG_DIR/.daily_report_sent"

# Telegram configuration (set these or use environment variables)
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

# Bot restart failure tracking
BOT_RESTART_ATTEMPTS=0
MAX_RESTART_ATTEMPTS=3

# Ensure log directory exists
mkdir -p "$LOG_DIR"

log_message() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$RESTART_LOG"
}

# ==========================================
# Telegram Functions
# ==========================================

send_telegram() {
    local message="$1"
    
    if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then
        log_message "⚠️ Telegram not configured. Skipping notification."
        return 1
    fi
    
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        -d "text=${message}" \
        -d "parse_mode=HTML" > /dev/null 2>&1
    
    if [ $? -eq 0 ]; then
        log_message "📱 Telegram notification sent"
        return 0
    else
        log_message "❌ Failed to send Telegram notification"
        return 1
    fi
}

send_bot_failure_alert() {
    local attempts="$1"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    
    local message="🚨 <b>US-ETF-Sniper 긴급 알림</b>
━━━━━━━━━━━━━━━━━━━━
⏰ ${timestamp}

<b>❌ Bot 재시작 실패!</b>

├ 재시작 시도: ${attempts}회
├ 상태: 🔴 STOPPED
└ 즉시 확인이 필요합니다!

<b>조치 방법:</b>
1. SSH 접속: ssh user@158.180.81.25
2. 로그 확인: tail -f database/trading_*.log
3. 수동 시작: ./auto_restart_bot.sh

🔗 Dashboard: http://158.180.81.25:8501
━━━━━━━━━━━━━━━━━━━━"
    
    send_telegram "$message"
}

send_daily_report() {
    # Python 스크립트를 통해 상세 보고서 생성 및 발송
    cd "$SCRIPT_DIR"
    
    if [ -f "venv/bin/python" ]; then
        venv/bin/python -c "
from modules.telegram_notifier import TelegramNotifier
notifier = TelegramNotifier()
if notifier.is_configured():
    result = notifier.send_daily_report()
    exit(0 if result else 1)
else:
    print('Telegram not configured')
    exit(1)
" 2>/dev/null
        return $?
    else
        log_message "❌ Python venv not found for daily report"
        return 1
    fi
}

should_send_daily_report() {
    local current_hour=$(date +%H)
    local current_date=$(date +%Y%m%d)
    
    # 오후 4시(16시)에 일일 보고서 발송 (KR 장 마감 후)
    if [ "$current_hour" -eq 16 ]; then
        # 오늘 이미 보냈는지 확인
        if [ -f "$DAILY_REPORT_FLAG" ]; then
            local last_sent=$(cat "$DAILY_REPORT_FLAG")
            if [ "$last_sent" == "$current_date" ]; then
                return 1  # 이미 보냄
            fi
        fi
        return 0  # 보내야 함
    fi
    return 1  # 16시가 아님
}

mark_daily_report_sent() {
    echo "$(date +%Y%m%d)" > "$DAILY_REPORT_FLAG"
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
        log_message "⚠️ Bot process not found. Restarting... (attempt: $((BOT_RESTART_ATTEMPTS + 1)))"
        cd "$SCRIPT_DIR"
        nohup venv/bin/python run_bot.py >> "$LOG_DIR/bot_stdout.log" 2>&1 &
        sleep 3
        
        if pgrep -f "python.*run_bot.py" > /dev/null; then
            log_message "✅ Bot restarted successfully (PID: $(pgrep -f 'python.*run_bot.py'))"
            BOT_RESTART_ATTEMPTS=0  # 성공 시 카운터 리셋
        else
            BOT_RESTART_ATTEMPTS=$((BOT_RESTART_ATTEMPTS + 1))
            log_message "❌ Failed to restart bot! (attempts: $BOT_RESTART_ATTEMPTS)"
            
            # 재시작 실패 시 즉시 텔레그램 알림
            if [ $BOT_RESTART_ATTEMPTS -ge $MAX_RESTART_ATTEMPTS ]; then
                log_message "🚨 Max restart attempts reached. Sending alert..."
                send_bot_failure_alert $BOT_RESTART_ATTEMPTS
                BOT_RESTART_ATTEMPTS=0  # 알림 후 리셋 (다음 사이클에서 다시 시도)
            fi
        fi
    else
        # 봇이 정상 실행 중이면 카운터 리셋
        if [ $BOT_RESTART_ATTEMPTS -gt 0 ]; then
            log_message "✅ Bot is now running. Resetting restart counter."
            BOT_RESTART_ATTEMPTS=0
        fi
    fi
}

check_and_restart_dashboard() {
    if ! pgrep -f "uvicorn.*web.app:app" > /dev/null; then
        log_message "⚠️ Dashboard (uvicorn) process not found. Restarting..."
        cd "$SCRIPT_DIR"
        nohup venv/bin/python -m uvicorn web.app:app --host 0.0.0.0 --port 8501 >> "$LOG_DIR/dashboard_stdout.log" 2>&1 &
        sleep 3
        if pgrep -f "uvicorn.*web.app:app" > /dev/null; then
            log_message "✅ Dashboard (uvicorn) restarted successfully"
        else
            log_message "❌ Failed to restart dashboard (uvicorn)!"
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
    
    # 일일 보고서 체크 (16시에 발송)
    if should_send_daily_report; then
        log_message "📊 Sending daily report..."
        if send_daily_report; then
            mark_daily_report_sent
            log_message "✅ Daily report sent successfully"
        else
            log_message "❌ Failed to send daily report"
        fi
    fi
    
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
