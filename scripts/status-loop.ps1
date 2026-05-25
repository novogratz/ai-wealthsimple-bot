param(
    [int]$IntervalMinutes = 30
)

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

Write-Host "Starting Status Loop (every $IntervalMinutes minutes)..."
Write-Host "Press Ctrl+C to stop."

while ($true) {
    $now = Get-Date -Format "HH:mm:ss"
    Write-Host "[$now] Updating status..."
    
    # 1. Fetch live balance (this also sends a Telegram update)
    & $python -m fashion_bot balance
    
    # 2. Calculate and send PnL update
    & $python -m fashion_bot pnl --notify
    
    Write-Host "Waiting $IntervalMinutes minutes..."
    Start-Sleep -Seconds ($IntervalMinutes * 60)
}
