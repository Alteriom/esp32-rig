from types import SimpleNamespace

from alteriom_hil.pytest_plugin import _serial_opener


class FakeSerial:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.rts = True
        self.dtr = True


def test_hardware_serial_opener_releases_esp32_reset_lines():
    serial_mod = SimpleNamespace(Serial=FakeSerial)
    board = SimpleNamespace(port="/dev/esp32-farm-01", baud=115200)
    stream = _serial_opener(serial_mod, board)()
    assert stream.args == ("/dev/esp32-farm-01", 115200)
    assert stream.kwargs == {"timeout": 0.1, "rtscts": False, "dsrdtr": False}
    assert stream.rts is False
    assert stream.dtr is False
