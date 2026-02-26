# sync_trades.ps1 — Continuously sync trades.csv, bot.log, and bot_state.json from VM to local.
# Run in a separate PowerShell window. Stops when you close the window.
# Usage: .\deploy\sync_trades.ps1
#        .\deploy\sync_trades.ps1 -VmUser "williamreel07" -VmIp "35.246.236.160"

param(
    [string]$VmUser = "williamreel07",
    [string]$VmIp = "35.246.236.160",
    [int]$IntervalSeconds = 30
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LocalDir = Split-Path -Parent $ScriptDir
$SshTarget = "${VmUser}@${VmIp}"
$RemoteBase = "/home/$VmUser/polymarket"

$Files = @(
    @{ Remote = "$RemoteBase/trades.csv"; Local = "$LocalDir\trades.csv" },
    @{ Remote = "$RemoteBase/bot.log"; Local = "$LocalDir\bot.log" },
    @{ Remote = "$RemoteBase/bot_state.json"; Local = "$LocalDir\bot_state.json" }
)

Write-Host "=== PolyMarket Sync (runs until window closed) ==="
Write-Host "VM:     $SshTarget"
Write-Host "Local:  $LocalDir"
Write-Host "Every:  ${IntervalSeconds}s"
Write-Host ""

$Count = 0
while ($true) {
    $Count++
    $Now = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$Now] Sync #$Count"
    foreach ($f in $Files) {
        try {
            scp -q "${SshTarget}:$($f.Remote)" $f.Local 2>$null
            if ($LASTEXITCODE -eq 0) { Write-Host "  OK  $($f.Remote)" } else { Write-Host "  --  $($f.Remote) (not found or error)" }
        } catch {
            Write-Host "  ERR $($f.Remote): $_"
        }
    }
    Write-Host ""
    Start-Sleep -Seconds $IntervalSeconds
}
