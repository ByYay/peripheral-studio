import unittest
from unittest.mock import patch

from desktop.actions import open_url


class OpenUrlTests(unittest.TestCase):
    def test_web_addresses_open_in_the_default_browser(self):
        with patch("desktop.actions.os.startfile", create=True) as startfile:
            open_url("https://example.org/path?q=1")
        startfile.assert_called_once_with("https://example.org/path?q=1")

    def test_other_schemes_credentials_and_malformed_addresses_never_reach_windows(self):
        for url in ("file:///C:/Windows/System32/calc.exe", "javascript:alert(1)", "ms-settings:privacy",
                    "https://user:secret@example.org/", "https://example.org/a b", "https://example.org:99999/",
                    "https:///no-host"):
            with self.subTest(url=url), patch("desktop.actions.os.startfile", create=True) as startfile:
                with self.assertRaises(ValueError):
                    open_url(url)
                startfile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
