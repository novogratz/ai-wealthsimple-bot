param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("BUY", "SELL")]
    [string]$Action,

    [Parameter(Mandatory = $true)]
    [string]$Symbol,

    [Parameter(Mandatory = $true)]
    [int]$Shares,

    [Parameter(Mandatory = $true)]
    [double]$ReferencePrice,

    [string]$Reason = "No reason provided",

    [switch]$NoBrowser,

    [switch]$Speak
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $root "data"
$ticketPath = Join-Path $dataDir "review-ticket.html"

New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

$estimatedCost = [math]::Round($Shares * $ReferencePrice, 2)
$safeSymbol = [System.Net.WebUtility]::HtmlEncode($Symbol.ToUpperInvariant())
$safeAction = [System.Net.WebUtility]::HtmlEncode($Action.ToUpperInvariant())
$safeReason = [System.Net.WebUtility]::HtmlEncode($Reason)
$safeTime = [System.Net.WebUtility]::HtmlEncode((Get-Date).ToString("yyyy-MM-dd HH:mm:ss zzz"))

$html = @"
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trade Review Ticket</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Arial, Helvetica, sans-serif;
      background: #f4f6f8;
      color: #111827;
    }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
    }
    main {
      width: min(760px, 100%);
      background: white;
      border: 2px solid #111827;
      border-radius: 8px;
      padding: 28px;
      box-sizing: border-box;
    }
    h1 {
      margin: 0 0 18px;
      font-size: 34px;
      line-height: 1.15;
    }
    .grid {
      display: grid;
      gap: 14px;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    }
    .field {
      border: 1px solid #c7d0da;
      border-radius: 6px;
      padding: 16px;
      min-height: 96px;
    }
    .label {
      font-size: 16px;
      color: #4b5563;
      margin-bottom: 8px;
    }
    .value {
      font-size: 30px;
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .reason {
      margin-top: 16px;
      font-size: 20px;
      line-height: 1.45;
    }
    .warning {
      margin-top: 22px;
      padding: 16px;
      border: 2px solid #b91c1c;
      color: #7f1d1d;
      background: #fff1f2;
      border-radius: 6px;
      font-size: 20px;
      line-height: 1.35;
    }
  </style>
</head>
<body>
  <main>
    <h1>Manual Trade Review</h1>
    <div class="grid">
      <section class="field">
        <div class="label">Action</div>
        <div class="value">$safeAction</div>
      </section>
      <section class="field">
        <div class="label">Symbol</div>
        <div class="value">$safeSymbol</div>
      </section>
      <section class="field">
        <div class="label">Shares</div>
        <div class="value">$Shares</div>
      </section>
      <section class="field">
        <div class="label">Reference Price</div>
        <div class="value">`$$("{0:N2}" -f $ReferencePrice)</div>
      </section>
      <section class="field">
        <div class="label">Estimated Cost</div>
        <div class="value">`$$("{0:N2}" -f $estimatedCost)</div>
      </section>
      <section class="field">
        <div class="label">Created</div>
        <div class="value" style="font-size:22px">$safeTime</div>
      </section>
    </div>
    <div class="reason"><strong>Reason:</strong> $safeReason</div>
    <div class="warning">
      This assistant has not placed an order. Review the ticket yourself before taking any action in Wealthsimple.
    </div>
  </main>
</body>
</html>
"@

Set-Content -Path $ticketPath -Value $html -Encoding UTF8

$spoken = "Manual trade review. $Action $Shares shares of $Symbol. Reference price $ReferencePrice dollars. Estimated cost $estimatedCost dollars. Reason: $Reason. No order has been placed."

if ($Speak) {
    Add-Type -AssemblyName System.Speech
    $speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $speaker.Rate = -1
    $speaker.Volume = 100
    $speaker.Speak($spoken)
}

Start-Process $ticketPath

if (-not $NoBrowser) {
    Start-Process "https://my.wealthsimple.com/app/home"
    if ($Symbol.ToUpperInvariant().EndsWith(".TO")) {
        $wealthsimpleSymbol = $Symbol.ToLowerInvariant().Replace(".to", "")
        Start-Process "https://www.wealthsimple.com/en-ca/quote/tsx/$wealthsimpleSymbol"
    }
}

Write-Host "Created review ticket: $ticketPath"
Write-Host $spoken
