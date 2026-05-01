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

# Stop existing processes gracefully
log "🔄 Stopping existing processes..."

# Kill all matching processes to avoid duplicate PIDs
pkill -f "python.*run_bot.py" 2>/dev/null && log "  - Bot processes stopped" || log "  - No bot processes found"
pkill -f "uvicorn.*web.app:app" 2>/dev/null && log "  - Uvicorn dashboard stopped" || log "  - No uvicorn dashboard found"
pkill -f "streamlit.*dashboard.py" 2>/dev/null && log "  - Streamlit dashboard stopped (legacy)" || log "  - No streamlit dashboard found"

sleep 3

# Start services
log "🚀 Starting services..."

# Start bot
nohup "$PYTHON_BIN" run_bot.py >> database/bot_stdout.log 2>&1 &
sleep 2

# Start dashboard (FastAPI via uvicorn)
nohup "$PYTHON_BIN" -m uvicorn web.app:app --host 0.0.0.0 --port 8501 >> database/dashboard_stdout.log 2>&1 &
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
