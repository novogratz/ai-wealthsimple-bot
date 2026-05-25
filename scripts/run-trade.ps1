<#
.SYNOPSIS
    Full trading day: scan TSX -> browser buy -> wait until 15:45 ET -> browser sell.
    Uses Playwright automation with DOM selectors.

.EXAMPLE
    .\scripts\run-trade.ps1 -Balance 17.24           # full day, review only
    .\scripts\run-trade.ps1 -Balance 17.24 -BuyOnly  # morning buy only, review only
    .\scripts\run-trade.ps1 -SellOnly                # close open position, review only
    .\scripts\run-trade.ps1 -Balance 17.24 -DryRun   # scan only, no browser
    .\scripts\run-trade.ps1 -Balance 17.24 -Confirm  # submit real orders

    First-time setup (saves Wealthsimple session):
        python scripts/wealthsimple_auto.py setup
#>
param(
    [double]$Balance = 17.24,
    [switch]$DryRun,
    [switch]$Confirm,
    [switch]$BuyOnly,
    [switch]$SellOnly,
    [int]$ExitHour = 15,
    [int]$ExitMin = 45
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$posFile = Join-Path $root "data\open_position.json"

# Prefer the project venv, fall back to PATH.
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

$autoScript = Join-Path $root "scripts\wealthsimple_auto.py"

function Send-TradeNotification {
    param(
        [Parameter(Mandatory = $true)][string]$Event,
        [string]$Symbol,
        [Nullable[int]]$Shares,
        [Nullable[double]]$Price,
        [string]$Message
    )

    $args = @("-m", "fashion_bot", "notify", "--event", $Event)
    if ($Symbol) { $args += @("--symbol", $Symbol) }
    if ($null -ne $Shares) { $args += @("--shares", $Shares) }
    if ($null -ne $Price) { $args += @("--price", $Price) }
    if ($Message) { $args += @("--message", $Message) }

    $oldErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $out = & $python @args 2>&1
    $exit = $LASTEXITCODE
    $ErrorActionPreference = $oldErrorAction

    if ($exit -ne 0 -and $exit -ne 2) {
        Write-Warning "Telegram notification failed: $($out -join ' ')"
    }
}

function Get-ScanSummary {
    param([object[]]$ScanOutput)

    $candidateLines = $ScanOutput |
        Where-Object { $_ -match '^\s+\d+\s+\S+' } |
        Select-Object -First 5

    if (-not $candidateLines) {
        return $null
    }

    $summary = @("Potential purchases from scan:")
    foreach ($line in $candidateLines) {
        $cols = ($line -replace '\s+', ' ' -replace '^\s+', '') -split ' '
        if ($cols.Count -ge 5) {
            $rank = $cols[0]
            $symbol = $cols[1]
            $price = $cols[2]
            $shares = $cols[3]
            $score = $cols[4]
            $summary += "$rank. $symbol @ `$$price, shares $shares, score $score"
        }
    }
    return ($summary -join "`n")
}

# =============================================================================
# STEP 1 - Scan
# =============================================================================
if (-not $SellOnly) {
    Write-Host ""
    Write-Host "=== [1/4] SCANNING TSX  budget: `$$Balance ==="

    $oldErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $scanOut = & $python -m fashion_bot scan --cash $Balance 2>&1
    $scanExit = $LASTEXITCODE
    $ErrorActionPreference = $oldErrorAction
    $scanOut | ForEach-Object { Write-Host "    $_" }

    if ($scanExit -ne 0) {
        Write-Error "Scan failed (exit $scanExit)."
        exit $scanExit
    }

    $scanSummary = Get-ScanSummary -ScanOutput $scanOut
    if ($scanSummary) {
        Send-TradeNotification -Event "scan_candidates" -Message $scanSummary
    }

    $topLine = $scanOut | Where-Object { $_ -match '^\s+\d+\s+\S+' } | Select-Object -First 1
    if (-not $topLine) {
        Write-Error "No candidates - markets may be closed or no stock passed filters."
        exit 1
    }

    $cols = ($topLine -replace '\s+', ' ' -replace '^\s+', '') -split ' '
    $Symbol = $cols[1]
    $Price = [double]$cols[2]
    $Shares = [math]::Floor($Balance / $Price)
    $wsSymbol = $Symbol -replace '\.(TO|V|CN|NE)$', ''

    Write-Host ""
    Write-Host "  TOP PICK : $Symbol"
    Write-Host "  Price    : `$$Price"
    Write-Host "  Shares   : $Shares"
    Write-Host "  WS search: $wsSymbol"
    Write-Host ""
    Send-TradeNotification -Event "scan_top" -Symbol $Symbol -Shares $Shares -Price $Price -Message "Top scan result selected."

    if ($DryRun) {
        Write-Host "[DryRun] No browser opened."
        exit 0
    }

    # =========================================================================
    # STEP 2 - Navigate and Buy
    # =========================================================================
    Write-Host "=== [2/4] BUYING $Shares x $Symbol ==="
    $buyArgs = @(
        $autoScript,
        "buy",
        "--symbol", $Symbol,
        "--max-dollars"
    )
    if ($Confirm) {
        $buyArgs += "--confirm"
        Write-Host "    Confirm mode enabled: this will submit the buy order."
    } else {
        Write-Host "    Review mode: stopping before final buy submit."
    }
    Send-TradeNotification -Event "buy_preparing" -Symbol $Symbol -Shares $Shares -Price $Price -Message "Preparing Wealthsimple buy ticket with Dollars Max."
    & $python @buyArgs

    if ($LASTEXITCODE -ne 0) {
        Send-TradeNotification -Event "error" -Symbol $Symbol -Message "Buy automation failed."
        Write-Error "Buy automation failed (exit $LASTEXITCODE)."
        exit $LASTEXITCODE
    }

    $pos = @{
        symbol = $Symbol
        wsSymbol = $wsSymbol
        shares = $Shares
        buyPrice = $Price
        time = (Get-Date -Format "o")
    }
    $pos | ConvertTo-Json | Set-Content $posFile -Encoding utf8
    Write-Host ""
    if ($Confirm) {
        Write-Host "[OK] BUY submitted: $Shares x $Symbol @ `$$Price"
        Send-TradeNotification -Event "buy_submitted" -Symbol $Symbol -Shares $Shares -Price $Price -Message "Buy automation submitted the order."
    } else {
        Write-Host "[OK] BUY review prepared: $Shares x $Symbol @ `$$Price"
        Send-TradeNotification -Event "buy_review" -Symbol $Symbol -Shares $Shares -Price $Price -Message "Buy ticket is ready for manual review."
    }
    Write-Host "     Position saved: $posFile"
}

if ($BuyOnly) {
    Write-Host ""
    Write-Host "BuyOnly - done. Run with -SellOnly to close position."
    exit 0
}

# =============================================================================
# STEP 3 - Wait
# =============================================================================
if (-not (Test-Path $posFile)) {
    Send-TradeNotification -Event "error" -Message "No open position file found for sell."
    Write-Error "No position file at $posFile - run buy first."
    exit 1
}

$pos = Get-Content $posFile | ConvertFrom-Json
$Symbol = $pos.symbol
$wsSymbol = $pos.wsSymbol
$Shares = [int]$pos.shares

$tz = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$targetMin = $ExitHour * 60 + $ExitMin
$nowET = [TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $tz)
$nowMin = $nowET.Hour * 60 + $nowET.Minute

if ($nowMin -lt $targetMin) {
    Write-Host ""
    Write-Host "=== Holding $Shares x $Symbol - selling at $ExitHour`:$($ExitMin.ToString('00')) ET ==="
    while ($true) {
        $nowET = [TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $tz)
        $nowMin = $nowET.Hour * 60 + $nowET.Minute
        if ($nowMin -ge $targetMin) { break }
        $left = $targetMin - $nowMin
        Write-Host ("  " + $nowET.ToString('HH:mm') + " ET - sell in $left min  (Ctrl+C to abort)")
        Start-Sleep -Seconds 60
    }
}

# =============================================================================
# STEP 4 - Sell
# =============================================================================
Write-Host ""
Write-Host "=== [4/4] SELLING $Shares x $Symbol ==="
$sellArgs = @(
    $autoScript,
    "sell",
    "--symbol", $Symbol,
    "--shares", $Shares
)
if ($Confirm) {
    $sellArgs += "--confirm"
    Write-Host "    Confirm mode enabled: this will submit the sell order."
} else {
    Write-Host "    Review mode: stopping before final sell submit."
}
Send-TradeNotification -Event "sell_preparing" -Symbol $Symbol -Shares $Shares -Message "Preparing Wealthsimple sell ticket."
& $python @sellArgs

if ($LASTEXITCODE -ne 0) {
    Send-TradeNotification -Event "error" -Symbol $Symbol -Shares $Shares -Message "Sell automation failed."
    Write-Error "Sell automation failed (exit $LASTEXITCODE)."
    exit $LASTEXITCODE
}

Remove-Item $posFile -ErrorAction SilentlyContinue
Write-Host ""
if ($Confirm) {
    Write-Host "[OK] SELL submitted: $Shares x $Symbol. Position closed."
    Send-TradeNotification -Event "sell_submitted" -Symbol $Symbol -Shares $Shares -Message "Sell automation submitted the order."
} else {
    Write-Host "[OK] SELL review prepared: $Shares x $Symbol."
    Send-TradeNotification -Event "sell_review" -Symbol $Symbol -Shares $Shares -Message "Sell ticket is ready for manual review."
}
Write-Host "     Check data\screen_sell_done.png to verify."
