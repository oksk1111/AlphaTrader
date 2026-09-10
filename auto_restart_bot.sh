#!/bin/bash

# ==============================================
# Alpha Trader Auto Restart Script
# Monitors and auto-restarts both bot and dashboard
# [v6.1] 봇은 장 안팎을 가리지 않고 항상 살려둔다 (개장 순간 프로세스 부재 방지)
# Includes Telegram notifications
# ==============================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Force KST timezone for all date operations (Oracle Cloud default is UTC)
export TZ='Asia/Seoul'

LOG_DIR="$SCRIPT_DIR/database"
RESTART_LOG="$LOG_DIR/restart.log"
DAILY_REPORT_FLAG="$LOG_DIR/.daily_report_sent"
WEEKLY_BACKTEST_FLAG="$LOG_DIR/.weekly_backtest_sent"

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
    
    local message="🚨 <b>Alpha Trader 긴급 알림</b>
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

run_weekly_backtest() {
    cd "$SCRIPT_DIR"

    if [ ! -f "venv/bin/python" ]; then
        log_message "❌ Python venv not found for weekly backtest"
        return 1
    fi

    log_message "🧪 Running weekly backtest report..."
    venv/bin/python modules/backtest_runner.py >> "$LOG_DIR/backtest_stdout.log" 2>&1

    if [ $? -eq 0 ]; then
        log_message "✅ Weekly backtest report generated"
        return 0
    fi

    log_message "❌ Weekly backtest failed"
    return 1
}

should_run_weekly_backtest() {
    local day_of_week=$(date +%u)  # 1=Mon ... 7=Sun
    local current_hour=$(date +%H)
    local current_week=$(date +%G-W%V)

    # 일요일 07시(장 시작 전 점검 시간대)에 1회 실행
    if [ "$day_of_week" -eq 7 ] && [ "$current_hour" -eq 7 ]; then
        if [ -f "$WEEKLY_BACKTEST_FLAG" ]; then
            local last_sent_week
            last_sent_week=$(cat "$WEEKLY_BACKTEST_FLAG")
            if [ "$last_sent_week" == "$current_week" ]; then
                return 1
            fi
        fi
        return 0
    fi

    return 1
}

mark_weekly_backtest_sent() {
    date +%G-W%V > "$WEEKLY_BACKTEST_FLAG"
}

# ==========================================================================
# [v6.1] 시장 시간 판정은 더 이상 이 스크립트가 직접 하지 않는다.
#
# v6.0 은 run_bot.py 의 시계만 modules/market_clock 로 교체하고 이 감시 스크립트는
# 그대로 뒀다. 여기 하드코딩된 미국장 창(23:20~06:10 KST)은 미국 표준시(EST) 기준
# 이라, 1년의 약 2/3 를 차지하는 서머타임(EDT) 기간에는 실제 개장(22:30 KST)보다
# 50분 늦다. 그 50분 동안 check_and_restart_bot 이 호출되지 않으므로, 봇이 밤사이
# 죽어 있으면 **미국장 개장 첫 50분 동안 아무도 되살리지 않았다**. 유동성이 가장
# 높은 구간이 매일 통째로 비는 구조였다.
#
# 더 근본적으로, "장중에만 봇을 되살린다"는 설계 자체가 틀렸다. 감시 스크립트의
# 유일한 임무는 프로세스를 살려두는 것이고, 언제 거래할지는 봇이 스스로 안다
# (job() 은 CLOSED 면 즉시 반환하며 KIS 토큰도 발급하지 않는다). 장 밖에서 봇을
# 죽여둘 이유가 없고, 죽여두면 개장 순간에 살아 있을 보장이 사라진다.
#
# 따라서 v6.1 은 봇을 항상 살려두고, market_clock 은 로그/폴링주기 결정에만 쓴다.
# ==========================================================================

