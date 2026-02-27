#!/bin/bash
# Run this ON the VM after connecting via Remote-SSH
# Usage: ./vm_setup.sh

set -e
cd "$(dirname "$0")"

echo "=== Polymarket Bot VM Setup ==="

# Ensure Python 3 and pip
sudo apt-get update -qq
sudo apt-get install -y python3-pip python3-venv > /dev/null 2>&1

# Create and activate virtual env
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -q -r requirements.txt

# Create .env from example if missing
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example — edit it with your credentials."
else
    echo ".env already exists."
fi

echo ""
echo "Setup complete. Next steps:"
echo "  1. Edit .env with POLY_PRIVATE_KEY and PROXY_WALLET"
echo "  2. Run: source venv/bin/activate && python main.py"
echo ""
