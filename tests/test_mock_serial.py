from dps150.protocol import CMD_GET, CMD_SET, Frame, FrameDecoder, encode, pack_f32, unpack_f32
from dps150.testing import FakeClock, MockSerial

TELEMETRY_FRAME_LEN = 4 + 12 + 1


def make_port(**kwargs) -> tuple[MockSerial, FakeClock]:
    clock = FakeClock()
    return MockSerial(clock=clock, **kwargs), clock


def test_pushes_c3_telemetry_every_500_ms():
    port, clock = make_port()
    assert port.in_waiting == 0
    clock.sleep(0.49)
    assert port.in_waiting == 0
    clock.sleep(0.01)
    assert port.in_waiting == TELEMETRY_FRAME_LEN
    clock.sleep(1.0)
    assert port.in_waiting == 3 * TELEMETRY_FRAME_LEN


def test_telemetry_frame_carries_output_voltage_current_power():
    port, clock = make_port()
    port.telemetry = (5.0, 0.25, 1.25)
    clock.sleep(0.5)
    (frame,) = FrameDecoder().feed(port.read(port.in_waiting))
    assert frame.register == 0xC3
    assert [unpack_f32(frame.data, i) for i in (0, 4, 8)] == [5.0, 0.25, 1.25]


def test_set_is_recorded_and_stored_without_an_echo():
    port, _ = make_port()
    request = encode(CMD_SET, 0xC1, pack_f32(3.3))
    assert port.write(request) == len(request)
    assert port.written == [request]
    assert port.registers[0xC1] == pack_f32(3.3)
    assert port.in_waiting == 0


def test_get_replies_with_the_stored_register_value():
    port, _ = make_port()
    port.write(encode(CMD_SET, 0xC1, pack_f32(3.3)))
    port.write(encode(CMD_GET, 0xC1, b"\x00"))
    assert FrameDecoder().feed(port.read(port.in_waiting)) == [Frame(0xC1, pack_f32(3.3))]


def test_get_of_unknown_register_gets_no_reply():
    port, _ = make_port()
    port.write(encode(CMD_GET, 0xD1, b"\x00"))
    assert port.in_waiting == 0


def test_corrupt_set_is_recorded_but_ignored():
    port, _ = make_port()
    port.registers[0xC1] = pack_f32(1.0)
    corrupt = bytearray(encode(CMD_SET, 0xC1, pack_f32(3.3)))
    corrupt[-1] ^= 0xFF
    port.write(bytes(corrupt))
    assert port.written == [bytes(corrupt)]
    assert port.registers[0xC1] == pack_f32(1.0)


def test_read_waits_up_to_timeout_on_the_injected_clock():
    port, clock = make_port(timeout=1.0)
    data = port.read(1000)
    assert clock.monotonic() == 1.0
    assert len(data) == 2 * TELEMETRY_FRAME_LEN


def test_read_returns_as_soon_as_size_bytes_arrive():
    port, clock = make_port(timeout=5.0)
    data = port.read(TELEMETRY_FRAME_LEN)
    assert clock.monotonic() == 0.5
    assert FrameDecoder().feed(data)[0].register == 0xC3


def test_zero_timeout_read_does_not_wait():
    port, clock = make_port(timeout=0)
    assert port.read(10) == b""
    assert clock.monotonic() == 0.0


def test_pushed_bytes_come_out_in_order_and_split_reads_still_decode():
    port, clock = make_port(timeout=0)
    port.push(bytes.fromhex("F0 A1 DB 01 01 DD"))
    clock.sleep(0.5)
    decoder = FrameDecoder()
    frames = []
    while chunk := port.read(3):
        frames += decoder.feed(chunk)
    assert [f.register for f in frames] == [0xDB, 0xC3]
