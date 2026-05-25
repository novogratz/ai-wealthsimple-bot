param(
    [Parameter(Mandatory = $true)]
    [int]$Shares,

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

Write-Host "Action: Sell all (or $Shares shares) x Market"

if ($DryRun) {
    Write-Host "[DryRun] Would fill Market Sell for $Shares shares. No clicks sent."
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
# Calibrated for Firefox maximised on the right half of a 1920x1080 display.
# Assumptions: You are already on the stock page.

$X = 1285   # horizontal centre of the Buy/Sell trading panel

# Y Positions for Sell Tab (Market Order)
$YSellTab           = 236
$YMaxButton         = 405   # Wealthsimple often has a "Max" button for selling
$YSharesField       = 405   # Same field as max if typing manually
$YSelectAccount     = 498
$YFirstUnregistered = 545
$YNextButton        = 480

# ── Step 1: Switch to Sell tab ────────────────────────────────────────────────
Write-Host "1/4  Switching to Sell tab…"
Click 1400 $YSellTab 600   # Click "Sell" (usually right side of the tab bar)

# ── Step 2: Select Max Shares ─────────────────────────────────────────────────
Write-Host "2/4  Selecting Max shares…"
# We click the right edge of the shares field where the "Max" button usually is
Click 1450 $YMaxButton 500 

# ── Step 3: Select account ────────────────────────────────────────────────────
Write-Host "3/4  Opening account picker…"
Click $X $YSelectAccount 800
Click $X $YFirstUnregistered 500

# ── Step 4: Click Next ────────────────────────────────────────────────────────
Write-Host "4/4  Clicking Next…"
Click $X $YNextButton 300

Write-Host ""
Write-Host "Done. Review the pre-filled SELL order before you confirm."
