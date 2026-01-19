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

if pgrep -f "python.*run_bot.py" > /dev/null; then
    pkill -f "python.*run_bot.py" || true
    log "  - Bot stopped"
fi

if pgrep -f "streamlit.*dashboard.py" > /dev/null; then
    pkill -f "streamlit.*dashboard.py" || true
    log "  - Dashboard stopped"
fi

sleep 2

# Start services
log "🚀 Starting services..."
source venv/bin/activate

# Start bot
nohup python run_bot.py >> database/bot_stdout.log 2>&1 &
sleep 2

# Start dashboard
nohup streamlit run dashboard.py --server.port 8501 --server.address 0.0.0.0 >> database/dashboard_stdout.log 2>&1 &
sleep 3

# Verify
ERRORS=0

if pgrep -f "python.*run_bot.py" > /dev/null; then
    log "✅ Bot started (PID: $(pgrep -f 'python.*run_bot.py'))"
else
    log "❌ Bot failed to start"
    ERRORS=$((ERRORS + 1))
fi

if pgrep -f "streamlit.*dashboard.py" > /dev/null; then
    log "✅ Dashboard started"
else
    log "❌ Dashboard failed to start"
    ERRORS=$((ERRORS + 1))
fi

if [ $ERRORS -eq 0 ]; then
    log "🎉 Deployment completed successfully!"
    exit 0
else
    log "⚠️ Deployment completed with $ERRORS error(s)"
    exit 1
fi
