import unittest
from unittest.mock import MagicMock

from scripts.wealthsimple_auto import is_login_page


class WealthsimpleSessionTests(unittest.TestCase):
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
