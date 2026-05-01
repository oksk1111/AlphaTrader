#!/bin/bash

# Stop script on error
set -e

echo "=== 🚀 Alpha Trader Cloud Setup Script ==="

# 1. Update System
echo "Checking system updates..."
sudo apt update && sudo apt upgrade -y

# 2. Install Dependencies
echo "Installing dependencies..."
sudo apt install -y python3-pip python3-venv git

# 3. Setup Python Virtual Environment
echo "Setting up Virtual Environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "venv created."
fi

source venv/bin/activate

# 4. Install Python Requirements
echo "Installing Python packages..."
pip install --upgrade pip
pip install -r requirements.txt

# 5. Create .env if not exists
if [ ! -f ".env" ]; then
    echo "⚠️  .env file not found!"
    echo "Creating template .env..."
    cat <<EOT >> .env
KIS_APP_KEY=your_app_key_here
KIS_APP_SECRET=your_app_secret_here
KIS_ACCOUNT_NO=your_account_no_here
GEMINI_API_KEY=your_gemini_key_here
DISCORD_WEBHOOK_URL=optional_webhook_url
EOT
    echo "Please edit .env file with your actual keys."
fi

# 6. Setup Systemd Services
echo "Setting up Systemd Services..."

# Bot Service
sudo bash -c "cat > /etc/systemd/system/alphatrader-bot.service" <<EOT
[Unit]
Description=Alpha Trader Bot
After=network.target

[Service]
User=$USER
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/venv/bin/python $(pwd)/run_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOT

# Dashboard Service
sudo bash -c "cat > /etc/systemd/system/alphatrader-dashboard.service" <<EOT
[Unit]
Description=Alpha Trader Dashboard
After=network.target

[Service]
User=$USER
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/venv/bin/streamlit run dashboard.py --server.port 8501 --server.headings.visible=false --server.headless=true
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOT

# 7. Reload and Enable
sudo systemctl daemon-reload
echo "✅ Services registered."
echo "---------------------------------------------------"
echo "To start services:"
echo "1. Edit .env file with your keys."
echo "2. Run: sudo systemctl start alphatrader-bot"
echo "3. Run: sudo systemctl start alphatrader-dashboard"
echo "---------------------------------------------------"
