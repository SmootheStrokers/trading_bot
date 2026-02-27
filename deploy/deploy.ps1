# deploy.ps1 — Run from your LOCAL Windows machine to deploy to the GCP VM
# Usage: .\deploy.ps1 -RepoUrl "https://github.com/yourusername/polymarket-bot.git"
#        .\deploy.ps1 -RepoUrl "..." -VmIp "35.246.236.160" -SshUser "williamreel07"
#        .\deploy.ps1 -RepoUrl "..." -SshUser "williamreel07" -IdentityFile "$env:USERPROFILE\.ssh\id_ed25519"
#
# IMPORTANT: Pass -SshUser "williamreel07" (your GCP VM user). $env:USERNAME is your Windows user, not the VM user.
# If using a custom SSH key: -IdentityFile "$env:USERPROFILE\.ssh\id_ed25519"

param(
    [Parameter(Mandatory=$true)]
    [string]$RepoUrl,
    [string]$VmIp = "34.95.194.231",
    [string]$SshUser = "williamreel07",
    [string]$IdentityFile = ""   # e.g. "$env:USERPROFILE\.ssh\id_ed25519" — leave empty for default ~/.ssh/id_rsa or id_ed25519
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$SshTarget = "${SshUser}@${VmIp}"

# Build ssh/scp args for optional identity file
$SshScpArgs = @()
if ($IdentityFile -and (Test-Path $IdentityFile)) {
    $SshScpArgs = @("-i", $IdentityFile)
}

Write-Host "=== PolyMarket Bot Deployment ==="
Write-Host "Target:  $SshTarget"
Write-Host "Repo:    $RepoUrl"
if ($SshScpArgs.Count -gt 0) {
    Write-Host "Key:     $IdentityFile"
}
Write-Host ""

# Upload remote-setup.sh
Write-Host ">>> Uploading remote-setup.sh..."
if ($SshScpArgs.Count -gt 0) {
    scp $SshScpArgs "$ScriptDir\remote-setup.sh" "${SshTarget}:/tmp/remote-setup.sh"
} else {
    scp "$ScriptDir\remote-setup.sh" "${SshTarget}:/tmp/remote-setup.sh"
}

# Run remote setup
Write-Host ">>> Running remote setup (clone, pip install, systemd)..."
$setupCmd = "chmod +x /tmp/remote-setup.sh && /tmp/remote-setup.sh '$RepoUrl' '$SshUser'"
if ($SshScpArgs.Count -gt 0) {
    ssh $SshScpArgs $SshTarget $setupCmd
} else {
    ssh $SshTarget $setupCmd
}

# Upload .env if it exists
$envPath = Join-Path $ProjectDir ".env"
if (Test-Path $envPath) {
    Write-Host ">>> Uploading your .env..."
    if ($SshScpArgs.Count -gt 0) {
        scp $SshScpArgs $envPath "${SshTarget}:/home/${SshUser}/polymarket/.env"
    } else {
        scp $envPath "${SshTarget}:/home/${SshUser}/polymarket/.env"
    }
    Write-Host ">>> Setting PRICE_FEED_SOURCE=binance..."
    if ($SshScpArgs.Count -gt 0) {
        ssh $SshScpArgs $SshTarget "sed -i 's/^PRICE_FEED_SOURCE=.*/PRICE_FEED_SOURCE=binance/' /home/${SshUser}/polymarket/.env; grep -q '^PRICE_FEED_SOURCE=' /home/${SshUser}/polymarket/.env || echo 'PRICE_FEED_SOURCE=binance' >> /home/${SshUser}/polymarket/.env"
        ssh $SshScpArgs $SshTarget "systemctl --user restart polymarket-bot"
    } else {
        ssh $SshTarget "sed -i 's/^PRICE_FEED_SOURCE=.*/PRICE_FEED_SOURCE=binance/' /home/${SshUser}/polymarket/.env; grep -q '^PRICE_FEED_SOURCE=' /home/${SshUser}/polymarket/.env || echo 'PRICE_FEED_SOURCE=binance' >> /home/${SshUser}/polymarket/.env"
        ssh $SshTarget "systemctl --user restart polymarket-bot"
    }
}

Write-Host ""
Write-Host "=== Deployment complete ==="
Write-Host "Bot is running at $SshTarget"
Write-Host "  ssh $SshTarget 'journalctl --user -u polymarket-bot -f'   # View logs"
Write-Host "  ssh $SshTarget 'systemctl --user status polymarket-bot'    # Status"
