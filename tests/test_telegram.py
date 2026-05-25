import json
import unittest
from urllib.parse import parse_qs

from fashion_bot.telegram import TelegramConfig, send_message, trade_message


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


if __name__ == "__main__":
    unittest.main()
