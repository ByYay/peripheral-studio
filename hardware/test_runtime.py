import struct
import threading
import time
import unittest
from unittest.mock import Mock, patch
from hardware.runtime import HardwareManager, LIGHTING_NOT_KEPT
from hardware.hidpp import HidppReader, estimate_battery_percent
from hardware import DeviceError


class FakeG733:
    """HID++ double: two LED zones with effects [Disabled, Static, Breathe, Cycle] and optional overriding controller."""

    EFFECTS = [0x0, 0x1, 0xA, 0x3]

    def __init__(self, override=None):
        self.states = [bytes([1, 0x27, 0xdc, 0xe8]), bytes([1, 0x4a, 0xd6, 0xe0])]
        self.override = override
        self.writes = []

    def feature(self, feature_id):
        return {0x8070: 4, 0x1F20: 8}.get(feature_id, 0)

    def adc_battery(self):
        return 29, False, 3747

    def query(self, index, function=0, parameters=b''):
        if function == 0x00:
            return bytes([2, 0, 1, 0, 3])
        if function == 0x10:
            return bytes([parameters[0], 0, 1, len(self.EFFECTS)])
        if function == 0x20:
            return bytes([parameters[0], parameters[1], 0, self.EFFECTS[parameters[1]], 0, 0, 0, 0])
        if function == 0x30:
            self.writes.append(bytes(parameters))
            self.states[parameters[0]] = self.override or bytes(parameters[1:12]).ljust(11, b'\0')
            return b''
        if function == 0xE0:
            return bytes([parameters[0]]) + self.states[parameters[0]].ljust(11, b'\0') + bytes(5)
        raise AssertionError(function)


class FakeKeyboard:
    """BACKLIGHT2 double: automatic mode picks ambient level 2; manual keeps the written level."""

    def __init__(self):
        self.enabled, self.options, self.level = 1, 0x0D, 2

    def feature(self, feature_id):
        return 11 if feature_id == 0x1982 else 0

    def query(self, index, function=0, parameters=b''):
        if function == 0x00:
            return struct.pack('<BBBHBHHH', self.enabled, self.options, 0x3d, 1, self.level, 6, 6, 60)
        if function == 0x20:
            return bytes([8, 2, 2, 0])
        if function == 0x10:
            enabled, options, _, level = struct.unpack('<BBBB', parameters[:4])
            self.enabled, self.options = enabled, options
            self.level = level if (options >> 3) & 3 == 3 else 2
            return b''
        raise AssertionError(function)


class FakeReprog:
    """REPROG_CONTROLS_V4 (index 9) and FN_INVERSION (index 12) double; asleep requests time out."""

    def __init__(self, diverted=()):
        self.flags = {cid: 1 for cid in diverted}
        self.asleep = False
        self.fn = 1

    def feature(self, feature_id):
        if self.asleep:
            raise TimeoutError
        return {0x1B04: 9, 0x40A3: 12, 0x1D4B: 4}.get(feature_id, 0)

    def query(self, index, function=0, parameters=b''):
        if self.asleep:
            raise TimeoutError
        if index == 12:
            return bytes([0, self.fn, 1, 1])
        cid = int.from_bytes(parameters[:2], 'big')
        if function == 0x20:
            return parameters[:2] + bytes([self.flags.get(cid, 0), 0, 0])
        if function == 0x30 and parameters[2] & 0x02:
            self.flags[cid] = parameters[2] & 0x01
            return b''
        raise AssertionError(function)


class FakeGkey:
    """G733 GKEY (index 5) and ADC_MEASUREMENT (index 8) double; requests to a headset that is off time out."""

    def __init__(self):
        self.software, self.off, self.writes = None, False, 0

    def feature(self, feature_id):
        if self.off:
            raise TimeoutError
        return {0x8010: 5, 0x1F20: 8}.get(feature_id, 0)

    def query(self, index, function=0, parameters=b''):
        if self.off:
            raise TimeoutError
        if (index, function) == (5, 0x20):
            self.software, self.writes = parameters[0], self.writes + 1
            return parameters[:1]
        raise AssertionError((index, function))


