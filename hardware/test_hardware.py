import unittest
from unittest.mock import Mock, patch
from hardware import DeviceError, PeripheralAPI


class HardwareTests(unittest.TestCase):
    def test_rgb_maps_to_upstream_zone_methods(self):
        for label, method in [('top_color', 'set_z1_color'), ('middle_color', 'set_z2_color'), ('bottom_color', 'set_z3_color')]:
            self.assertEqual(PeripheralAPI._validate(label, '#ff0000'), method)
        with self.assertRaises(ValueError):
            PeripheralAPI._validate('dpi', 801)

    def test_default_blocks_valid_write_before_open(self):
        api = PeripheralAPI()
        with patch.object(api, '_open') as opened:
            with self.assertRaisesRegex(DeviceError, 'hardware_writes_disabled'):
                api.set_setting('aerox-3', 'dpi', 800)
            opened.assert_not_called()

    def test_invalid_settings_never_open_hardware(self):
        api = PeripheralAPI()
        with patch.object(api, '_open') as opened:
            for key, value in [('dpi', True), ('dpi', 99), ('polling_rate', 2000),
                               ('top_color', 'red'), ('script', 'anything')]:
                with self.assertRaises((ValueError, DeviceError)):
                    api.set_setting('aerox-3', key, value)
            opened.assert_not_called()

    def test_disconnect_closes_handle(self):
        api, mouse = PeripheralAPI(allow_unverified_writes=True), Mock()
        mouse.set_sensitivity.side_effect = OSError('disconnected')
        with patch.object(api, '_open', return_value=mouse):
            with self.assertRaises(OSError):
                api.set_setting('aerox-3', 'dpi', 800)
        mouse.close.assert_called_once()

    def test_unknown_battery_is_not_zero(self):
        api, mouse = PeripheralAPI(allow_unverified_writes=True), Mock()
        mouse.battery = {'level': None, 'is_charging': None}
        with patch.object(api, '_open', return_value=mouse):
            self.assertIsNone(api.status('aerox-3')['battery_percent'])

    def test_unsupported_backend(self):
        with self.assertRaises(DeviceError):
            PeripheralAPI().status('mx-keys-mini')

    def test_write_is_not_readback(self):
        api, mouse = PeripheralAPI(allow_unverified_writes=True), Mock()
        with patch.object(api, '_open', return_value=mouse):
            self.assertFalse(api.set_setting('aerox-3', 'dpi', 800)['readback_verified'])
        mouse.set_sensitivity.assert_called_once_with(800)
        mouse.save.assert_not_called()


class AeroxConnectionTests(unittest.TestCase):
    def open_with(self, product_ids):
        devices = Mock()
        devices.list_plugged_devices.return_value = [{"vendor_id": 0x1038, "product_id": pid} for pid in product_ids]
        mouse = Mock()
        modules = {"rivalcfg.devices": devices, "rivalcfg.mouse": mouse}
        with patch("hardware.importlib.import_module", side_effect=modules.__getitem__):
            PeripheralAPI()._open("aerox-3")
        return mouse.get_mouse.call_args.kwargs["product_id"]

    def test_cable_wins_over_the_receiver_while_both_are_plugged_in(self):
        self.assertEqual(self.open_with([0x1838]), 0x1838)  # receiver only
        self.assertEqual(self.open_with([0x1838, 0x183A]), 0x183A)  # charging cable: wired mode
        with self.assertRaises(DeviceError):
            self.open_with([0x1838, 0x1878])  # two receivers: ambiguous

if __name__ == '__main__':
    unittest.main()
