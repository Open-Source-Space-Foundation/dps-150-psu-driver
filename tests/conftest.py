import pytest

from dps150.protocol import ALLOWED_COMMANDS, HOST_HEADER, pack_f32
from dps150.testing import MockSerial

FACTORY_REGISTERS = {
    0xC1: pack_f32(0.0),
    0xC2: pack_f32(0.0),
    0xD1: pack_f32(30.0),
    0xD2: pack_f32(5.1),
    0xDB: b"\x00",
    0xDC: b"\x05",
    0xDD: b"\x01",
    0xDE: b"DPS-150",
    0xDF: b"V1.0",
    0xE0: b"V1.2",
    0xE2: pack_f32(11.8),
    0xE3: pack_f32(5.1),
}


def assert_only_allowlisted_commands(ports: list[MockSerial]) -> None:
    """Fail if any host frame written to any mock port carries a command outside the allowlist.

    MockSerial.write raises on such a frame, but driver code that catches Exception
    would swallow that. Scanning `written` afterwards catches it regardless.
    """
    for port in ports:
        for frame in port.written:
            if len(frame) >= 2 and frame[0] == HOST_HEADER:
                assert frame[1] in ALLOWED_COMMANDS, (
                    f"host sent forbidden command 0x{frame[1]:02X}: {frame.hex(' ')}"
                )


@pytest.fixture(autouse=True)
def forbid_non_allowlisted_commands(request, monkeypatch):
    ports: list[MockSerial] = []
    original_init = MockSerial.__init__

    def tracking_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        ports.append(self)

    monkeypatch.setattr(MockSerial, "__init__", tracking_init)
    yield
    if request.node.get_closest_marker("sends_forbidden_command") is None:
        assert_only_allowlisted_commands(ports)
