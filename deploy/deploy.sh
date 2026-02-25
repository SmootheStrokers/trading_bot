#!/bin/bash
# deploy.sh — Run this from your LOCAL machine to deploy the bot to the GCP VM
# Prerequisites: ssh access to the VM (add your key to ~/.ssh/ or use gcloud compute ssh)
#
# Usage:
#   ./deploy.sh <REPO_URL> [SSH_USER@VM_IP]
#
# Examples:
#   ./deploy.sh https://github.com/yourusername/polymarket-bot.git
#   ./deploy.sh https://github.com/yourusername/polymarket-bot.git myuser@35.246.236.160

set -e
REPO_URL="${1:-}"
TARGET="${2:-}"
VM_IP="${VM_IP:-35.246.236.160}"
SSH_USER="${SSH_USER:-$USER}"

if [ -z "$REPO_URL" ]; then
  echo "Usage: $0 <REPO_URL> [SSH_USER@VM_IP]"
  echo ""
  echo "Example:"
  echo "  $0 https://github.com/yourusername/polymarket-bot.git"
  echo "  $0 https://github.com/yourusername/polymarket-bot.git myuser@35.246.236.160"
  echo ""
  echo "Env overrides: VM_IP, SSH_USER"
  exit 1
fi

if [ -n "$TARGET" ]; then
  SSH_USER="${TARGET%%@*}"
  VM_IP="${TARGET##*@}"
fi

SSH_TARGET="${SSH_USER}@${VM_IP}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== PolyMarket Bot Deployment ==="
echo "Target:  $SSH_TARGET"
echo "Repo:   $REPO_URL"
echo ""

# Step 1: Copy remote-setup and run it (creates polymarket dir)
echo ">>> Uploading remote-setup.sh..."
scp "$SCRIPT_DIR/remote-setup.sh" "${SSH_TARGET}:/tmp/remote-setup.sh"

echo ">>> Running remote setup (clone, pip install, systemd)..."
ssh "$SSH_TARGET" "chmod +x /tmp/remote-setup.sh && /tmp/remote-setup.sh '$REPO_URL' '$SSH_USER'"

# Step 2: Copy .env to VM if it exists (overwrites template)
if [ -f "$PROJECT_DIR/.env" ]; then
  echo ">>> Uploading your .env..."
  scp "$PROJECT_DIR/.env" "${SSH_TARGET}:/home/${SSH_USER}/polymarket/.env"
  echo ">>> Setting PRICE_FEED_SOURCE=binance (Frankfurt can access Binance API)..."
  ssh "$SSH_TARGET" "sed -i 's/^PRICE_FEED_SOURCE=.*/PRICE_FEED_SOURCE=binance/' /home/${SSH_USER}/polymarket/.env; grep -q '^PRICE_FEED_SOURCE=' /home/${SSH_USER}/polymarket/.env || echo 'PRICE_FEED_SOURCE=binance' >> /home/${SSH_USER}/polymarket/.env"
  echo ">>> Restarting bot to pick up .env..."
  ssh "$SSH_TARGET" "systemctl --user restart polymarket-bot"
fi

echo ""
echo "=== Deployment complete ==="
echo "Bot is running at $SSH_TARGET"
echo "  ssh $SSH_TARGET 'journalctl --user -u polymarket-bot -f'   # View logs"
echo "  ssh $SSH_TARGET 'systemctl --user status polymarket-bot'    # Status"
