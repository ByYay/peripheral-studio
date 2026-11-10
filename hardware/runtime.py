"""Real hardware provider. Unknown telemetry stays null.

Writes are limited to settings physically verified on 2026-11-09 (see
compatibility-report.json): G733 lighting and MX Keys backlight are confirmed
by HID++ readback; Aerox 3 commands are acknowledged only (no readback).
"""
import importlib
import json
import logging
import math
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
import copy
from datetime import datetime, timezone

from . import DPI_BUTTON_MAPPINGS, DeviceError, PeripheralAPI
from .hidpp import HidppReader

LOGGER = logging.getLogger(__name__)

G733_HIDPP = (0x046d, 0x0afe, 0xff43, 0x0202)
BOLT_HIDPP = (0x046d, 0xc548, 0xff00, 0x0002)
AEROX_LIMITS = {'dpi': {'min': 100, 'max': 18000, 'step': 100}, 'pollingRates': [125, 250, 500, 1000],
                'lightingZones': ['front', 'middle', 'rear'], 'lightingEffects': ['static', 'breathing', 'rainbow'],
                # rivalcfg's rainbow effect has no speed; breathing is animated by the app.
                'lightingSpeedEffects': ['breathing']}
# rivalcfg zone names mapped to the physical location (verified by the user on 2026-11-10).
AEROX_ZONES = {'front': ('top_color', 'z1'), 'middle': ('middle_color', 'z2'), 'rear': ('bottom_color', 'z3')}
# Effect period in seconds per speed; the mouse has no hardware breathing, the app animates it while running.
BREATHING_PERIODS = {'slow': 6.0, 'normal': 4.0, 'fast': 2.0}
CYCLE_PERIODS = {'slow': 12.0, 'normal': 6.0, 'fast': 3.0}
LED_EFFECTS = 0x8070
BACKLIGHT2 = 0x1982
LED_DISABLED, LED_STATIC, LED_CYCLE, LED_BREATHING = 0x0000, 0x0001, 0x0003, 0x000A
# COLOR_LED_EFFECTS zone of each headset light (both cups show the same; verified by the user on 2026-11-10).
G733_ZONES = {'top': 1, 'bottom': 0}
BACKLIGHT_CODES = {'auto': 1, 'manual': 3}
BACKLIGHT_NAMES = {code: name for name, code in BACKLIGHT_CODES.items()}
REPROG_CONTROLS = 0x1B04
# MX Keys Mini control ids from its REPROG_CONTROLS_V4 table (read 2026-11-10): F-row
# positions 4-12 and the volume-up key next to del ("INS"); all report DIVERTABLE.
MX_KEY_CIDS = {'F4': 0x00E2, 'F5': 0x00E3, 'F6': 0x0103, 'F7': 0x0108, 'F8': 0x010A, 'F9': 0x011C,
               'F10': 0x00E5, 'F11': 0x00E7, 'F12': 0x00E8, 'INS': 0x00E9}
FN_INVERSION = 0x40A3  # Fn+Esc on the keyboard toggles it
WIRELESS_STATUS = 0x1D4B  # notifies wake-ups and reconnections
LISTENER_SOFTWARE_ID = 15  # other readers use 10-14
GKEY, ADC_MEASUREMENT = 0x8010, 0x1F20
# G733 GKEY bits (verified with the user on 2026-11-10): the microphone button and a short press of the power button.
G733_BUTTONS = {'g1': 0x01, 'g2': 0x02}
LIGHTING_NOT_KEPT ='The headset did not keep the requested lighting. Windows Dynamic Lighting may be controlling it.'
KEYBOARD_ASLEEP = 'The keyboard is asleep. Press a key and try again.'
KEY_NOT_CONFIRMED = 'The keyboard did not confirm the key assignment.'
PRESENCE_INTERVAL = 3  # seconds between checks for receivers and devices that come or go
# The USB devices each managed device is reached through; one changing affects only its device.
RECEIVERS = {(0x046d, 0xc548): 'mx-keys-mini', (0x046d, 0x0afe): 'g733', (0x1038, 0x1838): 'aerox-3',
             (0x1038, 0x183A): 'aerox-3', (0x1038, 0x1878): 'aerox-3', (0x1038, 0x187A): 'aerox-3'}
HEADSET_OFF = 'The headset is off or out of range. Turn it on and try again.'


def scaled_color(color, brightness):
    """Static-color hardware has no brightness parameter; scale the RGB value."""
    rgb = bytes.fromhex(color[1:])
    return bytes(round(channel * brightness / 100) for channel in rgb)


class HardwareManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._api = PeripheralAPI()
        # Separate instance: only apply() uses it, for physically verified Aerox commands.
        self._writer = PeripheralAPI(allow_unverified_writes=True)
        self._keyboard = None
        self._hid = None
        self._cache = None
        self._refreshed = 0
        self._led_effects = None  # per-zone {effect id: effect index}
        self._g733_lighting = None  # last verified write: settings and expected readback
        self._aerox_lighting = {'lightingColor': '#ffffff', 'lightingBrightness': 100}
        self._breathing = None  # (stop event, thread) of the software breathing effect
        # MX keys the app wants reported -> whether another app (e.g. Logi Options+) had diverted them
        # too and also receives their presses (None: not applied yet).
        self._wanted = {}
        self._released = set()  # keys to give back to the keyboard once it is awake
        self._on_control = None
        self._listener = None
        self._buttons = set()  # G733 buttons the app wants reported (GKEY software control)
        self._button_listener = None
        self._presence = None

    def _load(self):
        if self._hid is None:
            try:
                self._hid = importlib.import_module('hid')
            except ImportError:
                vendor = Path(__file__).parent / 'vendor'
                if not vendor.is_dir():
                    raise DeviceError('missing_dependency: hidapi')
                sys.path.append(str(vendor))
                self._hid = importlib.import_module('hid')
        return self._hid

    @staticmethod
    def _base(device_id, name, kind, transport):
        return {'id': device_id, 'name': name, 'kind': kind, 'connection': transport,
                'batteryPercent': None, 'charging': None, 'settings': {}, 'limits': {},
                'capabilities': {'lighting': False, 'dpi': False, 'pollingRate': False, 'backlight': False},
                'status': 'connected', 'source': 'hardware',
                'observedAt': datetime.now(timezone.utc).isoformat()}

    def _open(self, hid, key):
        for interface in hid.enumerate():
            if (interface['vendor_id'], interface['product_id'], interface.get('usage_page'), interface.get('usage')) == key:
                handle = hid.device()
                try:
                    handle.open_path(interface['path'])
                except OSError:
                    handle.close()
                    raise
                return handle
        raise DeviceError('device_disconnected')

    def _scan(self):
        with self._lock:
            hid = self._load()
            interfaces = hid.enumerate()
            result = []
            deadline = time.monotonic() + 5
            if any(d['vendor_id'] == 0x1038 and d['product_id'] in (0x1838,0x183a,0x1878,0x187a) for d in interfaces):
                device = self._base('aerox-3', 'SteelSeries Aerox 3 Wireless', 'mouse', 'wireless')
                try:
                    battery = self._api.status('aerox-3')
                    device.update(batteryPercent=battery['battery_percent'], charging=battery['charging'])
                    if battery['battery_percent'] is None:
                        device.update(status='unavailable', error='Mouse battery unavailable; receiver detected')
                    else:
                        # Responding mouse: commands are physically verified, settings cannot be read back.
                        device['capabilities'].update(lighting=True, dpi=True, pollingRate=True)
                        device.update(limits=copy.deepcopy(AEROX_LIMITS), writeVerification='acknowledged')
                except (OSError, DeviceError, ValueError) as exc:
                    device.update(status='unavailable', error=str(exc))
                result.append(device)
            if any(d['vendor_id'] == 0x046d and d['product_id'] == 0x0afe for d in interfaces):
                device = self._base('g733', 'Logitech G733', 'headset', 'wireless')
                try:
                    self._headset(device, hid, interfaces)
                except (OSError, ValueError, subprocess.SubprocessError, DeviceError) as exc:
                    device.update(status='unavailable', error=str(exc))
                result.append(device)
            for interface in interfaces:
                if time.monotonic() >= deadline or any(d['id'] == 'mx-keys-mini' for d in result):
                    break
                if (interface['vendor_id'], interface['product_id'], interface.get('usage_page'), interface.get('usage')) != (0x046d,0xc548,0xff00,2):
                    continue
                handle = hid.device()
                try:
                    handle.open_path(interface['path'])
                    slots = [self._keyboard] if self._keyboard else list(range(1, 7))
                    for slot in slots:
                        if time.monotonic() >= deadline:
                            break
                        try:
                            reader = HidppReader(handle, slot)
                            try:
                                name = reader.name()
                            except TimeoutError:
                                # First request can wake a sleeping Bolt device.
                                # Retry the same slot, bounded by discovery budget.
                                if time.monotonic() >= deadline:
                                    raise
                                name = reader.name()
                            if name not in ('MX Keys Mini', 'MX Keys Mini for Business'):
                                if slot == self._keyboard:
                                    # Re-paired: another device now uses the remembered receiver slot.
                                    self._keyboard = None
                                    slots.extend(other for other in range(1, 7) if other != slot)
                                continue
                            self._keyboard = slot
                            device = self._base('mx-keys-mini', f'Logitech {name}', 'keyboard', 'wireless')
                            try:
                                device['batteryPercent'], device['charging'] = reader.battery()
                                state = self._backlight_state(reader)
                                device['capabilities']['backlight'] = True
                                device.update(settings=state['settings'], writeVerification='readback',
                                              limits={'backlightLevel': {'min': 0, 'max': state['levels'] - 1, 'step': 1}})
                            except (OSError, ValueError) as exc:
                                if device['batteryPercent'] is None:
                                    device.update(status='unavailable', error=str(exc))
                            try:
                                device['fnKeys'] = self._fn_keys(reader)
                            except (OSError, ValueError):
                                pass  # Unknown Fn mode stays absent.
                            result.append(device)
                            break
                        except (OSError, ValueError):
                            self._keyboard = None
                except OSError:
                    self._keyboard = None
                finally:
                    handle.close()
            return result

    def enumerate_devices(self):
        with self._lock:
            if self._cache is not None and time.monotonic() - self._refreshed < 10:
                return copy.deepcopy(self._cache)
            error = None
            try:
                found = {d['id']: d for d in self._scan()}
            except (OSError, ImportError, DeviceError, ValueError) as exc:
                found, error = {}, str(exc)
            devices = []
            for device_id, name, kind in [('mx-keys-mini','Logitech MX Keys Mini','keyboard'),
                                          ('g733','Logitech G733','headset'),
                                          ('aerox-3','SteelSeries Aerox 3 Wireless','mouse')]:
                device = found.get(device_id)
                if device is None:
                    device = self._base(device_id, name, kind, 'disconnected')
                    device['status'] = 'unavailable' if error else 'disconnected'
                    device['error'] = error or 'Device not responding or not detected'
                else:
                    device['connection'] = 'connected'
                device['telemetry'] = {'transport':'wireless', 'charging':device.pop('charging'),
                                       'source':device.pop('source'), 'observedAt':device.pop('observedAt')}
                millivolts = device.pop('batteryMillivolts', None)
                if millivolts is not None:
                    # The percentage is estimated from this measured voltage.
                    device['telemetry']['batteryMillivolts'] = millivolts
                verification = device.pop('writeVerification', None)
                if verification is not None:
                    device['telemetry']['writeVerification'] = verification
                fn_keys = device.pop('fnKeys', None)
                if fn_keys is not None:
                    device['telemetry']['fnKeys'] = fn_keys
                if device_id == 'mx-keys-mini':
                    shared = [control for control in MX_KEY_CIDS if self._wanted.get(control)]
                    if shared:
                        device['telemetry']['sharedKeys'] = shared
                device['status'] = {'code':device['status'], 'message':device.pop('error', 'Live hardware telemetry')}
                devices.append(device)
            self._cache, self._refreshed = devices, time.monotonic()
            return copy.deepcopy(devices)

    def _headset(self, device, hid, interfaces):
        # HeadsetControl 4.2 often reports BATTERY_UNAVAILABLE for the G733; the
        # headset's own HID++ ADC measurement is consistent (verified 2026-11-09).
        path = next((d['path'] for d in interfaces if (d['vendor_id'], d['product_id'], d.get('usage_page'), d.get('usage'))
                     == (0x046d, 0x0afe, 0xff43, 0x0202)), None)
        if path is None:
            return self._headsetcontrol(device)
        handle = hid.device()
        try:
            handle.open_path(path)
            reader = HidppReader(handle, 0xFF)
            try:
                level, charging, millivolts = reader.adc_battery()
            except TimeoutError:
                level, charging, millivolts = reader.adc_battery()
            device.update(batteryPercent=level, charging=charging, batteryMillivolts=millivolts)
            try:
                device['settings'] = self._read_g733_lighting(reader)
                device['capabilities']['lighting'] = True
                device['writeVerification'] = 'readback'
                if len(self._led_effects) == len(G733_ZONES):
                    shared = set.intersection(*(set(effects) for effects in self._led_effects))
                    animated = [name for name, effect in (('breathing', LED_BREATHING), ('rainbow', LED_CYCLE)) if effect in shared]
                    device['limits'] = {'lightingZones': list(G733_ZONES), 'lightingEffects': ['static'] + animated,
                                        'lightingSpeedEffects': animated}
            except (OSError, ValueError):
                pass  # Lighting stays unavailable; the battery reading is still valid.
        except ValueError:
            device.update(status='unavailable', error='Headset battery unavailable; receiver detected')
        finally:
            handle.close()

    def _led_zones(self, reader):
        """COLOR_LED_EFFECTS index and per-zone {effect id: effect index}, discovered once."""
        index = reader.feature(LED_EFFECTS)
        if not index:
            raise OSError('LED effects unsupported')
        if self._led_effects is None:
            info = reader.query(index)
            if not int.from_bytes(info[3:5], 'big') & 0x01:
                raise OSError('LED state is not readable')
            zones = []
            for zone in range(info[0]):
                effects = {}
                for effect in range(reader.query(index, 0x10, bytes([zone, 0xFF, 0x00]))[3]):
                    data = reader.query(index, 0x20, bytes([zone, effect, 0x00]))
                    effects[int.from_bytes(data[2:4], 'big')] = effect
                if not {LED_DISABLED, LED_STATIC} <= effects.keys():
                    raise OSError('Static zone lighting unsupported')
                zones.append(effects)
            self._led_effects = zones
        return index, self._led_effects

    def _led_states(self, reader, index, zones):
        # getZoneEffect (0xE0) reply: zone, effect index, 10 effect parameters.
        return [bytes(reader.query(index, 0xE0, bytes([zone]))[1:12]) for zone in range(len(zones))]

    @staticmethod
    def _g733_zone_state(target, zone, effects):
        """Effect index and the parameters it uses (Solaar LEDEffects layout); the rest stays zero."""
        effect = 'off' if target['lightingBrightness'] == 0 else target.get('lightingEffect', 'static')
        effect_id = {'off': LED_DISABLED, 'static': LED_STATIC, 'breathing': LED_BREATHING, 'rainbow': LED_CYCLE}[effect]
        if effect_id not in effects:
            raise DeviceError('unsupported_setting')
        state = bytes([effects[effect_id]])
        color = scaled_color((target.get('lightingZones') or {}).get(zone, target['lightingColor']), target['lightingBrightness'])
        if effect == 'static':
            return state + color
        speed = target.get('lightingSpeed', 'normal')
        if effect == 'breathing':  # color, period (ms), waveform 0 = default, intensity
            return state + color + struct.pack('>HBB', int(BREATHING_PERIODS[speed] * 1000), 0, 100)
        if effect == 'rainbow':  # period (ms) at 5, intensity at 7
            return state + bytes(5) + struct.pack('>HB', int(CYCLE_PERIODS[speed] * 1000), target['lightingBrightness'])
        return state

    @staticmethod
    def _speed(periods, period):
        milliseconds = int.from_bytes(period, 'big')
        return {'lightingSpeed': name for name, seconds in periods.items() if int(seconds * 1000) == milliseconds}

    def _read_g733_lighting(self, reader):
        index, zones = self._led_zones(reader)
        states = self._led_states(reader, index, zones)
        last = self._g733_lighting
        if last and [state[:len(expected)] for state, expected in zip(states, last['states'])] == last['states']:
            return copy.deepcopy(last['settings'])
        kinds = [next((effect_id for effect_id, effect in effects.items() if effect == state[0]), None)
                 for state, effects in zip(states, zones)]
        if all(kind == LED_DISABLED for kind in kinds):
            return {'lightingBrightness': 0}
        if all(kind == LED_CYCLE for kind in kinds):
            return {'lightingEffect': 'rainbow', 'lightingBrightness': states[0][8] or 100,
                    **self._speed(CYCLE_PERIODS, states[0][6:8])}
        if len(set(kinds)) == 1 and kinds[0] in (LED_STATIC, LED_BREATHING) and len(zones) == len(G733_ZONES):
            colors = {name: '#' + states[zone][1:4].hex() for name, zone in G733_ZONES.items()}
            settings = {'lightingEffect': 'static' if kinds[0] == LED_STATIC else 'breathing',
                        'lightingColor': colors['top'], 'lightingZones': colors, 'lightingBrightness': 100}
            if kinds[0] == LED_BREATHING:
                settings.update(self._speed(BREATHING_PERIODS, states[0][4:6]))
            return settings
        return {}  # Mixed (e.g. Windows Dynamic Lighting): unknown, not estimated.
    def _backlight_state(self, reader):
        index = reader.feature(BACKLIGHT2)
        if not index:
            raise OSError('Backlight unsupported')
        enabled, options, _supported, _effects, level, *durations = struct.unpack('<BBBHBHHH', reader.query(index)[:12])
        levels = reader.query(index, 0x20)[0]
        if not levels:
            raise ValueError('invalid backlight level count')
        mode = 'off' if not enabled else BACKLIGHT_NAMES.get((options >> 3) & 0x03)
        settings = {'backlightLevel': level}
        if mode:
            settings['backlightMode'] = mode
        return {'index': index, 'options': options, 'durations': durations, 'levels': levels, 'settings': settings}

    @staticmethod
    def _fn_keys(reader):
        """'special' when F1-F12 run their printed function without Fn, else 'standard'."""
        index = reader.feature(FN_INVERSION)
        if not index:
            raise OSError('Fn inversion unsupported')
        # Read with the current-host prefix 0xFF; state in byte 1 bit 0 (Solaar K375sFnSwap).
        return 'special' if reader.query(index, 0x00, b'\xff')[1] & 0x01 else 'standard'

    def _headsetcontrol(self, device):
        executable = Path(__file__).parent / 'tools' / 'headsetcontrol.exe'
        if not executable.is_file():
            raise DeviceError('missing_dependency: headsetcontrol 4.2.0')
        completed = subprocess.run([str(executable), '-d', '046d:0afe', '-o', 'json'],
                                   capture_output=True, text=True, timeout=2,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if completed.returncode:
            raise DeviceError('headsetcontrol_failed: ' + completed.stderr[:300])
        payload = json.loads(completed.stdout)
        candidates = [d for d in payload.get('devices', [])
                      if d.get('id_vendor') == '0x046d' and d.get('id_product') == '0x0afe']
        if len(candidates) != 1:
            raise DeviceError('headset_missing_or_ambiguous')
        battery = candidates[0].get('battery', {})
        level = battery.get('level')
        if battery.get('status') == 'BATTERY_UNAVAILABLE' or type(level) is not int or not 0 <= level <= 100:
            device.update(status='unavailable', error='Headset battery unavailable; receiver detected')
        else:
            device['batteryPercent'] = level

    def forget_readings(self):
        self._cache = None  # the next enumerate_devices() scans again

    def read(self, device_id):
        for device in self.enumerate_devices():
            if device['id'] == device_id:
                return device
        raise DeviceError('device_disconnected')

    def apply(self, device_id, settings):
        appliers = {'aerox-3': self._apply_aerox, 'g733': self._apply_g733, 'mx-keys-mini': self._apply_backlight}
        if device_id not in appliers:
            raise DeviceError('unsupported_backend')
        with self._lock:
            self._load()  # Writes may come before the first scan (e.g. restored assignments).
            try:
                return appliers[device_id](settings)
            finally:
                self._cache = None  # The next read reflects the device, not the request.

    @staticmethod
    def _require(settings, allowed):
        if not settings or not set(settings) <= allowed:
            raise DeviceError('unsupported_setting')

    def _apply_aerox(self, settings):
        lighting_keys = {'lightingColor', 'lightingBrightness', 'lightingZones', 'lightingEffect', 'lightingSpeed'}
        self._require(settings, {'dpi', 'pollingRate'} | lighting_keys)
        if 'dpi' in settings:
            self._writer.set_setting('aerox-3', 'dpi', settings['dpi'])
        if 'pollingRate' in settings:
            self._writer.set_setting('aerox-3', 'polling_rate', settings['pollingRate'])
        if lighting_keys & set(settings):
            lighting = {**self._aerox_lighting, **{key: settings[key] for key in lighting_keys if key in settings}}
            self._stop_breathing()
            effect = lighting.get('lightingEffect', 'static')
            if effect == 'rainbow':
                self._writer.set_setting('aerox-3', 'rainbow_effect')
            else:
                # Setting a color also ends the mouse's own rainbow effect.
                for zone, color in self._aerox_colors(lighting).items():
                    self._writer.set_setting('aerox-3', AEROX_ZONES[zone][0], color)
                if effect == 'breathing':
                    self._start_breathing(lighting)
            self._aerox_lighting = lighting
        # Transport acknowledgement only: the mouse cannot report settings back.
        return {'settings': dict(settings), 'verification': 'acknowledged'}

    @staticmethod
    def _aerox_colors(lighting, factor=1.0):
        zones = lighting.get('lightingZones') or {}
        return {zone: '#' + scaled_color(zones.get(zone, lighting['lightingColor']), lighting['lightingBrightness'] * factor).hex()
                for zone in AEROX_ZONES}

    def _start_breathing(self, lighting):
        stop = threading.Event()
        period = BREATHING_PERIODS[lighting.get('lightingSpeed', 'normal')]

        def run():
            while not stop.is_set():
                try:
                    with self._writer.color_session('aerox-3') as set_color:
                        while not stop.is_set():
                            started = time.monotonic()
                            phase = (started % period) / period
                            factor = 0.08 + 0.92 * (0.5 - 0.5 * math.cos(2 * math.pi * phase))
                            for zone, color in self._aerox_colors(lighting, factor).items():
                                set_color(AEROX_ZONES[zone][1], color)
                            # ~20 frames/s: three zone commands take about 48 ms.
                            stop.wait(max(0.0, 0.05 - (time.monotonic() - started)))
                except (OSError, ValueError, RuntimeError):
                    stop.wait(2)  # Mouse asleep or out of range: retry quietly.
                except Exception:
                    LOGGER.exception("Breathing effect failed; it starts again")
                    stop.wait(10)

        thread = threading.Thread(target=run, name='aerox-breathing', daemon=True)
        self._breathing = (stop, thread)
        thread.start()

    def _stop_breathing(self):
        if self._breathing is not None:
            stop, thread = self._breathing
            stop.set()
            thread.join(2)
            self._breathing = None

    def set_aerox_dpi_button(self, mode):
        """'dpi' (native), 'disabled', or 'action' (sends F23 for an app action)."""
        if mode not in DPI_BUTTON_MAPPINGS:
            raise ValueError('unsupported DPI button mode')
        with self._lock:
            self._load()
            self._writer.set_setting('aerox-3', 'buttons_mapping', DPI_BUTTON_MAPPINGS[mode])

    def _apply_g733(self, settings):
        keys = ('lightingColor', 'lightingBrightness', 'lightingZones', 'lightingEffect', 'lightingSpeed')
        self._require(settings, set(keys))
        handle = self._open(self._load(), G733_HIDPP)
        try:
            reader = HidppReader(handle, 0xFF)
            index, zones = self._led_zones(reader)
            if len(zones) != len(G733_ZONES):
                raise DeviceError('unsupported_setting')
            target = {'lightingColor': '#ffffff', 'lightingBrightness': 100, 'lightingEffect': 'static',
                      **self._read_g733_lighting(reader), **settings}
            if 'lightingColor' in settings and 'lightingZones' not in settings:
                target.pop('lightingZones', None)  # one color for both lights
            expected = [self._g733_zone_state(target, name, zones[zone])
                        for name, zone in sorted(G733_ZONES.items(), key=lambda item: item[1])]
            for zone, state in enumerate(expected):
                # setZoneEffect without the persistence flag: kept until the headset powers off.
                reader.query(index, 0x30, bytes([zone]) + state)
            time.sleep(0.15)  # Let a competing lighting controller show itself in the readback.
            observed = self._led_states(reader, index, zones)
        finally:
            handle.close()
        if [state[:len(wanted)] for state, wanted in zip(observed, expected)] != expected:
            self._g733_lighting = None
            raise DeviceError(LIGHTING_NOT_KEPT)
        applied = {key: copy.deepcopy(target[key]) for key in keys if key in target}
        self._g733_lighting = {'settings': applied, 'states': expected}
        return {'settings': copy.deepcopy(applied)}
    def _apply_backlight(self, settings):
        self._require(settings, {'backlightMode', 'backlightLevel'})
        if self._keyboard is None:
            raise DeviceError('device_disconnected')
        handle = self._open(self._load(), BOLT_HIDPP)
        try:
            reader = HidppReader(handle, self._keyboard)
            deadline = time.monotonic() + 3
            while True:
                try:
                    state = self._backlight_state(reader)
                    break
                except TimeoutError:
                    if time.monotonic() >= deadline:
                        raise DeviceError(KEYBOARD_ASLEEP) from None
            mode = settings.get('backlightMode', state['settings'].get('backlightMode', 'auto'))
            level = settings.get('backlightLevel', state['settings']['backlightLevel'])
            if not 0 <= level < state['levels']:
                raise ValueError('Backlight level is outside the device limits.')
            options = state['options'] if mode == 'off' else (state['options'] & 0x07) | (BACKLIGHT_CODES[mode] << 3)
            # Layout from Solaar Backlight.write; the level only applies in manual mode.
            reader.query(state['index'], 0x10, struct.pack('<BBBBHHH', 0 if mode == 'off' else 1, options, 0xFF,
                                                           level if mode == 'manual' else 0, *state['durations']))
            observed = self._backlight_state(reader)['settings']
        finally:
            handle.close()
        if observed.get('backlightMode') != mode or (mode == 'manual' and observed['backlightLevel'] != level):
            raise DeviceError('The keyboard did not confirm the requested backlight setting.')
        return {'settings': observed, 'ignored': [] if mode == 'manual' else ['backlightLevel']}

    @staticmethod
    def _awake(call, seconds):
        """Retry while a sleeping keyboard wakes up; its first requests time out."""
        deadline = time.monotonic() + seconds
        while True:
            try:
                return call()
            except TimeoutError:
                if time.monotonic() >= deadline:
                    raise DeviceError(KEYBOARD_ASLEEP) from None

    @staticmethod
    def _reporting(reader, index, control):
        # getCidReporting reply: cid, flags (bit0 diverted), ...
        cid = struct.pack('!H', MX_KEY_CIDS[control])
        reply = reader.query(index, 0x20, cid)
        if reply[:2] != cid:
            raise OSError('HID++ reply for another key')  # a late reply to an earlier request
        return bool(reply[2] & 0x01)

    @staticmethod
    def _set_reporting(reader, index, control, diverted):
        # setCidReporting: cid, flags (bit0 diverted, bit1 "diverted is valid"), remap 0 = keep.
        reader.query(index, 0x30, struct.pack('!HBH', MX_KEY_CIDS[control], 0x03 if diverted else 0x02, 0))

    def _reconcile(self, reader, index):
        """Bring the keyboard's diversion flags in line with the wanted and released keys."""
        for control, before in list(self._wanted.items()):
            diverted = self._reporting(reader, index, control)
            if before is None:
                self._wanted[control] = diverted
            if not diverted:
                self._set_reporting(reader, index, control, True)
                if not self._reporting(reader, index, control):
                    raise DeviceError(KEY_NOT_CONFIRMED)
        for control in list(self._released):
            self._set_reporting(reader, index, control, False)
            if self._reporting(reader, index, control):
                raise DeviceError(KEY_NOT_CONFIRMED)
            self._released.discard(control)

    def _sync_keys(self, seconds):
        handle = self._open(self._load(), BOLT_HIDPP)
        try:
            reader = HidppReader(handle, self._keyboard)
            index = self._awake(lambda: reader.feature(REPROG_CONTROLS), seconds)
            if not index:
                raise DeviceError('unsupported_setting')
            self._reconcile(reader, index)
        finally:
            handle.close()

    def set_key_diversion(self, control, diverted, wait=True):
        """Report an MX Keys key to the app (temporary HID++ diversion) or give it back.

        With wait, a sleeping keyboard is an error and nothing changes; without it
        (restoring saved assignments) the listener applies the change on wake-up.
        """
        if control not in MX_KEY_CIDS:
            raise ValueError('unsupported key')
        with self._lock:
            self._load()
            if self._keyboard is None:
                self.enumerate_devices()  # finds the keyboard's receiver slot
            if (control in self._wanted) == diverted and (not diverted or self._wanted[control] is not None):
                return  # Already applied; the listener keeps it applied.
            was_wanted, before = control in self._wanted, self._wanted.get(control)
            if diverted:
                self._wanted[control] = None
                self._released.discard(control)
            elif self._wanted.pop(control) is not None:
                # Back to the keyboard's own function, even for keys another app had diverted:
                # whether it still runs is unknown, and a diverted key without a listener is dead.
                self._released.add(control)
            try:
                if self._keyboard is None:
                    raise DeviceError('device_disconnected')
                self._sync_keys(3 if wait else 0)
            except (OSError, DeviceError):
                if wait:
                    self._wanted.pop(control, None)
                    self._released.discard(control)
                    if was_wanted:
                        self._wanted[control] = before
                    raise
            self._cache = None  # The next read reports keys shared with another app.
            if self._wanted:
                self._start_listener()

    def watch_presence(self, on_change):
        """Call on_change(device_ids) within a few seconds when receivers are plugged in, removed
        or moved, the mouse switches between its cable and the receiver, or the mouse or headset
        is switched on: the devices that may have lost their settings."""
        if self._presence is None:
            stop = threading.Event()
            thread = threading.Thread(target=self._watch_presence, args=(on_change, stop), name='device-presence', daemon=True)
            self._presence = (stop, thread)
            thread.start()

    def _presence_state(self):
        receivers = frozenset((d['vendor_id'], d['product_id'], d['path']) for d in self._load().enumerate()
                              if (d['vendor_id'], d['product_id']) in RECEIVERS)
        with self._lock:
            mouse = any(vendor == 0x1038 for vendor, _, _ in receivers) and self._mouse_responds()
            headset = any(product == 0x0afe for _, product, _ in receivers) and self._headset_responds()
        return receivers, mouse, headset

    def _mouse_responds(self):
        try:
            return self._api.status('aerox-3')['battery_percent'] is not None
        except (OSError, ValueError, DeviceError):
            return False

    def _headset_responds(self):
        try:
            handle = self._open(self._hid, G733_HIDPP)
        except (OSError, DeviceError):
            return False
        try:
            HidppReader(handle, 0xFF).adc_battery()  # fails while the headset is off
            return True
        except (OSError, ValueError):
            return False
        finally:
            handle.close()

    def _watch_presence(self, on_change, stop):
        last = None
        while True:
            try:
                state = self._presence_state()
            except Exception:
                LOGGER.exception("Checking for devices failed")
                state = last
            if last is not None and state is not None:
                changed = set()
                for vendor, product, _ in state[0] ^ last[0]:  # receivers plugged, removed or moved
                    changed.add(RECEIVERS[(vendor, product)])
                if state[1] and not last[1]:
                    changed.add('aerox-3')
                if state[2] and not last[2]:
                    changed.add('g733')
                if changed:
                    self._cache = None
                    try:
                        on_change(changed)
                    except Exception:
                        LOGGER.exception("Handling devices that came back failed")
            last = state
            if stop.wait(PRESENCE_INTERVAL):
                return

    def _report(self, device_id, control):
        """Hand a press to the app; a failing action must not stop the device listener."""
        callback = self._on_control
        if callback is not None:
            try:
                callback(device_id, control)
            except Exception:
                LOGGER.exception("Action for %s %s failed", device_id, control)

    def watch_controls(self, on_control):
        """on_control(device_id, control) runs on a listener thread when a wanted key or button is pressed."""
        self._on_control = on_control

    def _start_listener(self):
        if self._listener is None:
            stop = threading.Event()
            thread = threading.Thread(target=self._listen, args=(stop,), name='mx-keys-listener', daemon=True)
            self._listener = (stop, thread)
            thread.start()

    def _pending_keys(self):
        return None in self._wanted.values() or bool(self._released)

    def _listen(self, stop):
        names = {cid: control for control, cid in MX_KEY_CIDS.items()}
        while not stop.is_set():
            handle, slot = None, self._keyboard
            try:
                handle = self._open(self._load(), BOLT_HIDPP)
                if slot is None:
                    # Asleep since the app started: the first notification of a waking device starts a
                    # scan. Replies (non-zero software id) and receiver errors are not notifications.
                    deadline = time.monotonic() + 5
                    while not stop.is_set() and self._keyboard is None and time.monotonic() < deadline:
                        report = bytes(handle.read(64, 250))
                        if len(report) >= 7 and 1 <= report[1] <= 6 and (
                                (report[0] == 0x10 and report[2] == 0x41) or (report[0] == 0x11 and not report[3] & 0x0F)):
                            stop.wait(0.3)
                            with self._lock:
                                self._cache = None
                                self.enumerate_devices()
                    continue
                # A keyboard that is dozing off or waking up can answer after more than a second.
                reader = HidppReader(handle, slot, LISTENER_SOFTWARE_ID, timeout=2)
                index = wireless = None
                pressed, due, woke = set(), 0.0, 0.0
                while not stop.is_set() and self._keyboard == slot:  # re-paired: reopen on the new slot
                    if time.monotonic() >= due:
                        try:
                            with self._lock:
                                while handle.read(64, 1):  # 0 would block until a report arrives
                                    pass  # Late replies to this reader's timed-out requests.
                                index = index or reader.feature(REPROG_CONTROLS)
                                if not index:
                                    raise DeviceError('unsupported_setting')
                                if wireless is None:
                                    wireless = reader.feature(WIRELESS_STATUS)
                                self._reconcile(reader, index)
                            due = time.monotonic() + 60  # safety net; wake-ups are notified
                        except (OSError, DeviceError):
                            # Asleep or just waking up: retry soon while something is waiting for it.
                            recent = time.monotonic() - woke < 30
                            due = time.monotonic() + (2 if recent or self._pending_keys() else 60)
                    report = bytes(handle.read(64, 250))
                    if len(report) < 7 or report[1] != slot:  # HID++ short reports are 7 bytes
                        continue
                    if (report[0] == 0x10 and report[2] == 0x41) or (wireless and report[0] == 0x11 and report[2] == wireless):
                        # Reconnected or woke up (verified 2026-11-10: WIRELESS_DEVICE_STATUS "reconfiguration
                        # needed"); the keyboard forgets temporary diversion while it sleeps.
                        woke = due = time.monotonic()
                    elif index and report[0] == 0x11 and report[2] == index and report[3] == 0x00:
                        # Diverted-keys event: up to four control ids currently held down.
                        down = {int.from_bytes(report[4 + 2 * i:6 + 2 * i], 'big') for i in range(4)} - {0}
                        for cid in down - pressed:
                            control = names.get(cid)
                            if control in self._wanted:
                                self._report('mx-keys-mini', control)
                        pressed = down
            except (OSError, DeviceError, ImportError):
                stop.wait(5)  # Receiver gone or re-enumerated (e.g. after sleep): open it again.
            except Exception:
                LOGGER.exception("Device listener failed; it starts again")
                stop.wait(5)
            finally:
                if handle is not None:
                    handle.close()

    def _set_software_control(self, reader, enabled):
        index = reader.feature(GKEY)
        if not index:
            raise DeviceError('unsupported_setting')
        reader.query(index, 0x20, bytes([1 if enabled else 0]))
        return index

    def set_headset_button(self, control, captured, wait=True):
        """Report a G733 button to the app (GKEY software control) or give it back.

        The headset switches both buttons together: while either one is captured, neither runs
        its own function (holding power still turns the headset off). Software control ends when
        the headset powers off; the listener renews it when the headset is back. Without wait, an
        unreachable headset is updated by the listener instead of failing.
        """
        if control not in G733_BUTTONS:
            raise ValueError('unsupported button')
        with self._lock:
            wanted = self._buttons | {control} if captured else self._buttons - {control}
            if bool(wanted) != bool(self._buttons):
                try:
                    handle = self._open(self._load(), G733_HIDPP)
                    try:
                        self._set_software_control(HidppReader(handle, 0xFF), bool(wanted))
                    finally:
                        handle.close()
                except (OSError, DeviceError) as exc:
                    if wait:
                        raise DeviceError(HEADSET_OFF) from exc
            self._buttons = wanted
            if wanted:
                self._start_button_listener()

    def _start_button_listener(self):
        if self._button_listener is None:
            stop = threading.Event()
            thread = threading.Thread(target=self._listen_buttons, args=(stop,), name='g733-buttons-listener', daemon=True)
            self._button_listener = (stop, thread)
            thread.start()

    def _listen_buttons(self, stop):
        while not stop.is_set():
            handle = None
            try:
                handle = self._open(self._load(), G733_HIDPP)
                reader = HidppReader(handle, 0xFF, LISTENER_SOFTWARE_ID, timeout=2)
                index = adc = None
                held, due, powered = 0, 0.0, True
                while not stop.is_set():
                    if time.monotonic() >= due:
                        try:
                            with self._lock:
                                while handle.read(64, 1):  # late replies to this reader's requests
                                    pass
                                adc = adc or reader.feature(ADC_MEASUREMENT)
                                index = self._set_software_control(reader, bool(self._buttons))
                            due = time.monotonic() + 30  # safety net; power-ons are notified
                        except (OSError, DeviceError):
                            due = time.monotonic() + 30  # Off or out of range.
                    report = bytes(handle.read(64, 250))
                    if len(report) < 7 or report[3] & 0x0F:  # replies to requests, not notifications
                        continue
                    if adc and report[2] == adc:
                        # ADC_MEASUREMENT notification: millivolts, flags (bit 0: headset on).
                        if report[6] & 0x01 and not powered:
                            due = time.monotonic() + 0.5  # Turned on: software control was reset.
                        powered = bool(report[6] & 0x01)
                    elif index and report[2] == index and report[3] == 0x00:
                        for control, bit in G733_BUTTONS.items():
                            if report[4] & bit and not held & bit and control in self._buttons:
                                self._report('g733', control)
                        held = report[4]
            except (OSError, DeviceError, ImportError):
                stop.wait(5)  # Receiver gone or re-enumerated (e.g. after sleep): open it again.
            except Exception:
                LOGGER.exception("Device listener failed; it starts again")
                stop.wait(5)
            finally:
                if handle is not None:
                    handle.close()

    def close(self):
        if self._presence is not None:
            self._presence[0].set()
            self._presence[1].join(3)
            self._presence = None
        if self._button_listener is not None:
            self._button_listener[0].set()
            self._button_listener[1].join(3)
            self._button_listener = None
        with self._lock:
            if self._buttons:
                self._buttons = set()
                try:  # Give the buttons back to the headset.
                    handle = self._open(self._load(), G733_HIDPP)
                    try:
                        self._set_software_control(HidppReader(handle, 0xFF), False)
                    finally:
                        handle.close()
                except (OSError, DeviceError):
                    pass  # Off: software control already ended.
        if self._listener is not None:
            self._listener[0].set()
            self._listener[1].join(2)
            self._listener = None
        with self._lock:
            # Give keys back so they keep their own function after the app exits.
            self._released |= {control for control, shared in self._wanted.items() if shared is not None}
            self._wanted = {}
            if self._released and self._keyboard is not None:
                try:
                    self._sync_keys(2)
                except (OSError, DeviceError):
                    pass  # Asleep: the keys recover when the keyboard reconnects.
        if self._breathing is not None:
            self._stop_breathing()
            try:  # Leave the mouse at full brightness instead of a random dimmed frame.
                for zone, color in self._aerox_colors(self._aerox_lighting).items():
                    self._writer.set_setting('aerox-3', AEROX_ZONES[zone][0], color)
            except (OSError, ValueError, RuntimeError):
                pass