get_market_status() {
    # 봇과 동일한 시계(modules/market_clock)를 사용한다. 실패 시 UNKNOWN.
    local py="$SCRIPT_DIR/venv/bin/python"
    local snippet="from modules import market_clock; print(market_clock.get_market_status())"
    local status=""
    if [ -x "$py" ]; then
        status=$("$py" -c "$snippet" 2>/dev/null)
    fi
    if [ -n "$status" ]; then
        echo "$status"
    else
        echo "UNKNOWN"
    fi
}

# ==========================================================================
# [v6.1] 좀비 감시 — "프로세스가 있다" 는 "봇이 살아 있다" 가 아니다.
#
# 2026-09-10 사고: pgrep 은 run_bot.py 를 정상적으로 찾아냈고 대시보드도
# "🟢 Running" 을 표시했지만, 미국장 개장 후 44분 동안 봇은 로그를 한 줄도
# 쓰지 않았다. 메인 루프가 멈춘(또는 세션 진입이 조용히 막힌) 상태에서도
# 프로세스는 멀쩡히 존재하므로, pgrep 기반 감시로는 **원리적으로** 잡을 수 없다.
#
# 그래서 봇이 주기적으로 갱신하는 database/heartbeat.json 의 '나이'를 본다.
# 오래됐으면 프로세스가 있어도 죽은 것으로 간주하고 강제 재기동한다.
# ==========================================================================

HEARTBEAT_FILE="$LOG_DIR/heartbeat.json"
HEARTBEAT_MAX_AGE_OPEN=300      # 장중: 5분 이상 정지면 좀비
HEARTBEAT_MAX_AGE_CLOSED=1800   # 장외: 30분

check_bot_liveness() {
    local market="$1"
    # 프로세스가 아예 없으면 check_and_restart_bot 이 처리한다.
    pgrep -f "python.*run_bot.py" > /dev/null || return 0
    [ -f "$HEARTBEAT_FILE" ] || return 0   # 아직 한 번도 안 찍힘(기동 직후)

    local max_age="$HEARTBEAT_MAX_AGE_CLOSED"
    if [ "$market" = "US" ] || [ "$market" = "KR" ]; then
        max_age="$HEARTBEAT_MAX_AGE_OPEN"
    fi

    local mtime now age
    mtime=$(date -r "$HEARTBEAT_FILE" +%s 2>/dev/null) || return 0
    now=$(date +%s)
    age=$((now - mtime))

    if [ "$age" -gt "$max_age" ]; then
        log_message "🧟 Bot heartbeat stale (${age}s > ${max_age}s, market=$market). Forcing restart."
        send_telegram "🧟 <b>Alpha Trader</b>%0A봇 프로세스는 살아 있으나 ${age}초 동안 진행이 없습니다 (market=$market).%0A강제 재기동합니다."
        pkill -9 -f "python.*run_bot.py" 2>/dev/null
        sleep 2
        rm -f "$HEARTBEAT_FILE"
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

    # 주간 백테스트 리포트 체크 (일요일 07시)
    if should_run_weekly_backtest; then
        if run_weekly_backtest; then
            mark_weekly_backtest_sent
        fi
    fi
    
    # [v6.1] 봇은 장 안팎을 가리지 않고 항상 살려둔다. 개장 순간에 프로세스가
    # 없어서 첫 구간을 통째로 놓치는 사고를 구조적으로 제거한다.
    # 순서 주의: 먼저 좀비를 걷어낸 뒤 재기동해야 한 사이클 안에 복구된다.
    check_bot_liveness "$MARKET_STATUS"
    check_and_restart_bot

    if [ "$MARKET_STATUS" = "US" ] || [ "$MARKET_STATUS" = "KR" ]; then
        if [ "$LAST_STATUS" != "ACTIVE" ]; then
            log_message "📈 Market is OPEN ($MARKET_STATUS). Polling every 30s."
            LAST_STATUS="ACTIVE"
        fi
        sleep 30
    else
        if [ "$LAST_STATUS" != "IDLE" ]; then
            log_message "😴 Market $MARKET_STATUS. Bot kept alive; polling every 5m."
            LAST_STATUS="IDLE"
        fi
        sleep 300
    fi
done
