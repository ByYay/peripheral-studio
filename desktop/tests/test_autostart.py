import sys
import unittest
import uuid

from desktop.autostart import APPROVED, Autostart


@unittest.skipUnless(sys.platform == "win32", "Windows registry")
class AutostartTests(unittest.TestCase):
    def setUp(self):
        import winreg
        self.winreg = winreg
        # A throwaway key: the real Windows startup list is never touched.
        self.root = rf"Software\UnifiedPeripheralManagerTests\{uuid.uuid4().hex}"
        self.run, self.approved = self.root + r"\Run", self.root + r"\Approved"
        self.addCleanup(self.remove_keys)
        self.autostart = Autostart(r"C:\Apps\UPM.exe", self.run, self.approved)

    def remove_keys(self):
        for key in (self.run, self.approved, self.root, r"Software\UnifiedPeripheralManagerTests"):
            try:
                self.winreg.DeleteKey(self.winreg.HKEY_CURRENT_USER, key)
            except FileNotFoundError:
                pass
            except PermissionError:
                pass  # another test run still has keys under the parent

    def value(self, key):
        try:
            with self.winreg.OpenKey(self.winreg.HKEY_CURRENT_USER, key) as handle:
                return self.winreg.QueryValueEx(handle, "UnifiedPeripheralManager")[0]
        except FileNotFoundError:
            return None

    def write(self, key, value, kind):
        with self.winreg.CreateKey(self.winreg.HKEY_CURRENT_USER, key) as handle:
            self.winreg.SetValueEx(handle, "UnifiedPeripheralManager", 0, kind, value)

    def test_turning_on_and_off_writes_and_removes_the_run_entry(self):
        self.assertFalse(self.autostart.enabled())
        self.autostart.set(True)
        self.assertEqual(self.value(self.run), r'"C:\Apps\UPM.exe"')
        self.assertTrue(self.autostart.enabled())
        self.autostart.set(False)
        self.assertIsNone(self.value(self.run))
        self.assertFalse(self.autostart.enabled())

    def test_turned_off_in_task_manager_shows_off_and_can_be_turned_on_again(self):
        self.autostart.set(True)
        self.write(self.approved, b"\x03" + bytes(11), self.winreg.REG_BINARY)  # Task Manager: disabled
        self.assertFalse(self.autostart.enabled())
        self.autostart.set(True)
        self.assertEqual(self.value(self.approved), APPROVED)
        self.assertTrue(self.autostart.enabled())
        self.autostart.set(False)
        self.assertIsNone(self.value(self.approved))

    def test_moved_app_updates_its_entry(self):
        self.write(self.run, r'"D:\Old\UPM.exe"', self.winreg.REG_SZ)
        self.autostart.refresh()
        self.assertEqual(self.value(self.run), r'"C:\Apps\UPM.exe"')

    def test_source_checkout_cannot_register(self):
        unavailable = Autostart(None, self.run, self.approved)
        self.assertFalse(unavailable.available)
        self.assertFalse(unavailable.enabled())
        with self.assertRaises(RuntimeError):
            unavailable.set(True)
        unavailable.refresh()
        self.assertIsNone(self.value(self.run))


if __name__ == "__main__":
    unittest.main()
