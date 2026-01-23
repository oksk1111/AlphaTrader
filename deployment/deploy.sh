#!/bin/bash

# ==============================================
# US-ETF-Sniper Deployment Script
# Called by GitHub Actions for auto-deployment
# ==============================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

LOG_FILE="$PROJECT_DIR/database/deploy.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE"
}

log "🚀 Starting deployment..."

# Git pull
log "📥 Pulling latest changes..."
git fetch origin main
git reset --hard origin/main

# Check if requirements changed
if git diff HEAD~1 --name-only 2>/dev/null | grep -q "requirements.txt"; then
    log "📦 Installing new dependencies..."
    source venv/bin/activate
    pip install -r requirements.txt --quiet
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
source venv/bin/activate

# Start bot
nohup python run_bot.py >> database/bot_stdout.log 2>&1 &
sleep 2

# Start dashboard (FastAPI via uvicorn)
nohup venv/bin/python -m uvicorn web.app:app --host 0.0.0.0 --port 8501 >> database/dashboard_stdout.log 2>&1 &
sleep 3

# Verify
ERRORS=0

if pgrep -f "python.*run_bot.py" > /dev/null; then
    log "✅ Bot started (PID: $(pgrep -f 'python.*run_bot.py'))"
else
    log "❌ Bot failed to start"
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
