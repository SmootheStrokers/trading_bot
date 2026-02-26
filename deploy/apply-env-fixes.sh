#!/bin/bash
# Apply .env fixes for bot to trade 24/7 with relaxed edge requirements.
# Run from project root or: bash deploy/apply-env-fixes.sh

cd "$(dirname "$0")/.." || exit 1

ENV_FILE="${ENV_FILE:-.env}"
[ -f "$ENV_FILE" ] || touch "$ENV_FILE"

# Remove old conflicting lines
sed -i.bak -e '/^ACTIVE_HOURS_ENABLED=/d' \
          -e '/^MIN_EDGE_SIGNALS=/d' \
          -e '/^MIN_KELLY_EDGE=/d' \
          -e '/^MIN_EDGE_PCT=/d' \
          -e '/^XRP_REQUIRE_CATALYST=/d' \
          -e '/^MIN_MARKET_VOLUME_USD=/d' \
          "$ENV_FILE" 2>/dev/null || true

# Append new values
{
  echo ""
  echo "# Applied by apply-env-fixes.sh"
  echo "ACTIVE_HOURS_ENABLED=false"
  echo "MIN_EDGE_SIGNALS=2"
  echo "MIN_KELLY_EDGE=0.03"
  echo "MIN_EDGE_PCT=0.03"
  echo "XRP_REQUIRE_CATALYST=false"
  echo "MIN_MARKET_VOLUME_USD=500"
  echo "PAPER_TRADING=true"
} >> "$ENV_FILE"

echo "Applied .env fixes. Restart bot to pick up changes."
echo "  systemctl --user restart polymarket-bot"
echo "  journalctl --user -u polymarket-bot -f"
