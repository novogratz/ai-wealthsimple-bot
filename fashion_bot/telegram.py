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
        "scan_candidates": "Potential purchase candidates",
        "scan_top": "Top scan candidate",
        "buy_preparing": "Preparing buy ticket",
        "buy_review": "Buy ticket ready for review",
        "buy_submitted": "Buy order submitted",
        "sell_preparing": "Preparing sell ticket",
        "sell_review": "Sell ticket ready for review",
        "sell_submitted": "Sell order submitted",
        "error": "Trading assistant error",
        "info": "Trading assistant update",
    }
    label = labels.get(event, event.replace("_", " ").title())
    when = (timestamp or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")

    lines = [f"{label}", f"Time: {when}"]
    if symbol:
        lines.append(f"Symbol: {symbol}")
    if shares is not None:
        lines.append(f"Shares: {shares}")
    if price is not None:
        lines.append(f"Reference price: ${price:.2f} CAD")
    if message:
        lines.append(message)
    return "\n".join(lines)
