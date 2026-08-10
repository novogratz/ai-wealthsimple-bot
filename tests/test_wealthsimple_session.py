import unittest
from unittest.mock import MagicMock

from scripts.wealthsimple_auto import _nonregistered_usd_balances, is_login_page


class WealthsimpleSessionTests(unittest.TestCase):
    def test_parses_current_balance_before_account_layout(self):
        text = "$2.01 CAD · $0.03 USD\nNon-registered\n$0.00 CAD · $118.00 USD\nNon-registered"
        self.assertEqual(_nonregistered_usd_balances(text), [0.03, 118.0])

    def test_balance_parser_excludes_registered_accounts(self):
        text = "$0.00 CAD · $118.00 USD\nNon-registered\n$0.00 CAD · $999.00 USD\nRRSP"
        self.assertEqual(_nonregistered_usd_balances(text), [118.0])

    def test_login_url_is_detected_without_password_field(self):
        page = MagicMock()
        page.url = "https://my.wealthsimple.com/app/login?redirect=home"
        self.assertTrue(is_login_page(page))
        page.locator.assert_not_called()

    def test_authenticated_home_is_not_login(self):
        page = MagicMock()
        page.url = "https://my.wealthsimple.com/app/home"
        page.locator.return_value.first.is_visible.return_value = False
        self.assertFalse(is_login_page(page))


if __name__ == "__main__":
    unittest.main()
