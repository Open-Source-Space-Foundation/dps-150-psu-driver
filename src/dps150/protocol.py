"""DPS-150 wire format.

Host frame:   F1 <cmd> <reg> <len> <data...> <chk>
Device frame: F0 A1 <reg> <len> <data...> <chk>
chk = (reg + len + sum(data)) & 0xFF

The device pushes frames unprompted (telemetry every 500 ms), so replies are not
paired with requests: the reader decodes a continuous stream into frames.
"""

import struct
from dataclasses import dataclass

HOST_HEADER = 0xF1
DEVICE_PREFIX = b"\xf0\xa1"

CMD_GET = 0xA1
CMD_BAUD = 0xB0
CMD_SET = 0xB1
CMD_SESSION = 0xC1


@dataclass(frozen=True, slots=True)
class Frame:
    """One checksum-valid frame received from the device."""

    register: int
    data: bytes


def checksum(register: int, data: bytes) -> int:
    return (register + len(data) + sum(data)) & 0xFF


def encode(command: int, register: int, data: bytes) -> bytes:
    if len(data) > 0xFF:
        raise ValueError(f"payload is {len(data)} bytes; the length field holds at most 255")
    return bytes([HOST_HEADER, command, register, len(data), *data, checksum(register, data)])


def pack_f32(value: float) -> bytes:
    return struct.pack("<f", value)


def unpack_f32(data: bytes, offset: int = 0) -> float:
    return struct.unpack_from("<f", data, offset)[0]


class FrameDecoder:
    """Turns an unpaired device byte stream into frames, across arbitrary read splits.

    Scans for the F0 A1 prefix, so it resyncs after a stream that starts mid-frame
    or after line noise. A frame whose checksum fails is dropped by skipping only
    its first byte: a false F0 A1 inside data can claim any length, and discarding
    that whole span would also discard the real frames inside it.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def pending(self) -> int:
        """Bytes held back waiting for the rest of a frame."""
        return len(self._buf)

    def feed(self, chunk: bytes) -> list[Frame]:
        buf = self._buf
        buf += chunk
        frames: list[Frame] = []
        pos = 0
        while True:
            start = buf.find(DEVICE_PREFIX, pos)
            if start < 0:
                keep_tail = len(buf) > pos and buf[-1] == DEVICE_PREFIX[0]
                del buf[: len(buf) - 1 if keep_tail else len(buf)]
                return frames
            if len(buf) - start < 4:
                break
            register, length = buf[start + 2], buf[start + 3]
            end = start + 4 + length
            if end >= len(buf):
                break
            data = bytes(buf[start + 4 : end])
            if buf[end] == checksum(register, data):
                frames.append(Frame(register, data))
                pos = end + 1
            else:
                pos = start + 1
        del buf[:start]
        return frames
