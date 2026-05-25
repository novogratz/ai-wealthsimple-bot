from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TELEGRAM_API = "https://api.telegram.org"
ROOT = Path(__file__).resolve().parents[1]


class TelegramConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_id: str

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        load_dotenv(ROOT / ".env")
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            raise TelegramConfigError(
                "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable Telegram notifications."
            )
        return cls(bot_token=token, chat_id=chat_id)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def send_message(
    text: str,
    config: TelegramConfig | None = None,
    timeout: float = 10.0,
    opener: Callable[..., object] = urlopen,
) -> None:
    cfg = config or TelegramConfig.from_env()
    url = f"{TELEGRAM_API}/bot{cfg.bot_token}/sendMessage"
    payload = urlencode(
        {
            "chat_id": cfg.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = Request(url, data=payload, method="POST")

    try:
        with opener(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram API rejected the message: HTTP {exc.code} {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Telegram notification failed: {exc.reason}") from exc

    data = json.loads(body)
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API returned an error: {data}")


def trade_message(
    event: str,
    symbol: str | None = None,
    shares: int | None = None,
    price: float | None = None,
    message: str | None = None,
    timestamp: datetime | None = None,
) -> str:
    labels = {
        "scan_candidates": "🔍 <b>Potential purchase candidates</b>",
        "scan_top": "💎 <b>Top scan candidate</b>",
        "buy_preparing": "📝 <b>Preparing buy ticket</b>",
        "buy_review": "👀 <b>Buy ticket ready for review</b>",
        "buy_submitted": "🟢 <b>Buy order submitted</b>",
        "sell_preparing": "📝 <b>Preparing sell ticket</b>",
        "sell_review": "👀 <b>Sell ticket ready for review</b>",
        "sell_submitted": "🔴 <b>Sell order submitted</b>",
        "error": "⚠️ <b>Trading assistant error</b>",
        "info": "ℹ️ <b>Trading assistant update</b>",
    }
    
    # Custom headers for big moves if the message contains win/loss keywords
    custom_header = ""
    if message:
        if "PnL: +$" in message or "BIG WIN" in message.upper():
            custom_header = "🚀 💰 <b>BIG WIN!</b> 💰 🚀\n\n"
        elif "PnL: -$" in message or "BIG LOSE" in message.upper():
            custom_header = "📉 ⚠️ <b>BIG LOSE!</b> ⚠️ 📉\n\n"

    label = labels.get(event, f"<b>{event.replace('_', ' ').title()}</b>")
    when = (timestamp or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")

    lines = [f"{custom_header}{label}", f"⏰ Time: {when}"]
    
    if symbol:
        lines.append(f"🎫 Symbol: <code>{symbol}</code>")
    if shares is not None:
        lines.append(f"🔢 Shares: {shares}")
    if price is not None:
        lines.append(f"💵 Reference price: ${price:.2f} CAD")
    
    if message:
        lines.append("")
        # Replace Markdown bold with HTML bold if any remains
        clean_message = message.replace("**", "<b>").replace("**", "</b>")
        lines.append(f"💬 {clean_message}")
        
    return "\n".join(lines)
