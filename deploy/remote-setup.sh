#!/bin/bash
# remote-setup.sh — Run this ON the VM (via SSH) to set up the bot
# Usage: ./remote-setup.sh [REPO_URL] [DEPLOY_USER]
# Example: ./remote-setup.sh https://github.com/you/polymarket-bot.git $USER

set -e
REPO_URL="${1:-}"
DEPLOY_USER="${2:-$(whoami)}"
INSTALL_DIR="/home/${DEPLOY_USER}/polymarket"

if [ -z "$REPO_URL" ]; then
  echo "Usage: $0 <REPO_URL> [DEPLOY_USER]"
  echo "Example: $0 https://github.com/you/polymarket-bot.git"
  exit 1
fi

echo "=== PolyMarket Bot Remote Setup ==="
echo "Repo: $REPO_URL"
echo "User: $DEPLOY_USER"
echo "Dir:  $INSTALL_DIR"
echo ""

# Clone or pull
if [ -d "$INSTALL_DIR/.git" ]; then
  echo ">>> Updating existing repo..."
  cd "$INSTALL_DIR"
  git pull
else
  echo ">>> Cloning repository..."
  git clone "$REPO_URL" "$INSTALL_DIR"
  cd "$INSTALL_DIR"
fi

# Create venv if not exists
if [ ! -d "venv" ]; then
  echo ">>> Creating Python virtual environment..."
  python3 -m venv venv
fi
source venv/bin/activate

# Install dependencies
echo ">>> Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Ensure .env exists (copy from example if missing)
if [ ! -f ".env" ]; then
  echo ">>> Creating .env from template (EDIT WITH YOUR KEYS!)"
  cp .env.example .env
  echo ""
  echo "*** IMPORTANT: Edit /home/${DEPLOY_USER}/polymarket/.env with your credentials ***"
  echo "    - POLY_PRIVATE_KEY, POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE"
  echo "    - PROXY_WALLET=0x9023dBDDf404811C7238D0909C8D8eadCC0592Df"
  echo "    - PRICE_FEED_SOURCE=binance  (Frankfurt can access Binance — no 451!)"
  echo ""
fi

# Set PRICE_FEED_SOURCE=binance for Frankfurt (optional override)
if grep -q "PRICE_FEED_SOURCE" .env; then
  sed -i 's/^PRICE_FEED_SOURCE=.*/PRICE_FEED_SOURCE=binance/' .env
else
  echo "PRICE_FEED_SOURCE=binance" >> .env
fi

# Install systemd user service (runs as DEPLOY_USER)
SERVICE_FILE="/home/${DEPLOY_USER}/.config/systemd/user/polymarket-bot.service"
mkdir -p "$(dirname "$SERVICE_FILE")"
cat > "$SERVICE_FILE" << 'SVCEOF'
[Unit]
Description=PolyMarket 15-Min Up/Down Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/DEPLOY_USER_PLACEHOLDER/polymarket
ExecStart=/home/DEPLOY_USER_PLACEHOLDER/polymarket/venv/bin/python -u main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=polymarket-bot

[Install]
WantedBy=default.target
SVCEOF
sed -i "s|DEPLOY_USER_PLACEHOLDER|${DEPLOY_USER}|g" "$SERVICE_FILE"

# Load env from .env into the service
if [ -f "$INSTALL_DIR/.env" ]; then
  # systemd user services don't support EnvironmentFile the same way; we use ExecStart with env
  # Alternative: use 'env $(cat .env | xargs)' — but .env can have comments. Simpler: run from directory with .env
  # The app loads .env via python-dotenv from CWD, so we're good.
  true
fi

echo ">>> Enabling and starting systemd user service..."
systemctl --user daemon-reload
systemctl --user enable polymarket-bot
systemctl --user restart polymarket-bot

# Enable lingering so user service runs without login
loginctl enable-linger "$DEPLOY_USER" 2>/dev/null || true

echo ""
echo "=== Setup complete ==="
echo "Bot is running. Commands:"
echo "  systemctl --user status polymarket-bot   # Check status"
echo "  systemctl --user restart polymarket-bot  # Restart"
echo "  journalctl --user -u polymarket-bot -f   # View logs"
echo ""
echo "Dashboard (optional): cd $INSTALL_DIR && source venv/bin/activate && uvicorn server:app --host 0.0.0.0 --port 8000"
echo "Then open http://35.246.236.160:8000 (or your VM IP)"
