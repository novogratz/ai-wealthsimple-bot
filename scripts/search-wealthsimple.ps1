param(
    [Parameter(Mandatory = $true)]
    [string]$Symbol,

    [switch]$NoEnter,

    [switch]$ClickFirstResult
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public class WealthsimpleMouse {
    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int x, int y);

    [DllImport("user32.dll")]
    public static extern void mouse_event(int flags, int dx, int dy, int data, int extra);
}
"@

# Coordinates for the Wealthsimple home-page global search icon in the left nav
# at the current browser/window layout.
[WealthsimpleMouse]::SetCursorPos(938, 201) | Out-Null
Start-Sleep -Milliseconds 200
[WealthsimpleMouse]::mouse_event(0x0002, 0, 0, 0, 0)
[WealthsimpleMouse]::mouse_event(0x0004, 0, 0, 0, 0)

Start-Sleep -Milliseconds 700
[System.Windows.Forms.SendKeys]::SendWait("^a")
Start-Sleep -Milliseconds 100
[System.Windows.Forms.SendKeys]::SendWait("{BACKSPACE}")
Start-Sleep -Milliseconds 100
[System.Windows.Forms.SendKeys]::SendWait($Symbol)
Start-Sleep -Milliseconds 1500
if (-not $NoEnter) {
    [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
}

if ($ClickFirstResult) {
    Start-Sleep -Milliseconds 1200
    # First result row in the Wealthsimple global search dropdown.
    [WealthsimpleMouse]::SetCursorPos(1170, 349) | Out-Null
    Start-Sleep -Milliseconds 150
    [WealthsimpleMouse]::mouse_event(0x0002, 0, 0, 0, 0)
    [WealthsimpleMouse]::mouse_event(0x0004, 0, 0, 0, 0)
}

Write-Host "Searched Wealthsimple home for $Symbol"
