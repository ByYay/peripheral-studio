"""API contract tests using explicit synthetic hardware and automation doubles.

These tests establish integration behavior, never physical-device compatibility.
"""
from copy import deepcopy
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from automation.engine import Action
from backend.domain import Device
from backend.server import create_server


class SyntheticHardware:
    def __init__(self):
        self.items = [Device('mx-keys-mini', 'Test keyboard', 'keyboard', 'connected', None,
                             {'lighting': False, 'dpi': False, 'pollingRate': False}, {}, {},
                             status={'code': 'unavailable', 'message': 'Synthetic unknown battery'})]
        self.applied = []
        self.routed = []
        self.waits = []
        self.on_control = None

    def devices(self):
        return deepcopy(self.items)

    def apply(self, updates):
        self.applied.append(deepcopy(updates))
        raise AssertionError('Unsupported synthetic hardware must not receive writes')

    def assign_control(self, device_id, control, mode, wait=True):
        self.routed.append((device_id, control, mode))
        self.waits.append(wait)

    def watch_controls(self, on_control):
        self.on_control = on_control

    def close(self):
        pass


class SyntheticAutomation:
    def __init__(self):
        self.enabled = False
        self.bindings = {}
        self.closed = False
        self.executed = []

    def configure(self, profile_id, bindings):
        self.bindings = dict(bindings)
        self.enabled = False

    def enable(self):
        self.enabled = True

    def disable(self):
        self.enabled = False

    def status(self):
        return {'enabled': self.enabled, 'error': None, 'busy': False}

    def execute(self, control):
        self.executed.append(control)

    def close(self):
        self.disable()
        self.closed = True


class RealModeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hardware = SyntheticHardware()
        self.automation = SyntheticAutomation()
        self.picked = None
        self.start()
        self.addCleanup(self.stop)

    def start(self, pick_file=True):
        self.server = create_server(data_dir=Path(self.directory.name), token='test-token',
                                    mode='hardware', provider=self.hardware,
                                    automation_service=self.automation,
                                    pick_file=(lambda: self.picked) if pick_file else None)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.server.service.close()
        self.thread.join(3)
        self.server = None

    def request(self, method, path, body=None, authenticated=True):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        headers = {'X-UPM-Token': 'test-token'} if authenticated else {}
        if body is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(body)
        try:
            connection.request(method, '/api/v1' + path, body, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_hardware_mode_never_substitutes_unknown_battery_or_simulation(self):
        status, payload = self.request('GET', '/health')
        self.assertEqual(status, 200)
        self.assertEqual(payload['mode'], 'hardware')
        status, payload = self.request('GET', '/devices')
        self.assertEqual(status, 200)
        self.assertEqual(len(payload['devices']), 1)
        self.assertIsNone(payload['devices'][0]['batteryPercent'])
        self.assertEqual(payload['devices'][0]['connection'], 'connected')
        status, _ = self.request('PATCH', '/devices/mx-keys-mini/settings', {'dpi': 800})
        self.assertEqual(status, 400)
        self.assertEqual(self.hardware.applied, [])

    def test_assignment_routes_require_session_token(self):
        for method, path, body in (('GET', '/assignments', None),
                                   ('PUT', '/assignments/aerox-3/dpi', {'type': 'media', 'key': 'mute'}),
                                   ('POST', '/dialogs/open-file', {})):
            with self.subTest(path=path):
                status, payload = self.request(method, path, body, authenticated=False)
                self.assertEqual(status, 401)
                self.assertIn('error', payload)
        self.assertEqual(self.hardware.routed, [])
        self.assertFalse(self.automation.enabled)

    def test_dpi_action_routes_the_button_and_enables_only_that_trigger(self):
        status, state = self.request('PUT', '/assignments/aerox-3/dpi', {'type': 'media', 'key': 'play_pause'})
        self.assertEqual(status, 200)
        self.assertEqual(state['devices']['aerox-3']['assignments']['dpi'], {'type': 'media', 'key': 'play_pause'})
        self.assertEqual(self.hardware.routed[-1], ('aerox-3', 'dpi', 'action'))
        self.assertEqual(self.automation.bindings, {'key:F23': Action('shortcut', keys=('MEDIA_PLAY_PAUSE',))})
        self.assertTrue(self.automation.enabled)
        status, state = self.request('PUT', '/assignments/aerox-3/dpi', {'type': 'disabled'})
        self.assertEqual(self.hardware.routed[-1], ('aerox-3', 'dpi', 'disabled'))
        self.assertEqual(self.automation.bindings, {})
        self.assertFalse(self.automation.enabled)
        status, state = self.request('PUT', '/assignments/aerox-3/dpi', {'type': 'default'})
        self.assertEqual(status, 200)
        self.assertEqual(self.hardware.routed[-1], ('aerox-3', 'dpi', 'default'))
        self.assertEqual(state['devices']['aerox-3']['assignments'], {})

    def test_assignments_survive_restart_and_are_reapplied(self):
        action = {'type': 'url', 'url': 'https://example.org/'}
        self.assertEqual(self.request('PUT', '/assignments/aerox-3/dpi', action)[0], 200)
        self.stop()
        self.hardware.routed.clear()
        self.hardware.waits.clear()
        self.automation = SyntheticAutomation()
        self.start()
        status, state = self.request('GET', '/assignments')
        self.assertEqual(state['devices']['aerox-3']['assignments']['dpi'], action)
        self.assertEqual(self.automation.bindings, {'key:F23': Action('url', target='https://example.org/')})
        deadline = time.monotonic() + 3
        while not self.hardware.routed and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.hardware.routed, [('aerox-3', 'dpi', 'action')])
        self.assertEqual(self.hardware.waits, [False])  # a sleeping device is updated when it wakes

    def test_keyboard_key_presses_run_only_their_assigned_action(self):
        status, state = self.request('GET', '/assignments')
        self.assertEqual(state['devices']['mx-keys-mini']['controls'],
                         ['F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12', 'INS'])
        status, state = self.request('PUT', '/assignments/mx-keys-mini/F7', {'type': 'shortcut', 'preset': 'copy'})
        self.assertEqual(status, 200)
        self.assertEqual(self.hardware.routed[-1], ('mx-keys-mini', 'F7', 'action'))
        self.assertEqual(self.automation.bindings, {'device:mx-keys-mini:F7': Action('shortcut', keys=('LCTRL', 'C'))})
        self.assertEqual(self.request('PUT', '/assignments/mx-keys-mini/INS', {'type': 'disabled'})[0], 200)
        self.assertEqual(self.hardware.routed[-1], ('mx-keys-mini', 'INS', 'disabled'))
        for control in ('F7', 'INS', 'F4'):
            self.hardware.on_control('mx-keys-mini', control)
        self.assertEqual(self.automation.executed, ['device:mx-keys-mini:F7'])
        self.assertEqual(self.request('PUT', '/assignments/mx-keys-mini/F7', {'type': 'default'})[0], 200)
        self.assertEqual(self.hardware.routed[-1], ('mx-keys-mini', 'F7', 'default'))
        self.assertEqual(self.request('PUT', '/assignments/mx-keys-mini/F1', {'type': 'default'})[0], 404)

    def test_headset_buttons_run_their_assigned_action(self):
        status, state = self.request('GET', '/assignments')
        self.assertEqual(state['devices']['g733']['controls'], ['g1', 'g2'])
        self.assertEqual(self.request('PUT', '/assignments/g733/g1', {'type': 'media', 'key': 'mute'})[0], 200)
        self.assertEqual(self.hardware.routed[-1], ('g733', 'g1', 'action'))
        self.hardware.on_control('g733', 'g1')
        self.hardware.on_control('g733', 'g2')  # not assigned: nothing runs
        self.assertEqual(self.automation.executed, ['device:g733:g1'])
    def test_deleting_saved_data_gives_controls_back_and_keeps_the_language(self):
        self.assertEqual(self.request('PUT', '/assignments/aerox-3/dpi', {'type': 'media', 'key': 'mute'})[0], 200)
        self.assertEqual(self.request('PUT', '/assignments/mx-keys-mini/F7', {'type': 'disabled'})[0], 200)
        self.assertEqual(self.request('PUT', '/preferences', {'language': 'tr'})[0], 200)
        self.assertEqual(self.request('POST', '/profiles', {'name': 'Gece'})[0], 201)
        data = Path(self.directory.name)
        (data / 'hardware-settings.json').write_text('{"aerox-3": {"dpi": 800}}', encoding='utf-8')
        status, payload = self.request('DELETE', '/data', authenticated=False)
        self.assertEqual(status, 401)
        status, payload = self.request('DELETE', '/data')
        self.assertEqual(status, 200)
        self.assertEqual(payload['profiles'], [])
        self.assertEqual(payload['assignments']['devices']['aerox-3']['assignments'], {})
        self.assertIn(('aerox-3', 'dpi', 'default'), self.hardware.routed)
        self.assertIn(('mx-keys-mini', 'F7', 'default'), self.hardware.routed)
        self.assertEqual(self.automation.bindings, {})
        self.assertFalse((data / 'hardware-settings.json').exists() or (data / 'assignments.json').exists())
        self.stop()
        self.automation = SyntheticAutomation()
        self.start()
        self.assertEqual(self.request('GET', '/profiles')[1], {'profiles': [], 'profileState': {
            'mainProfileId': None, 'activeProfileId': None, 'activeBy': None}})
        self.assertEqual(self.request('GET', '/preferences')[1], {'language': 'tr'})

    def test_invalid_actions_and_controls_are_rejected(self):
        for body in ({'type': 'macro'}, {'type': 'url', 'url': 'javascript:alert(1)'}, {'type': 'media', 'key': 'eject'},
                     {'type': 'shortcut', 'preset': 'format_disk'}, {'type': 'open', 'path': 'C:\\missing\\x.exe'},
                     {'type': 'open', 'path': 'relative.txt'}, {'type': 'default', 'approved': True}, []):
            with self.subTest(body=body):
                self.assertEqual(self.request('PUT', '/assignments/aerox-3/dpi', body)[0], 400)
        self.assertEqual(self.request('PUT', '/assignments/aerox-3/button1', {'type': 'default'})[0], 404)
        self.assertEqual(self.request('PUT', '/assignments/keyboard/F4', {'type': 'default'})[0], 404)
        self.assertEqual(self.hardware.routed, [])

    def test_open_file_action_and_native_picker(self):
        target = Path(self.directory.name) / 'notes.txt'
        target.write_text('x', encoding='utf-8')
        self.picked = str(target)
        status, payload = self.request('POST', '/dialogs/open-file', {})
        self.assertEqual((status, payload), (200, {'path': str(target)}))
        status, state = self.request('PUT', '/assignments/aerox-3/dpi', {'type': 'open', 'path': str(target)})
        self.assertEqual(status, 200)
        self.assertEqual(self.automation.bindings['key:F23'], Action('open', target=str(target.resolve())))
        self.picked = None
        self.assertEqual(self.request('POST', '/dialogs/open-file', {})[1], {'path': None})
        self.stop()
        self.start(pick_file=False)
        self.assertEqual(self.request('POST', '/dialogs/open-file', {})[0], 409)


if __name__ == '__main__':
    unittest.main()
