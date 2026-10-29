#!/bin/bash

# ==============================================
# Alpha Trader Deployment Script
# Called by GitHub Actions for auto-deployment
# ==============================================

set -e

# Force KST timezone for all date operations
export TZ='Asia/Seoul'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

LOG_FILE="$PROJECT_DIR/database/deploy.log"
PYTHON_BIN="$PROJECT_DIR/venv/bin/python"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE"
}

# ==========================================================================
# [v6.1] 배포 락
#
# 이 스크립트는 pkill 로 봇을 죽인 뒤 3초 후 새로 띄운다. 그 사이에
# auto_restart_bot.sh 가 "봇이 없다"고 판단해 또 하나를 띄우면 봇이 두 개 돌고
# **같은 신호에 주문이 두 번 나간다.** 락으로 그 구간을 막는다.
# trap 으로 정상/비정상 종료 모두에서 반드시 해제한다.
# ==========================================================================
DEPLOY_LOCK="$PROJECT_DIR/database/.deploying"
mkdir -p "$PROJECT_DIR/database"
touch "$DEPLOY_LOCK"
trap 'rm -f "$DEPLOY_LOCK"' EXIT

log "🚀 Starting deployment..."

if [ ! -x "$PYTHON_BIN" ]; then
    log "🐍 Python virtual environment not found. Creating venv..."
    python3 -m venv "$PROJECT_DIR/venv"
fi

# Git pull
log "📥 Pulling latest changes..."
git fetch origin main
git reset --hard origin/main

# Check if requirements changed
if git diff HEAD~1 --name-only 2>/dev/null | grep -q "requirements.txt" || [ ! -f "$PROJECT_DIR/venv/.deps_installed" ]; then
    log "📦 Installing new dependencies..."
    "$PYTHON_BIN" -m pip install --upgrade pip --quiet
    "$PYTHON_BIN" -m pip install -r requirements.txt --quiet
    touch "$PROJECT_DIR/venv/.deps_installed"
fi

# Stop 전에 새 코드와 현재 자격증명으로 read-only preflight를 끝낸다. 실패한
# 배포가 정상 동작 중인 봇을 먼저 끄는 것은 v6.1 관측 원칙에 정면으로 어긋난다.
# 이 명령은 shadow broker로 인증·계좌·시세·웹소켓만 검사하며 주문을 제출하지 않는다.
grep -q '^LIVE_GATE_SECRET=.' .env || { log "❌ LIVE_GATE_SECRET is required"; exit 1; }
log "🔐 Running read-only broker preflight before process restart..."
if ! "$PYTHON_BIN" scripts/live_preflight.py >> database/live_preflight.log 2>&1; then
    log "❌ Broker preflight failed — existing processes left untouched"
    tail -n 80 database/live_preflight.log | sed -E 's/(Bearer |access_token|client_secret)[^[:space:]"]+/\1[REDACTED]/g' | tee -a "$LOG_FILE"
    exit 1
fi
log "✅ Broker preflight passed (no order submitted)"

# Stop existing processes gracefully
log "🔄 Stopping existing processes..."

# Kill all matching processes to avoid duplicate PIDs
pkill -f "python.*run_bot.py" 2>/dev/null && log "  - Bot processes stopped" || log "  - No bot processes found"
pkill -f "uvicorn.*web.app:app" 2>/dev/null && log "  - Uvicorn dashboard stopped" || log "  - No uvicorn dashboard found"
pkill -f "streamlit.*dashboard.py" 2>/dev/null && log "  - Streamlit dashboard stopped (legacy)" || log "  - No streamlit dashboard found"
pkill -f "python.*modules.shadow_collector" 2>/dev/null && log "  - Toss shadow collector stopped" || log "  - No Toss shadow collector found"

sleep 3

# Start services
log "🚀 Starting services..."

# Start bot
nohup "$PYTHON_BIN" run_bot.py >> database/bot_stdout.log 2>&1 &
sleep 2

# Toss shadow collector: 장중 전략 검증용 실시간 데이터 수집 (EXECUTION_MODE 와 무관하게
# TossBroker(execution_mode="shadow") 로 돌아 주문을 절대 내지 않는다). 토스 자격증명이 있을 때만.
if grep -q '^TOSS_CLIENT_ID=.' .env 2>/dev/null; then
    pkill -f "python.*modules.shadow_collector" 2>/dev/null || true
    nohup "$PYTHON_BIN" -m modules.shadow_collector >> database/toss_shadow_stdout.log 2>&1 &
    log "  - Toss shadow collector started"
fi

# Start dashboard (FastAPI via uvicorn)
# HTTPS reverse proxy만 dashboard에 연결한다. 8501을 공인망에 직접 열지 않는다.
nohup "$PYTHON_BIN" -m uvicorn web.app:app --host 127.0.0.1 --port 8501 >> database/dashboard_stdout.log 2>&1 &
sleep 3

# Verify
ERRORS=0

if pgrep -f "python.*run_bot.py" > /dev/null; then
    log "✅ Bot started (PID: $(pgrep -f 'python.*run_bot.py'))"
else
    log "❌ Bot failed to start"
    if [ -f database/bot_stdout.log ]; then
        log "📋 Last bot logs:"
        tail -n 40 database/bot_stdout.log | tee -a "$LOG_FILE"
    fi
    ERRORS=$((ERRORS + 1))
fi

if pgrep -f "uvicorn.*web.app:app" > /dev/null; then
    log "✅ Dashboard (uvicorn) started"
else
    log "❌ Dashboard (uvicorn) failed to start"
    ERRORS=$((ERRORS + 1))
fi

if [ $ERRORS -eq 0 ]; then
    log "🎉 Deployment completed successfully!"
    exit 0
else
    log "⚠️ Deployment completed with $ERRORS error(s)"
    exit 1
fi
