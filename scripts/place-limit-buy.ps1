param(
    [Parameter(Mandatory = $true)]
    [double]$Balance,

    [Parameter(Mandatory = $true)]
    [double]$Price,

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# ── Derived values ────────────────────────────────────────────────────────────
$Shares = [math]::Floor($Balance / $Price)
if ($Shares -lt 1) {
    Write-Error "Balance $$Balance is not enough to buy one share at $$Price."
    exit 1
}
Write-Host "Balance: `$$Balance  |  Price: `$$Price  |  Max shares: $Shares"

if ($DryRun) {
    Write-Host "[DryRun] Would fill Limit Buy: $Shares shares at `$$Price. No clicks sent."
    exit 0
}

# ── Win32 helpers ─────────────────────────────────────────────────────────────
Add-Type -AssemblyName System.Windows.Forms
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class WSMouse {
    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")]
    public static extern void mouse_event(int flags, int dx, int dy, int data, int extra);
}
"@

function Click([int]$x, [int]$y, [int]$pauseMs = 300) {
    [WSMouse]::SetCursorPos($x, $y) | Out-Null
    Start-Sleep -Milliseconds 150
    [WSMouse]::mouse_event(0x0002, 0, 0, 0, 0)
    [WSMouse]::mouse_event(0x0004, 0, 0, 0, 0)
    Start-Sleep -Milliseconds $pauseMs
}

function ClearAndType([string]$text) {
    [System.Windows.Forms.SendKeys]::SendWait("^a")
    Start-Sleep -Milliseconds 80
    [System.Windows.Forms.SendKeys]::SendWait("{BACKSPACE}")
    Start-Sleep -Milliseconds 80
    [System.Windows.Forms.SendKeys]::SendWait($text)
    Start-Sleep -Milliseconds 150
}

# ── Screen coordinates ────────────────────────────────────────────────────────
# Calibrated for Firefox maximised on the right half of a 1920x1080 display,
# Wealthsimple stock-quote page already open with the Buy tab visible.
# If your window layout differs, adjust these values by running with -DryRun
# first and noting where each element actually sits.

$X = 1285   # horizontal centre of the Buy/Sell trading panel

# Row positions (Y) within the trading panel — Market order layout:
#   Buy|Sell tabs      ~236
#   Order type row     ~302  ← dropdown that currently reads "Market / Buy"
#   Buy in / Shares    ~358  ← shifts down by ~45 px after switching to Limit
#   Commissions text   ~410
#   Next button        ~460
#   Select account     ~498

$YOrderTypeDropdown = 302   # click opens the order-type picker
$YLimitOption       = 338   # "Limit" row inside the order-type picker
$YLimitPriceField   = 358   # limit-price text field (appears after Limit is chosen)
$YSharesField       = 405   # shares quantity field (shifted down ~45 px vs Market)
$YSelectAccount     = 498   # "Select account to continue" link
$YFirstUnregistered = 545   # first item in the account picker (Personal / unregistered)
$YNextButton        = 480   # "Next" button — re-check after account is selected

# ── Step 1: Switch order type to Limit ────────────────────────────────────────
Write-Host "1/5  Switching to Limit order…"
Click $X $YOrderTypeDropdown 600   # open dropdown
Click $X $YLimitOption 500         # select Limit

# ── Step 2: Enter limit price ─────────────────────────────────────────────────
Write-Host "2/5  Entering limit price `$$Price…"
Click $X $YLimitPriceField 200
ClearAndType ("{0:F2}" -f $Price)

# ── Step 3: Enter number of shares ────────────────────────────────────────────
Write-Host "3/5  Entering $Shares shares…"
Click $X $YSharesField 200
ClearAndType "$Shares"

# ── Step 4: Select first unregistered account ──────────────────────────────────
Write-Host "4/5  Opening account picker…"
Click $X $YSelectAccount 800       # open account list
Click $X $YFirstUnregistered 500   # click first item (Personal / unregistered)

# ── Step 5: Click Next ────────────────────────────────────────────────────────
Write-Host "5/5  Clicking Next…"
Click $X $YNextButton 300

Write-Host ""
Write-Host "Done. Review the pre-filled order in Wealthsimple before you confirm."
