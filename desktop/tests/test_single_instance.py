import sys
import tempfile
import threading
import unittest

from desktop.single_instance import SingleInstance


@unittest.skipUnless(sys.platform == "win32", "named Windows events")
class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_wakes_the_first(self):
        with tempfile.TemporaryDirectory() as folder:
            first = SingleInstance(folder, "simulation")
            self.assertTrue(first.first)
            shown = threading.Event()
            first.listen(shown.set)
            second = SingleInstance(folder, "simulation")
            self.assertFalse(second.first)
            second.notify_running()
            self.assertTrue(shown.wait(2))
            # Another data folder or mode is another app.
            self.assertTrue(SingleInstance(folder, "hardware").first)


if __name__ == "__main__":
    unittest.main()
