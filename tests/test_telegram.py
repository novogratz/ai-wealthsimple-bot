import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs

from fashion_bot.telegram import TelegramConfig, load_dotenv, send_message, trade_message


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps({"ok": True}).encode("utf-8")


class TelegramTests(unittest.TestCase):
    def test_trade_message_contains_order_context(self):
        text = trade_message("buy_preparing", symbol="LSPD.TO", price=12.34, message="Review mode")

        self.assertIn("Preparing buy ticket", text)
        self.assertIn("Symbol: LSPD.TO", text)
        self.assertIn("Reference price: $12.34 CAD", text)
        self.assertIn("Review mode", text)

    def test_send_message_posts_to_telegram(self):
        calls = []

        def fake_opener(request, timeout):
            calls.append((request, timeout))
            return FakeResponse()

        send_message(
            "hello",
            config=TelegramConfig(bot_token="token", chat_id="@channel"),
            opener=fake_opener,
        )

        self.assertEqual(len(calls), 1)
        request, timeout = calls[0]
        self.assertIn("/bottoken/sendMessage", request.full_url)
        self.assertEqual(timeout, 10.0)
        body = parse_qs(request.data.decode("utf-8"))
        self.assertEqual(body["chat_id"], ["@channel"])
        self.assertEqual(body["text"], ["hello"])

    def test_load_dotenv_does_not_override_existing_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "TELEGRAM_BOT_TOKEN=from_file\n"
                "TELEGRAM_CHAT_ID='@from_file'\n",
                encoding="utf-8",
            )
            os.environ["TELEGRAM_BOT_TOKEN"] = "already_set"
            try:
                load_dotenv(env_path)
                self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], "already_set")
                self.assertEqual(os.environ["TELEGRAM_CHAT_ID"], "@from_file")
            finally:
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
                os.environ.pop("TELEGRAM_CHAT_ID", None)


if __name__ == "__main__":
    unittest.main()
