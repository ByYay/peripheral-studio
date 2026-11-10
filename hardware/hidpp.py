"""Read-only HID++ 2 feature queries; protocol reference: Solaar hidpp20.py.

No guessed feature indices: ROOT discovers each feature at runtime.
"""
import time


# Li-ion discharge curve from Solaar's estimate_battery_level_percentage.
VOLTAGE_CURVE = ((4186, 100), (4067, 90), (3989, 80), (3922, 70), (3859, 60), (3811, 50), (3778, 40),
                 (3751, 30), (3717, 20), (3671, 10), (3646, 5), (3579, 2), (3500, 0))


def estimate_battery_percent(millivolts):
    if millivolts >= VOLTAGE_CURVE[0][0]:
        return 100
    for (high, high_percent), (low, low_percent) in zip(VOLTAGE_CURVE, VOLTAGE_CURVE[1:]):
        if low <= millivolts <= high:
            return round(low_percent + (high_percent - low_percent) * (millivolts - low) / (high - low))
    return 0


class HidppReader:
    def __init__(self, handle, slot, software_id=None, timeout=0.5):
        # A long-lived handle also receives replies meant for other readers; a reserved
        # software id keeps them from matching its own requests.
        self.handle, self.slot, self.timeout = handle, slot, timeout
        self.fixed_id = software_id
        self.software_id = software_id or 10

    def query(self, index, function=0, parameters=b''):
        self.software_id = self.fixed_id or 10 + (self.software_id - 9) % 5
        header = bytes([0x11, self.slot, index, function | self.software_id])
        packet = (header + parameters).ljust(20, b'\0')
        if self.handle.write(packet) != len(packet):
            raise OSError('incomplete HID++ query')
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            reply = bytes(self.handle.read(64, 100))
            if len(reply) >= 7 and reply[1:4] == header[1:4] and reply[0] in (0x10, 0x11):
                return reply[4:]
            if len(reply) >= 7 and reply[1] == self.slot and reply[2] == 0xff and reply[3:5] == header[2:4]:
                raise OSError('HID++ error: %d' % reply[5])
        raise TimeoutError('HID++ response timeout')

    def feature(self, feature_id):
        return self.query(0, parameters=feature_id.to_bytes(2, 'big'))[0]

    def name(self):
        index = self.feature(0x0005)
        if not index:
            return None
        length = self.query(index)[0]
        if not 0 < length <= 128:
            raise ValueError('invalid device name length')
        result = b''
        while len(result) < length:
            result += self.query(index, 0x10, bytes([len(result)]))[:length-len(result)]
        return result.decode('utf-8')

    def adc_battery(self):
        """G headsets (ADC_MEASUREMENT 0x1F20): (percent, charging, millivolts).

        Percent is the voltage-curve estimate Solaar uses; flag bit 0 means the
        headset is active and the measurement valid, bit 1 means charging.
        """
        index = self.feature(0x1F20)
        if not index:
            raise OSError('ADC measurement unsupported')
        data = self.query(index)
        millivolts, flags = int.from_bytes(data[:2], 'big'), data[2]
        if not flags & 0x01 or not 3000 <= millivolts <= 4500:
            raise ValueError('headset inactive or invalid battery voltage')
        return estimate_battery_percent(millivolts), bool(flags & 0x02), millivolts

    def battery(self):
        index = self.feature(0x1004)
        if index:
            data = self.query(index, 0x10)
            level, status = data[0], data[2]
        else:
            index = self.feature(0x1000)
            if not index:
                return None, None
            data = self.query(index)
            level, status = data[0], data[2]
        return (level if 0 <= level <= 100 else None,
                status in (1, 2, 3, 4) if status <= 4 else None)
