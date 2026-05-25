# Wealthsimple Guarded Trading Assistant

This project scans a configurable Canadian ticker universe, ranks intraday candidates, and can prepare a Wealthsimple browser order ticket for manual review.

Important: by default, browser automation stops at the Wealthsimple review screen. It only clicks the final submit button when you explicitly pass `--confirm`.

## Safety Model

- `data/ws_auth.json` stores your Wealthsimple browser session and is ignored by git.
- Screenshots, paper fills, open-position state, and market-data caches live under `data/` and are ignored by git.
- The scanner can pick a ticker, but you are responsible for reviewing any real-money order.
- `--confirm` submits a live order. Do not use it unless you intend to place the trade.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install firefox
```

## First-Time Browser Login

```powershell
python scripts/wealthsimple_auto.py setup
```

Log in to Wealthsimple in the Firefox window that opens, navigate to your home page, then press ENTER in the terminal. The session is saved to `data/ws_auth.json`.

## Scan

```powershell
python -m fashion_bot scan --cash 17.24
```

This ranks Canadian tickers from `config/universe.csv`.

## Paper Trade

```powershell
python -m fashion_bot paper --cash 17.24
```

The paper trader opens only during the configured entry window and exits on stop loss, take profit, trailing stop, or force-exit time.

## Browser Review Flow

Run a scan only:

```powershell
.\scripts\run-trade.ps1 -Balance 17.24 -DryRun
```

Prepare a buy ticket for the top scan result. The current buy flow uses Wealthsimple's `Dollars -> Max` control on the selected Non-registered account:

```powershell
.\scripts\run-trade.ps1 -Balance 17.24 -BuyOnly
```

Prepare a sell ticket for the saved position:

```powershell
.\scripts\run-trade.ps1 -SellOnly
```

Submit live orders only when you explicitly opt in:

```powershell
.\scripts\run-trade.ps1 -Balance 17.24 -Confirm
```

You can also call the browser automation directly:

```powershell
python scripts/wealthsimple_auto.py buy --symbol LSPD.TO --max-dollars --keep-open
python scripts/wealthsimple_auto.py sell --symbol LSPD.TO --shares 1 --keep-open
```

Add `--confirm` to either command only when you intend to submit a live Wealthsimple order.

## Telegram Notifications

Create a Telegram bot with BotFather, add it to your channel, and set these environment variables locally:

```powershell
$env:TELEGRAM_BOT_TOKEN = "<bot_token_from_botfather>"
$env:TELEGRAM_CHAT_ID = "@your_channel_username"
```

For a private channel, use the numeric chat ID instead of `@your_channel_username`.

Test the notification path:

```powershell
python -m fashion_bot notify --event info --message "Trading assistant connected"
```

The main runner sends Telegram updates for scan picks, preparing buy/sell tickets, review-ready tickets, submitted orders when `--confirm` is used, and automation errors. Missing Telegram config does not stop trading runs.

## Tests

```powershell
python -m unittest discover -s tests
```

## Configuration

Edit `config/settings.toml` for risk limits and session timing. Edit `config/universe.csv` to change the Canadian ticker universe.