class RuntimeTests(unittest.TestCase):
    def test_no_devices_has_no_simulation_fallback(self):
        manager = HardwareManager()
        manager._hid = Mock()
        manager._hid.enumerate.return_value = []
        devices = manager.enumerate_devices()
        self.assertEqual(len(devices), 3)
        self.assertTrue(all(d['connection'] == 'disconnected' and d['batteryPercent'] is None for d in devices))

    def test_apply_rejects_unknown_devices_and_settings(self):
        manager = HardwareManager()
        with self.assertRaises(DeviceError):
            manager.apply('unknown', {'dpi': 800})
        for device_id, settings in (('aerox-3', {'backlightMode': 'auto'}), ('g733', {'dpi': 800}),
                                    ('mx-keys-mini', {'lightingColor': '#ffffff'}), ('g733', {})):
            with self.subTest(device=device_id), self.assertRaises(DeviceError):
                manager.apply(device_id, settings)

    def manager_with(self, fake):
        manager = HardwareManager()
        manager._hid = Mock()
        manager._keyboard = 1
        patcher = patch('hardware.runtime.HidppReader', return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        manager._open = Mock(return_value=Mock())
        return manager

    def test_g733_lighting_is_written_scaled_and_verified_by_readback(self):
        fake = FakeG733()
        manager = self.manager_with(fake)
        with patch('hardware.runtime.time.sleep'):
            observed = manager.apply('g733', {'lightingColor': '#ff8000', 'lightingBrightness': 50})
        applied = {'lightingColor': '#ff8000', 'lightingBrightness': 50, 'lightingEffect': 'static'}
        self.assertEqual(observed, {'settings': applied})
        # One color for both lights, although they differed before.
        self.assertEqual(fake.writes, [bytes([0, 1, 128, 64, 0]), bytes([1, 1, 128, 64, 0])])
        # The scaled readback maps back to the requested values.
        self.assertEqual(manager._read_g733_lighting(fake), applied)
        with patch('hardware.runtime.time.sleep'):
            manager.apply('g733', {'lightingBrightness': 0})
        self.assertEqual(fake.states, [bytes(11), bytes(11)])

    def test_g733_zones_and_effects_use_the_headsets_own_effects(self):
        fake = FakeG733()
        manager = self.manager_with(fake)
        zones = {'top': '#ff0000', 'bottom': '#0000ff'}
        with patch('hardware.runtime.time.sleep'):
            manager.apply('g733', {'lightingZones': zones, 'lightingEffect': 'breathing', 'lightingBrightness': 100})
        # Zone 0 is the bottom light; breathing: color, period 4000 ms, default waveform, intensity 100.
        self.assertEqual(fake.writes, [bytes([0, 2, 0, 0, 255, 0x0f, 0xa0, 0, 100]), bytes([1, 2, 255, 0, 0, 0x0f, 0xa0, 0, 100])])
        manager._g733_lighting = None  # what another app instance would read
        self.assertEqual(manager._read_g733_lighting(fake), {'lightingEffect': 'breathing', 'lightingColor': '#ff0000',
                                                            'lightingZones': zones, 'lightingBrightness': 100,
                                                            'lightingSpeed': 'normal'})
        fake.writes.clear()
        with patch('hardware.runtime.time.sleep'):
            manager.apply('g733', {'lightingEffect': 'rainbow', 'lightingBrightness': 60})
        self.assertEqual(fake.writes[0], bytes([0, 3, 0, 0, 0, 0, 0, 0x17, 0x70, 60]))
        manager._g733_lighting = None
        self.assertEqual(manager._read_g733_lighting(fake), {'lightingEffect': 'rainbow', 'lightingBrightness': 60,
                                                            'lightingSpeed': 'normal'})
        with patch('hardware.runtime.time.sleep'):
            manager.apply('g733', {'lightingSpeed': 'slow'})
        self.assertEqual(fake.states[1][6:8], (12000).to_bytes(2, 'big'))  # a 12 s cycle

    def test_g733_scan_reports_zones_and_effects_it_supports(self):
        manager = HardwareManager()
        manager._hid = Mock()
        manager._hid.enumerate.return_value = [{'vendor_id': 0x046d, 'product_id': 0x0afe, 'usage_page': 0xff43,
                                                'usage': 0x0202, 'path': b'g733'}]
        with patch('hardware.runtime.HidppReader', return_value=FakeG733()):
            device = next(d for d in manager.enumerate_devices() if d['id'] == 'g733')
        self.assertEqual(device['limits'], {'lightingZones': ['top', 'bottom'], 'lightingEffects': ['static', 'breathing', 'rainbow'],
                                            'lightingSpeedEffects': ['breathing', 'rainbow']})
        self.assertEqual(device['settings']['lightingZones'], {'top': '#4ad6e0', 'bottom': '#27dce8'})

    def test_g733_overridden_lighting_fails_instead_of_claiming_success(self):
        manager = self.manager_with(FakeG733(override=bytes([1, 0x10, 0xe0, 0xee])))
        with patch('hardware.runtime.time.sleep'), self.assertRaisesRegex(DeviceError, 'Dynamic Lighting'):
            manager.apply('g733', {'lightingColor': '#ff0000', 'lightingBrightness': 100})
        self.assertIn('Dynamic Lighting', LIGHTING_NOT_KEPT)

    def test_backlight_manual_and_automatic_modes_are_read_back(self):
        fake = FakeKeyboard()
        manager = self.manager_with(fake)
        self.assertEqual(manager._backlight_state(fake)['settings'], {'backlightMode': 'auto', 'backlightLevel': 2})
        observed = manager.apply('mx-keys-mini', {'backlightMode': 'manual', 'backlightLevel': 7})
        self.assertEqual(observed, {'settings': {'backlightMode': 'manual', 'backlightLevel': 7}, 'ignored': []})
        observed = manager.apply('mx-keys-mini', {'backlightMode': 'auto', 'backlightLevel': 7})
        self.assertEqual(observed['settings']['backlightMode'], 'auto')
        self.assertEqual(observed['ignored'], ['backlightLevel'])
        self.assertEqual(manager.apply('mx-keys-mini', {'backlightMode': 'off'})['settings']['backlightMode'], 'off')
        with self.assertRaises(ValueError):
            manager.apply('mx-keys-mini', {'backlightMode': 'manual', 'backlightLevel': 8})

    def reprog_manager(self, fake):
        manager = self.manager_with(fake)
        manager._start_listener = Mock()
        return manager

    def test_mx_keys_are_diverted_and_given_back_to_the_keyboard(self):
        fake = FakeReprog(diverted=[0x0108])  # F7 is already diverted by another app
        manager = self.reprog_manager(fake)
        manager.set_key_diversion('F4', True)
        manager.set_key_diversion('F7', True)
        self.assertEqual((fake.flags[0x00E2], manager._wanted), (1, {'F4': False, 'F7': True}))
        manager._start_listener.assert_called()
        with patch.object(manager, '_scan', return_value=[]):
            keyboard = next(d for d in manager.enumerate_devices() if d['id'] == 'mx-keys-mini')
        self.assertEqual(keyboard['telemetry']['sharedKeys'], ['F7'])
        manager.set_key_diversion('F4', False)
        manager.set_key_diversion('F7', False)
        self.assertEqual((fake.flags[0x00E2], fake.flags[0x0108], manager._wanted), (0, 0, {}))
        with self.assertRaises(ValueError):
            manager.set_key_diversion('F1', True)

    def test_sleeping_keyboard_fails_saves_but_queues_restores(self):
        fake = FakeReprog()
        fake.asleep = True
        manager = self.reprog_manager(fake)
        with self.assertRaisesRegex(DeviceError, 'asleep'):
            manager.set_key_diversion('F5', True)
        self.assertEqual(manager._wanted, {})
        manager.set_key_diversion('F5', True, wait=False)
        self.assertTrue(manager._pending_keys())
        fake.asleep = False
        manager._sync_keys(0)  # what the listener does when the keyboard wakes up
        self.assertEqual((fake.flags[0x00E3], manager._wanted, manager._pending_keys()), (1, {'F5': False}, False))

    def test_close_gives_diverted_keys_back(self):
        fake = FakeReprog()
        manager = self.reprog_manager(fake)
        manager.set_key_diversion('INS', True)
        manager.close()
        self.assertEqual((fake.flags[0x00E9], manager._wanted), (0, {}))

    def test_listener_reports_each_press_of_wanted_keys_once(self):
        fake = FakeReprog()
        manager = self.reprog_manager(fake)
        manager.set_key_diversion('F4', True)
        pressed, stop = [], threading.Event()
        down, up = bytes([0x11, 1, 9, 0x00, 0x00, 0xE2]).ljust(20, b'\0'), bytes([0x11, 1, 9]).ljust(20, b'\0')
        other = bytes([0x11, 1, 9, 0x00, 0x00, 0xE3]).ljust(20, b'\0')  # F5 is not wanted
        reports = [down, down, up, other, up, down]

        def read(_size, timeout):
            if timeout == 1:
                return b''  # nothing stale to drain
            if not reports:
                stop.set()
                return b''
            return reports.pop(0)
        manager._open.return_value.read.side_effect = read

        def action(device, control):
            pressed.append((device, control))
            if len(pressed) == 1:
                raise KeyError('a failing action')  # must not stop the listener
        manager.watch_controls(action)
        with self.assertLogs('hardware.runtime', 'ERROR'):
            manager._listen(stop)
        self.assertEqual(pressed, [('mx-keys-mini', 'F4'), ('mx-keys-mini', 'F4')])

    def test_listener_survives_unexpected_errors(self):
        manager = self.reprog_manager(FakeReprog())
        stop = threading.Event()
        opens = []

        def open_handle(*_):
            opens.append(1)
            if len(opens) == 1:
                raise IndexError('unexpected')
            stop.set()
            raise OSError('receiver gone')
        manager._open.side_effect = open_handle
        with patch.object(stop, 'wait'), self.assertLogs('hardware.runtime', 'ERROR'):
            manager._listen(stop)
        self.assertEqual(len(opens), 2, 'the listener started again after the error')

    def test_listener_finds_a_keyboard_that_was_asleep_at_start(self):
        fake = FakeReprog()
        manager = self.reprog_manager(fake)
        manager._keyboard = None
        manager._wanted = {'F4': None}  # restored while the keyboard was asleep
        stop = threading.Event()
        wake = bytes([0x10, 1, 0x41, 0x04, 0x00, 0x00, 0x00])  # 7-byte connection notification
        reports = [wake, b'']

        def read(_size, timeout):
            if timeout == 1:
                return b''  # nothing stale to drain
            if not reports:
                stop.set()
                return b''
            return reports.pop(0)

        def scan():
            manager._keyboard = 1
        manager._open.return_value.read.side_effect = read
        with patch.object(manager, 'enumerate_devices', side_effect=scan), patch('hardware.runtime.time.sleep'):
            manager._listen(stop)
        self.assertEqual((fake.flags[0x00E2], manager._wanted), (1, {'F4': False}))

    def test_listener_diverts_again_when_the_keyboard_wakes_up(self):
        fake = FakeReprog()
        manager = self.reprog_manager(fake)
        manager.set_key_diversion('F7', True)
        stop = threading.Event()
        wake = bytes([0x11, 1, 4, 0x00, 0x01, 0x01, 0x01]).ljust(20, b'\0')  # WIRELESS_DEVICE_STATUS
        steps = ['sleep', wake, b'']

        def read(_size, timeout):
            if timeout == 1:
                return b''  # nothing stale to drain
            if not steps:
                stop.set()
                return b''
            step = steps.pop(0)
            if step == 'sleep':
                fake.flags[0x0108] = 0  # the keyboard forgot the diversion while asleep
                return b''
            return step
        manager._open.return_value.read.side_effect = read
        manager._listen(stop)
        self.assertEqual(fake.flags[0x0108], 1)

    def button_manager(self, fake):
        manager = self.manager_with(fake)
        manager._start_button_listener = Mock()
        return manager

    def test_headset_buttons_share_one_software_control_switch(self):
        fake = FakeGkey()
        manager = self.button_manager(fake)
        manager.set_headset_button('g1', True)
        manager.set_headset_button('g2', True)
        self.assertEqual((fake.software, fake.writes), (1, 1))
        manager.set_headset_button('g1', False)
        self.assertEqual(fake.software, 1)  # the power button is still assigned
        manager.set_headset_button('g2', False)
        self.assertEqual((fake.software, manager._buttons), (0, set()))
        with self.assertRaises(ValueError):
            manager.set_headset_button('volume', True)

    def test_headset_that_is_off_fails_saves_but_queues_restores(self):
        fake = FakeGkey()
        fake.off = True
        manager = self.button_manager(fake)
        with self.assertRaisesRegex(DeviceError, 'headset is off'):
            manager.set_headset_button('g1', True)
        self.assertEqual(manager._buttons, set())
        manager.set_headset_button('g1', True, wait=False)
        self.assertEqual(manager._buttons, {'g1'})
        manager._start_button_listener.assert_called_once()

    def test_button_listener_reports_presses_and_renews_control_after_power_on(self):
        fake = FakeGkey()
        manager = self.button_manager(fake)
        manager.set_headset_button('g1', True)
        stop, pressed = threading.Event(), []
        event = lambda mask: bytes([0x11, 0xFF, 5, 0x00, mask]).ljust(20, b'\0')
        battery = lambda flags: bytes([0x11, 0xFF, 8, 0x00, 0x0e, 0xa8, flags]).ljust(20, b'\0')
        steps = [event(1), event(1), event(0), event(2), event(0), battery(0), 'reset', battery(1), 'wait', b'']

        def read(_size, timeout):
            if timeout == 1:
                return b''
            if not steps:
                stop.set()
                return b''
            step = steps.pop(0)
            if step == 'reset':
                fake.software = 0  # powered off: software control ended
                return b''
            if step == 'wait':
                time.sleep(0.6)  # the listener renews control 0.5 s after power-on
                return b''
            return step
        manager._open.return_value.read.side_effect = read
        manager.watch_controls(lambda device, control: pressed.append((device, control)))
        manager._listen_buttons(stop)
        # One press of the microphone button; the power button (g2) is not assigned.
        self.assertEqual(pressed, [('g733', 'g1')])
        self.assertEqual(fake.software, 1)  # renewed after the headset was turned on again

    def test_close_gives_headset_buttons_back(self):
        fake = FakeGkey()
        manager = self.button_manager(fake)
        manager.set_headset_button('g2', True)
        manager.close()
        self.assertEqual((fake.software, manager._buttons), (0, set()))

    def test_keyboard_paired_to_another_receiver_slot_is_found_again(self):
        manager = HardwareManager()
        manager._hid = Mock()
        manager._hid.enumerate.return_value = [{'vendor_id': 0x046d, 'product_id': 0xc548, 'usage_page': 0xff00,
                                                'usage': 2, 'path': b'bolt'}]
        manager._keyboard = 2  # remembered, but a mouse uses slot 2 now
        names = {2: 'MX Master 3S', 3: 'MX Keys Mini'}

        def reader(_handle, slot):
            fake = FakeKeyboard()
            fake.name = lambda: names.get(slot) or (_ for _ in ()).throw(OSError('empty slot'))
            fake.battery = lambda: (80, False)
            return fake
        with patch('hardware.runtime.HidppReader', side_effect=reader):
            keyboard = next(d for d in manager.enumerate_devices() if d['id'] == 'mx-keys-mini')
        self.assertEqual((manager._keyboard, keyboard['connection']), (3, 'connected'))

    def test_presence_reports_devices_that_may_have_lost_their_settings(self):
        manager = HardwareManager()
        bolt, aerox_dongle, aerox_cable = (0x046d, 0xc548, b'1'), (0x1038, 0x1838, b'2'), (0x1038, 0x183A, b'3')
        headset, headset_moved = (0x046d, 0x0afe, b'4'), (0x046d, 0x0afe, b'5')
        states = iter([
            (frozenset({bolt, aerox_dongle, headset}), False, False),  # start: mouse and headset off
            (frozenset({bolt, aerox_dongle, headset}), True, False),   # mouse switched on
            (frozenset({bolt, aerox_dongle, headset}), True, True),    # headset switched on
            (frozenset({bolt, aerox_dongle, headset}), True, True),    # nothing new
            (frozenset({bolt, aerox_dongle, aerox_cable, headset}), True, True),  # mouse cable plugged in
            (frozenset({bolt, aerox_dongle, aerox_cable, headset_moved}), True, True),  # headset receiver re-listed
            (frozenset({aerox_dongle, aerox_cable, headset_moved}), True, True),  # keyboard receiver removed
        ])
        stop, changes = threading.Event(), []

        def state():
            try:
                return next(states)
            except StopIteration:
                stop.set()
                return (frozenset({aerox_dongle, aerox_cable, headset_moved}), True, True)
        with patch.object(manager, '_presence_state', side_effect=state), patch('hardware.runtime.PRESENCE_INTERVAL', 0):
            manager._watch_presence(changes.append, stop)
        self.assertEqual(changes, [{'aerox-3'}, {'g733'}, {'aerox-3'}, {'g733'}, {'mx-keys-mini'}])

    def test_fn_keys_mode_is_read_from_fn_inversion(self):
        fake = FakeReprog()
        self.assertEqual(HardwareManager._fn_keys(fake), 'special')
        fake.fn = 0
        self.assertEqual(HardwareManager._fn_keys(fake), 'standard')

    def test_writes_load_vendor_dependencies_before_any_scan(self):
        manager = HardwareManager()
        manager._writer = Mock()
        with patch.object(manager, '_load') as load:
            manager.set_aerox_dpi_button('action')
            manager.apply('aerox-3', {'dpi': 800})
        self.assertEqual(load.call_count, 2)
        manager._writer.set_setting.assert_any_call('aerox-3', 'buttons_mapping', 'buttons(button6=F23)')

    def test_aerox_writes_are_acknowledged_not_verified(self):
        manager = HardwareManager()
        manager._hid = Mock()
        manager._writer = Mock()
        observed = manager.apply('aerox-3', {'dpi': 800, 'pollingRate': 500, 'lightingColor': '#ff0000', 'lightingBrightness': 50})
        self.assertEqual(observed['verification'], 'acknowledged')
        calls = [call.args for call in manager._writer.set_setting.call_args_list]
        self.assertIn(('aerox-3', 'dpi', 800), calls)
        self.assertIn(('aerox-3', 'polling_rate', 500), calls)
        self.assertEqual([c for c in calls if c[1].endswith('_color')],
                         [('aerox-3', zone, '#800000') for zone in ('top_color', 'middle_color', 'bottom_color')])

    def test_cache_is_isolated_and_avoids_repeat_io(self):
        manager = HardwareManager()
        with patch.object(manager, '_scan', return_value=[]) as scan:
            first = manager.enumerate_devices()
            first[0]['capabilities']['lighting'] = True
            self.assertFalse(manager.enumerate_devices()[0]['capabilities']['lighting'])
            scan.assert_called_once()

    def test_missing_dependency_returns_all_unknown(self):
        manager = HardwareManager()
        with patch.object(manager, '_load', side_effect=ImportError('hid missing')):
            devices = manager.enumerate_devices()
        self.assertEqual(len(devices), 3)
        self.assertTrue(all(d['status']['code'] == 'unavailable' for d in devices))

    def test_reply_correlation_ignores_notifications(self):
        handle = Mock()
        handle.write.return_value = 20
        handle.read.side_effect = [[0x11,1,3,0,99,0,0], [0x11,1,3,11,20,0,0]]
        self.assertEqual(HidppReader(handle,1).query(3), bytes([20,0,0]))

    def test_reserved_software_id_ignores_replies_meant_for_other_readers(self):
        # The listener's long-lived handle also buffers another reader's root reply (feature index 3).
        handle = Mock()
        handle.write.return_value = 20
        handle.read.side_effect = [[0x11,1,0,11,3,0,0], [0x11,1,0,15,8,0,0]]
        self.assertEqual(HidppReader(handle, 1, 15).feature(0x1B04), 8)
        self.assertEqual(handle.write.call_args[0][0][3], 15)

    def test_short_write_fails(self):
        handle = Mock()
        handle.write.return_value = 1
        with self.assertRaises(OSError):
            HidppReader(handle,1).query(0)

    def test_voltage_curve_estimate(self):
        self.assertEqual(estimate_battery_percent(4300), 100)
        self.assertEqual(estimate_battery_percent(3747), 29)
        self.assertEqual(estimate_battery_percent(3400), 0)

    def test_adc_measurement_requires_active_flag(self):
        handle = Mock()
        handle.write.return_value = 20
        # ROOT lookup reply (feature index 8), then ADC replies: active 3747 mV, then inactive.
        handle.read.side_effect = lambda size, timeout: next(replies)
        replies = iter([[0x11, 0xff, 0, 0x0b, 8, 0, 0], [0x11, 0xff, 8, 0x0c, 0x0e, 0xa3, 0x01],
                        [0x11, 0xff, 0, 0x0d, 8, 0, 0], [0x11, 0xff, 8, 0x0e, 0x0e, 0xa3, 0x00]])
        reader = HidppReader(handle, 0xff)
        self.assertEqual(reader.adc_battery(), (29, False, 3747))
        with self.assertRaises(ValueError):
            reader.adc_battery()

    def test_g733_battery_uses_hidpp_and_reports_voltage(self):
        manager = HardwareManager()
        manager._hid = Mock()
        manager._hid.enumerate.return_value = [{'vendor_id': 0x046d, 'product_id': 0x0afe, 'usage_page': 0xff43,
                                                'usage': 0x0202, 'path': b'g733'}]
        with patch('hardware.runtime.HidppReader') as reader:
            reader.return_value.adc_battery.return_value = (29, True, 3747)
            device = next(d for d in manager.enumerate_devices() if d['id'] == 'g733')
        self.assertEqual((device['batteryPercent'], device['connection']), (29, 'connected'))
        self.assertEqual(device['telemetry']['batteryMillivolts'], 3747)
        self.assertTrue(device['telemetry']['charging'])
        manager._cache = None
        with patch('hardware.runtime.HidppReader') as reader:
            reader.return_value.adc_battery.side_effect = ValueError('inactive')
            device = next(d for d in manager.enumerate_devices() if d['id'] == 'g733')
        self.assertIsNone(device['batteryPercent'])
        self.assertEqual(device['status']['code'], 'unavailable')

    def test_unknown_battery_preserved(self):
        manager = HardwareManager()
        manager._hid = Mock()
        manager._hid.enumerate.return_value = [{'vendor_id':0x1038,'product_id':0x1838}]
        with patch.object(manager._api, 'status', return_value={'battery_percent':None,'charging':None}):
            device = next(d for d in manager.enumerate_devices() if d['id'] == 'aerox-3')
            self.assertIsNone(device['batteryPercent'])
            self.assertEqual(device['status']['code'], 'unavailable')

