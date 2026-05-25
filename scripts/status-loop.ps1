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
    
    # Fetch live balance (now includes overall PnL and sends a single Telegram update)
    & $python -m fashion_bot balance
    
    Write-Host "Waiting $IntervalMinutes minutes..."
    Start-Sleep -Seconds ($IntervalMinutes * 60)
}
