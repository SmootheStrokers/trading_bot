# PolyMarket Bot Deployment (GCP Frankfurt VM)

Deploy the bot to a non-US VM (e.g. Google Cloud Frankfurt) to access the Binance API without 451 geo-restriction errors.

## Architecture

- **systemd** keeps the bot running with auto-restart on crash and start on boot
- **deploy.sh** (local) SSHs to the VM and runs remote setup
- **remote-setup.sh** runs on the VM: clone, pip install, systemd service

## Prerequisites

1. **SSH access to the VM (key-based auth)** — See [SSH Key Setup (Windows)](#ssh-key-setup-windows) below
2. Your bot repo pushed to GitHub/GitLab (private or public)
3. GCP firewall: allow SSH (22) from your IP

### SSH Key Setup (Windows)

1. **Generate key** (if you don't have one):
   ```powershell
   ssh-keygen -t ed25519 -C "your_email@example.com" -f "$env:USERPROFILE\.ssh\id_ed25519"
   ```
   Press Enter to skip passphrase (or set one if you prefer).

2. **Display your public key** (copy this entire line):
   ```powershell
   Get-Content "$env:USERPROFILE\.ssh\id_ed25519.pub"
   ```

3. **Add key in Google Cloud Console**:
   - Go to [Compute Engine → Metadata → SSH keys](https://console.cloud.google.com/compute/metadata/sshKeys)
   - Click **Add SSH key**
   - Paste in format: `williamreel07:ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI... your_email@example.com`  
     (username colon, then your full public key line — no space between username and colon)

4. **Verify** (from PowerShell):
   ```powershell
   ssh -i "$env:USERPROFILE\.ssh\id_ed25519" williamreel07@35.246.236.160 "echo OK"
   ```

5. **Deploy** (use `-IdentityFile` if your key isn't the default `id_rsa`/`id_ed25519`):
   ```powershell
   cd c:\polymarket\deploy
   .\deploy.ps1 -RepoUrl "https://github.com/YOUR_USERNAME/YOUR_REPO.git" -IdentityFile "$env:USERPROFILE\.ssh\id_ed25519"
   ```

## Quick Deploy

From your **local machine** (in the polymarket project directory):

```bash
cd c:\polymarket\deploy
./deploy.sh https://github.com/YOUR_USERNAME/YOUR_REPO.git youruser@35.246.236.160
```

**Windows users:** Use Git Bash or WSL to run the bash script. Or run the steps manually (see below).

## What the deploy script does

1. SSHs to `35.246.236.160` (or your VM IP)
2. Clones your repo to `~/polymarket`
3. Creates a Python venv and installs `requirements.txt`
4. Copies your local `.env` to the VM (if present)
5. Sets `PRICE_FEED_SOURCE=binance` (Frankfurt can access Binance)
6. Installs a systemd user service and starts the bot

## Manual deploy (if script fails)

```bash
# 1. SSH into the VM
ssh youruser@35.246.236.160

# 2. Clone and set up
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git ~/polymarket
cd ~/polymarket
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Create .env (copy from your machine or edit)
# Use scp from your machine:
#   scp .env youruser@35.246.236.160:~/polymarket/
# Then ensure PRICE_FEED_SOURCE=binance

# 4. Create systemd service
mkdir -p ~/.config/systemd/user
# Copy polymarket-bot.service and adjust paths, then:
systemctl --user daemon-reload
systemctl --user enable polymarket-bot
systemctl --user start polymarket-bot
loginctl enable-linger $USER   # Run without login session
```

## Bot management

```bash
ssh youruser@35.246.236.160

# Status
systemctl --user status polymarket-bot

# Logs (live)
journalctl --user -u polymarket-bot -f

# Restart
systemctl --user restart polymarket-bot

# Stop
systemctl --user stop polymarket-bot
```

## Dashboard

To view the dashboard when the bot runs on the VM:

1. **Option A:** Run the dashboard server on the VM:
   ```bash
   ssh youruser@35.246.236.160
   cd ~/polymarket && source venv/bin/activate
   uvicorn server:app --host 0.0.0.0 --port 8000
   ```
   Then open **http://35.246.236.160:8000** in your browser (serve `index.html` from there or use the API directly).

2. **Option B:** Open `index.html` locally and point to the VM API:
   ```
   file:///path/to/index.html?api=http://35.246.236.160:8000
   ```
   Or run a local server: `python -m http.server 3000` and visit:
   ```
   http://localhost:3000/index.html?api=http://35.246.236.160:8000
   ```

## VM IP / Config

- **New VM:** `35.246.236.160` (Frankfurt)
- Dashboard API override: use `?api=http://35.246.236.160:8000` when opening the dashboard

## Required .env variables

- `POLY_PRIVATE_KEY`, `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE` — from polymarket.com → Settings → API Keys
- `PROXY_WALLET=0x9023dBDDf404811C7238D0909C8D8eadCC0592Df` — your Polymarket wallet
- `PRICE_FEED_SOURCE=binance` — set automatically by deploy (Frankfurt can use Binance)
- `PAPER_TRADING`, `DRY_RUN`, `BANKROLL` — as desired

No Binance API keys are required — the bot uses Binance's public API for funding rates and price data.
