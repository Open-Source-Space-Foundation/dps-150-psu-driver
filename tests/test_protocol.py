import struct

import pytest

from dps150.protocol import (
    CMD_BAUD,
    CMD_GET,
    CMD_SESSION,
    CMD_SET,
    Frame,
    FrameDecoder,
    checksum,
    encode,
    pack_f32,
    unpack_f32,
)


def device_frame(register: int, data: bytes) -> bytes:
    chk = (register + len(data) + sum(data)) & 0xFF
    return bytes([0xF0, 0xA1, register, len(data), *data, chk])


def test_session_start_frame_matches_known_bytes():
    assert encode(CMD_SESSION, 0x00, b"\x01") == bytes.fromhex("F1 C1 00 01 01 02")


def test_get_frame_with_single_zero_byte():
    assert encode(CMD_GET, 0xFF, b"\x00") == bytes.fromhex("F1 A1 FF 01 00 00")


def test_set_float_frame_layout():
    data = struct.pack("<f", 5.0)
    frame = encode(CMD_SET, 0xC1, data)
    assert frame[:4] == bytes([0xF1, 0xB1, 0xC1, 4])
    assert frame[4:8] == data
    assert frame[8] == (0xC1 + 4 + sum(data)) & 0xFF
    assert len(frame) == 9


def test_checksum_wraps_modulo_256():
    assert checksum(0xC1, b"\x80\x80") == 0xC3


def test_encode_allows_empty_payload():
    assert encode(CMD_GET, 0xC1, b"") == bytes.fromhex("F1 A1 C1 00 C1")


def test_encode_rejects_payload_longer_than_255():
    with pytest.raises(ValueError):
        encode(CMD_SET, 0xC1, bytes(256))


@pytest.mark.parametrize("command", [CMD_GET, CMD_BAUD, CMD_SET, CMD_SESSION])
def test_encode_accepts_every_allowlisted_command(command):
    assert encode(command, 0x00, b"\x00")[1] == command


@pytest.mark.parametrize("command", [0xC0, 0x00, 0xA0, 0xFF])
def test_encode_refuses_commands_outside_the_allowlist(command):
    with pytest.raises(ValueError, match=f"0x{command:02X}"):
        encode(command, 0x00, b"\x00")


def test_swapped_command_and_register_cannot_emit_the_dfu_command():
    with pytest.raises(ValueError, match="0xC0"):
        encode(0xC0, CMD_GET, b"\x00")


def test_float32_little_endian_round_trip():
    assert pack_f32(1.0) == bytes.fromhex("0000803f")
    assert unpack_f32(bytes.fromhex("0000803f")) == 1.0
    assert unpack_f32(b"\x00" * 4 + pack_f32(2.5), offset=4) == 2.5


def test_decodes_back_to_back_frames():
    stream = device_frame(0xC3, pack_f32(5.0) + pack_f32(0.1) + pack_f32(0.5)) + device_frame(
        0xDB, b"\x01"
    )
    frames = FrameDecoder().feed(stream)
    assert frames == [
        Frame(0xC3, pack_f32(5.0) + pack_f32(0.1) + pack_f32(0.5)),
        Frame(0xDB, b"\x01"),
    ]


def test_frame_split_across_every_byte_boundary():
    stream = device_frame(0xC1, pack_f32(3.3)) + device_frame(0xDB, b"\x00")
    for cut in range(len(stream) + 1):
        decoder = FrameDecoder()
        frames = decoder.feed(stream[:cut]) + decoder.feed(stream[cut:])
        assert frames == [Frame(0xC1, pack_f32(3.3)), Frame(0xDB, b"\x00")], cut


def test_byte_at_a_time():
    stream = device_frame(0xC1, pack_f32(3.3))
    decoder = FrameDecoder()
    frames = [f for b in stream for f in decoder.feed(bytes([b]))]
    assert frames == [Frame(0xC1, pack_f32(3.3))]


def test_stream_starting_mid_frame_resyncs_on_next_header():
    full = device_frame(0xC3, pack_f32(5.0) + pack_f32(0.1) + pack_f32(0.5))
    tail_of_previous = full[5:]
    frames = FrameDecoder().feed(tail_of_previous + device_frame(0xDB, b"\x01"))
    assert frames == [Frame(0xDB, b"\x01")]


def test_bad_checksum_frame_is_dropped_and_next_frame_survives():
    bad = bytearray(device_frame(0xC1, pack_f32(3.3)))
    bad[-1] ^= 0xFF
    frames = FrameDecoder().feed(bytes(bad) + device_frame(0xDB, b"\x01"))
    assert frames == [Frame(0xDB, b"\x01")]


def test_false_header_with_huge_length_does_not_swallow_real_frames():
    noise = bytes([0xF0, 0xA1, 0x00, 0xFF])
    real = device_frame(0xC1, pack_f32(3.3)) * 70
    frames = FrameDecoder().feed(noise + real)
    assert frames == [Frame(0xC1, pack_f32(3.3))] * 70


def test_host_frames_and_garbage_are_ignored():
    garbage = b"\x00\xf0\xf0\x12" + encode(CMD_SET, 0xDB, b"\x01")
    frames = FrameDecoder().feed(garbage + device_frame(0xDB, b"\x01"))
    assert frames == [Frame(0xDB, b"\x01")]


def test_trailing_partial_header_is_kept_for_next_feed():
    stream = device_frame(0xDB, b"\x01")
    decoder = FrameDecoder()
    assert decoder.feed(b"\x55\x55" + stream[:1]) == []
    assert decoder.feed(stream[1:]) == [Frame(0xDB, b"\x01")]


def test_buffer_does_not_grow_on_garbage():
    decoder = FrameDecoder()
    for _ in range(1000):
        decoder.feed(b"\x00" * 64)
    assert decoder.pending() <= 1
