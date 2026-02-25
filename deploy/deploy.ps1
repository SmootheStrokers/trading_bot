# deploy.ps1 — Run from your LOCAL Windows machine to deploy to the GCP VM
# Usage: .\deploy.ps1 -RepoUrl "https://github.com/yourusername/polymarket-bot.git"
#        .\deploy.ps1 -RepoUrl "..." -VmIp "35.246.236.160" -SshUser "youruser"

param(
    [Parameter(Mandatory=$true)]
    [string]$RepoUrl,
    [string]$VmIp = "35.246.236.160",
    [string]$SshUser = $env:USERNAME
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$SshTarget = "${SshUser}@${VmIp}"

Write-Host "=== PolyMarket Bot Deployment ==="
Write-Host "Target:  $SshTarget"
Write-Host "Repo:    $RepoUrl"
Write-Host ""

# Upload remote-setup.sh
Write-Host ">>> Uploading remote-setup.sh..."
scp "$ScriptDir\remote-setup.sh" "${SshTarget}:/tmp/remote-setup.sh"

# Run remote setup
Write-Host ">>> Running remote setup (clone, pip install, systemd)..."
$setupCmd = "chmod +x /tmp/remote-setup.sh && /tmp/remote-setup.sh '$RepoUrl' '$SshUser'"
ssh $SshTarget $setupCmd

# Upload .env if it exists
$envPath = Join-Path $ProjectDir ".env"
if (Test-Path $envPath) {
    Write-Host ">>> Uploading your .env..."
    scp $envPath "${SshTarget}:/home/${SshUser}/polymarket/.env"
    Write-Host ">>> Setting PRICE_FEED_SOURCE=binance..."
    ssh $SshTarget "sed -i 's/^PRICE_FEED_SOURCE=.*/PRICE_FEED_SOURCE=binance/' /home/${SshUser}/polymarket/.env; grep -q '^PRICE_FEED_SOURCE=' /home/${SshUser}/polymarket/.env || echo 'PRICE_FEED_SOURCE=binance' >> /home/${SshUser}/polymarket/.env"
    Write-Host ">>> Restarting bot..."
    ssh $SshTarget "systemctl --user restart polymarket-bot"
}

Write-Host ""
Write-Host "=== Deployment complete ==="
Write-Host "Bot is running at $SshTarget"
Write-Host "  ssh $SshTarget 'journalctl --user -u polymarket-bot -f'   # View logs"
Write-Host "  ssh $SshTarget 'systemctl --user status polymarket-bot'    # Status"
