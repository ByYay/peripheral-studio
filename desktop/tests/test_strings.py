import unittest

from desktop.strings import TEXTS, text


class StringsTests(unittest.TestCase):
    def test_turkish_covers_every_native_text(self):
        self.assertEqual(set(TEXTS["tr"]), set(TEXTS["en"]))

    def test_unknown_language_falls_back_to_english(self):
        self.assertEqual(text("de", "tray.quit"), "Quit")

    def test_battery_notification_formats(self):
        self.assertEqual(text("en", "battery.message", name="Aerox", level=15), "Aerox: 15% battery remaining.")
        self.assertEqual(text("tr", "battery.message", name="Aerox", level=15), "Aerox: kalan pil %15.")

    def test_script_header_keeps_literal_path_braces(self):
        header = text("tr", "script.header", path=r"C:\{x}\a.py", sha256="0" * 64)
        self.assertIn(r"C:\{x}\a.py", header)


if __name__ == "__main__":
    unittest.main()
