<#
.SYNOPSIS
    Full trading day: scan TSX -> browser buy 9:30-9:35 ET -> hold -> auto-sell at 15:55 ET.
    Uses Playwright automation with DOM selectors. Orders are always submitted automatically.

.EXAMPLE
    .\scripts\run-trade.ps1 -AutoDay                            # full automation: wait, scan, buy, hold, sell
    .\scripts\run-trade.ps1 -Balance 17.24 -BuyOnly             # morning buy only
    .\scripts\run-trade.ps1 -SellOnly                           # close open position now
    .\scripts\run-trade.ps1 -Balance 17.24 -DryRun              # scan only, no browser

    First-time setup (saves Wealthsimple session):
        python scripts/wealthsimple_auto.py setup
#>
param(
    [double]$Balance = 17.24,
    [switch]$DryRun,
    [switch]$AutoDay,
    [switch]$AllowLateEntry,
    [switch]$BuyOnly,
    [switch]$SellOnly,
    [int]$EntryHour = 9,
    [int]$EntryMin = 30,
    [int]$LatestEntryMin = 35,
    [int]$ExitHour = 15,
    [int]$ExitMin = 55
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$posFile = Join-Path $root "data\open_position.json"
$pnlFile = Join-Path $root "data\pnl_ledger.json"

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

function ConvertTo-NullableDouble {
    param([object]$Value)
    if ($null -eq $Value) { return $null }
    try {
        return [double]$Value
    } catch {
        return $null
    }
}

function Invoke-Automation {
    param([string[]]$CommandArgs)

    $oldErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $out = & $python @CommandArgs 2>&1
    $exit = $LASTEXITCODE
    $ErrorActionPreference = $oldErrorAction

    $out | ForEach-Object { Write-Host "    $_" }

    $jsonLine = $out | Where-Object { $_ -like "ORDER_RESULT_JSON:*" } | Select-Object -Last 1
    $result = $null
    if ($jsonLine) {
        try {
            $result = ($jsonLine -replace '^ORDER_RESULT_JSON:', '') | ConvertFrom-Json
        } catch {
            Write-Warning "Could not parse automation result JSON."
        }
    }

    return @{
        exit = $exit
        output = $out
        result = $result
    }
}

function Get-AllTimePnl {
    if (-not (Test-Path $pnlFile)) { return 0.0 }
    try {
        $items = Get-Content $pnlFile -Raw | ConvertFrom-Json
        if ($null -eq $items) { return 0.0 }
        $sum = 0.0
        foreach ($item in @($items)) {
            $sum += [double]$item.realizedPnl
        }
        return $sum
    } catch {
        return 0.0
    }
}

function Add-RealizedPnl {
    param(
        [string]$Symbol,
        [double]$BuyCost,
        [double]$SellValue,
        [double]$Quantity
    )

    $items = @()
    if (Test-Path $pnlFile) {
        try {
            $loaded = Get-Content $pnlFile -Raw | ConvertFrom-Json
            if ($loaded) { $items = @($loaded) }
        } catch {
            $items = @()
        }
    }

    $items += [pscustomobject]@{
        symbol = $Symbol
        quantity = $Quantity
        buyCost = $BuyCost
        sellValue = $SellValue
        realizedPnl = ($SellValue - $BuyCost)
        time = (Get-Date -Format "o")
    }
    $items | ConvertTo-Json | Set-Content $pnlFile -Encoding utf8
}

function Get-QuotePrice {
    param([string]$Symbol)

    $oldErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $out = & $python -m fashion_bot quote --symbol $Symbol --json 2>&1
    $exit = $LASTEXITCODE
    $ErrorActionPreference = $oldErrorAction
    if ($exit -ne 0) { return $null }

    try {
        $quote = ($out | Select-Object -Last 1) | ConvertFrom-Json
        return [double]$quote.last_price
    } catch {
        return $null
    }
}

function Format-Money {
    param([double]$Value)
    return ("{0:+$0.00;-$0.00;$0.00}" -f $Value)
}

function Get-NowEt {
    $tz = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    return [TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $tz)
}

function Wait-ForEntryWindow {
    param(
        [int]$Hour,
        [int]$Minute,
        [int]$LatestMinute,
        [switch]$AllowLate
    )

    while ($true) {
        $nowET = Get-NowEt
        if ($nowET.DayOfWeek -eq [DayOfWeek]::Saturday -or $nowET.DayOfWeek -eq [DayOfWeek]::Sunday) {
            $next = $nowET.Date.AddDays(1).AddHours($Hour).AddMinutes($Minute)
            while ($next.DayOfWeek -eq [DayOfWeek]::Saturday -or $next.DayOfWeek -eq [DayOfWeek]::Sunday) {
                $next = $next.AddDays(1)
            }
            $wait = [math]::Max(1, [int]($next - $nowET).TotalMinutes)
            Write-Host ("  " + $nowET.ToString("yyyy-MM-dd HH:mm") + " ET - weekend, entry in ~$wait min")
            Send-TradeNotification -Event "info" -Message ("Weekend wait. Next entry window starts " + $next.ToString("yyyy-MM-dd HH:mm") + " ET.")
            Start-Sleep -Seconds ([math]::Min(3600, $wait * 60))
            continue
        }

        $entry = $nowET.Date.AddHours($Hour).AddMinutes($Minute)
        $latest = $nowET.Date.AddHours($Hour).AddMinutes($LatestMinute)

        if ($nowET -lt $entry) {
            $wait = [math]::Max(1, [int]($entry - $nowET).TotalMinutes)
            Write-Host ("  " + $nowET.ToString("HH:mm") + " ET - entry in ~$wait min")
            Send-TradeNotification -Event "info" -Message ("Waiting for entry window at " + $entry.ToString("HH:mm") + " ET.")
            Start-Sleep -Seconds ([math]::Min(900, $wait * 60))
            continue
        }

        if ($nowET -gt $latest -and -not $AllowLate) {
            Send-TradeNotification -Event "error" -Message ("Entry window missed. Current time " + $nowET.ToString("HH:mm") + " ET, latest entry " + $latest.ToString("HH:mm") + " ET.")
            Write-Error "Entry window missed. Use -AllowLateEntry to override."
            exit 1
        }

        Write-Host ("  Entry window active at " + $nowET.ToString("HH:mm") + " ET")
        return
    }
}

if ($AutoDay -and -not $SellOnly -and -not $DryRun) {
    Write-Host ""
    Write-Host "=== Waiting for entry window $EntryHour`:$($EntryMin.ToString('00'))-$EntryHour`:$($LatestEntryMin.ToString('00')) ET ==="
    Wait-ForEntryWindow -Hour $EntryHour -Minute $EntryMin -LatestMinute $LatestEntryMin -AllowLate:$AllowLateEntry
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
    Send-TradeNotification -Event "buy_preparing" -Symbol $Symbol -Shares $Shares -Price $Price -Message "Next move: planning to buy $Symbol with Dollars Max in the Non-registered account."
    $buyRun = Invoke-Automation -CommandArgs $buyArgs

    if ($buyRun.exit -ne 0) {
        Send-TradeNotification -Event "error" -Symbol $Symbol -Message "Buy automation failed."
        Write-Error "Buy automation failed (exit $($buyRun.exit))."
        exit $buyRun.exit
    }

    $buyResult = $buyRun.result
    $estimatedQuantity = ConvertTo-NullableDouble $buyResult.estimated_quantity
    $estimatedCost = ConvertTo-NullableDouble $buyResult.estimated_value

    $pos = @{
        symbol = $Symbol
        wsSymbol = $wsSymbol
        shares = $Shares
        buyPrice = $Price
        estimatedQuantity = $estimatedQuantity
        estimatedCost = $estimatedCost
        sellAll = $true
        time = (Get-Date -Format "o")
    }
    $pos | ConvertTo-Json | Set-Content $posFile -Encoding utf8
    Write-Host ""
    Write-Host "[OK] BUY submitted: $Shares x $Symbol @ `$$Price"
    $msg = "Buy automation submitted the order."
    if ($estimatedQuantity -and $estimatedCost) {
        $msg += "`nEstimated quantity: $estimatedQuantity shares`nEstimated cost: `$$($estimatedCost.ToString('0.00')) CAD"
    }
    Send-TradeNotification -Event "buy_submitted" -Symbol $Symbol -Shares $Shares -Price $Price -Message $msg
    Write-Host "     Position saved: $posFile"
}

if ($BuyOnly) {
    Write-Host ""
    Write-Host "BuyOnly - done. Run with -SellOnly to close position."
    exit 0
}

# =============================================================================
# STEP 3 - Auto-Sell (AutoDay mode)
# =============================================================================
if ($AutoDay -and -not $SellOnly) {
    if (-not (Test-Path $posFile)) {
        Send-TradeNotification -Event "error" -Message "No open position file found for sell."
        Write-Error "No position file at $posFile - run buy first."
        exit 1
    }

    $pos = Get-Content $posFile -Raw | ConvertFrom-Json
    $Symbol = $pos.symbol
    $Shares = [int]$pos.shares
    $BuyCost = [double]($pos.estimatedCost)
    $AllTimePnlBefore = Get-AllTimePnl

    Write-Host ""
    Write-Host "=== [3/4] AUTO-MONITOR $Symbol ==="
    Send-TradeNotification -Event "info" -Symbol $Symbol -Shares $Shares -Message ("Holding $Symbol until close.`nEntry: `$$($pos.buyPrice) CAD`nShares: $Shares`nAll-time P/L before: " + (Format-Money $AllTimePnlBefore) + " CAD")

    $watchArgs = @("-m", "fashion_bot", "watch", "--position-file", $posFile)
    $watchRun = Invoke-Automation -CommandArgs $watchArgs

    if ($watchRun.exit -ne 0) {
        Send-TradeNotification -Event "error" -Symbol $Symbol -Message "Auto-sell monitoring failed."
        Write-Error "Auto-sell monitoring failed (exit $($watchRun.exit))."
        exit $watchRun.exit
    }

    $sellResult = $watchRun.result
    $sellValue = $null
    $sellQuantity = $null
    if ($sellResult) {
        $sellQuantity = ConvertTo-NullableDouble $sellResult.estimated_quantity
        $sellValue = ConvertTo-NullableDouble $sellResult.estimated_value
    }
    if ($null -eq $sellQuantity) { $sellQuantity = [double]$Shares }
    if ($null -eq $sellValue) { $sellValue = $sellQuantity * [double]$pos.buyPrice }

    $tradePnl = $sellValue - $BuyCost
    Add-RealizedPnl -Symbol $Symbol -BuyCost $BuyCost -SellValue $sellValue -Quantity $sellQuantity

    Remove-Item $posFile -ErrorAction SilentlyContinue
    $allTimeAfter = Get-AllTimePnl

    Write-Host ""
    Write-Host "[OK] Auto-sold $Symbol. Position closed."
    $msg = "Auto-sold $Symbol.`nEstimated proceeds: `$$($sellValue.ToString('0.00')) CAD`nTrade P/L: $(Format-Money $tradePnl) CAD`nAll-time P/L: $(Format-Money $allTimeAfter) CAD"
    Send-TradeNotification -Event "sell_submitted" -Symbol $Symbol -Shares $Shares -Message $msg
    exit 0
}

# =============================================================================
# STEP 3 - Wait (legacy, for SellOnly / manual mode)
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
$EstimatedQuantity = ConvertTo-NullableDouble $pos.estimatedQuantity
if ($null -eq $EstimatedQuantity) { $EstimatedQuantity = [double]$Shares }
$BuyCost = ConvertTo-NullableDouble $pos.estimatedCost
if ($null -eq $BuyCost) { $BuyCost = $EstimatedQuantity * [double]$pos.buyPrice }
$AllTimePnlBefore = Get-AllTimePnl

$targetMin = $ExitHour * 60 + $ExitMin
$nowET = Get-NowEt
$nowMin = $nowET.Hour * 60 + $nowET.Minute

if ($nowMin -lt $targetMin) {
    Write-Host ""
    Write-Host "=== Holding $Shares x $Symbol - selling at $ExitHour`:$($ExitMin.ToString('00')) ET ==="
    Send-TradeNotification -Event "info" -Symbol $Symbol -Shares $Shares -Message ("Holding until scheduled sell at " + $ExitHour + ":" + $ExitMin.ToString("00") + " ET.`nEstimated quantity: " + $EstimatedQuantity + " shares`nTracked cost: `$" + $BuyCost.ToString("0.00") + " CAD`nAll-time realized P/L: " + (Format-Money $AllTimePnlBefore) + " CAD")
    while ($true) {
        $nowET = Get-NowEt
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
$quotePrice = Get-QuotePrice -Symbol $Symbol
$planningMessage = "Next move: planning to sell all available $Symbol."
if ($quotePrice) {
    $estimatedSellValue = $EstimatedQuantity * $quotePrice
    $estimatedPnl = $estimatedSellValue - $BuyCost
    $planningMessage += "`nEstimated quantity: $EstimatedQuantity shares"
    $planningMessage += "`nCurrent quote: `$$($quotePrice.ToString('0.00')) CAD"
    $planningMessage += "`nEstimated proceeds: `$$($estimatedSellValue.ToString('0.00')) CAD"
    $planningMessage += "`nEstimated trade P/L: $(Format-Money $estimatedPnl) CAD"
    $planningMessage += "`nAll-time realized P/L before this sell: $(Format-Money $AllTimePnlBefore) CAD"
}
$sellArgs = @(
    $autoScript,
    "sell",
    "--symbol", $Symbol,
    "--sell-all"
)
Send-TradeNotification -Event "sell_preparing" -Symbol $Symbol -Shares $Shares -Message $planningMessage
$sellRun = Invoke-Automation -CommandArgs $sellArgs

if ($sellRun.exit -ne 0) {
    Send-TradeNotification -Event "error" -Symbol $Symbol -Shares $Shares -Message "Sell automation failed."
    Write-Error "Sell automation failed (exit $($sellRun.exit))."
    exit $sellRun.exit
}

$sellResult = $sellRun.result
$sellQuantity = ConvertTo-NullableDouble $sellResult.estimated_quantity
if ($null -eq $sellQuantity) { $sellQuantity = $EstimatedQuantity }
$sellValue = ConvertTo-NullableDouble $sellResult.estimated_value
$tradePnl = $null
$allTimeAfter = $AllTimePnlBefore
if ($null -ne $sellValue -and $null -ne $BuyCost) {
    $tradePnl = $sellValue - $BuyCost
    Add-RealizedPnl -Symbol $Symbol -BuyCost $BuyCost -SellValue $sellValue -Quantity $sellQuantity
    $allTimeAfter = Get-AllTimePnl
}

Remove-Item $posFile -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "[OK] SELL submitted: all available $Symbol. Position closed."
$msg = "Sell-all automation submitted the order."
if ($null -ne $sellValue) { $msg += "`nEstimated proceeds: `$$($sellValue.ToString('0.00')) CAD" }
if ($null -ne $tradePnl) { $msg += "`nEstimated trade P/L: $(Format-Money $tradePnl) CAD" }
$msg += "`nAll-time realized P/L: $(Format-Money $allTimeAfter) CAD"
Send-TradeNotification -Event "sell_submitted" -Symbol $Symbol -Shares $Shares -Message $msg
Write-Host "     Check data\screen_sell_done.png to verify."
