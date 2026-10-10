"""Hardware-free stand-ins for driver tests: a fake clock and a mock DPS-150 serial port."""

import time
from typing import Protocol

from .protocol import CMD_GET, CMD_SET, DEVICE_PREFIX, HOST_HEADER, checksum, pack_f32

TELEMETRY_REGISTER = 0xC3
TELEMETRY_PERIOD_S = 0.5


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class FakeClock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += seconds


class MockSerial:
    """A pyserial-like port that behaves like a DPS-150 on the wire.

    Pushes a C3 telemetry frame (output V, I, P as float32) every 500 ms of clock
    time. A valid SET stores its payload in `registers`; a valid GET of a stored
    register replies with it. A SET is not echoed: sources disagree on whether the
    real device echoes, so drivers must read back with GET (see issue #6). Tests
    inject any other device bytes with `push` and inspect host writes in `written`.
    """

    def __init__(self, clock: Clock = time, timeout: float | None = 1.0) -> None:
        self.timeout = timeout
        self.telemetry = (0.0, 0.0, 0.0)
        self.written: list[bytes] = []
        self.registers: dict[int, bytes] = {}
        self._clock = clock
        self._rx = bytearray()
        self._next_push = clock.monotonic() + TELEMETRY_PERIOD_S

    @property
    def in_waiting(self) -> int:
        self._push_due_telemetry()
        return len(self._rx)

    def push(self, data: bytes) -> None:
        self._rx += data

    def write(self, data: bytes) -> int:
        data = bytes(data)
        self.written.append(data)
        if _is_valid_host_frame(data):
            command, register, payload = data[1], data[2], data[4:-1]
            if command == CMD_SET:
                self.registers[register] = payload
            elif command == CMD_GET and register in self.registers:
                self.push(_device_frame(register, self.registers[register]))
        return len(data)

    def read(self, size: int = 1) -> bytes:
        deadline = None if self.timeout is None else self._clock.monotonic() + self.timeout
        while self.in_waiting < size:
            now = self._clock.monotonic()
            wait = self._next_push - now
            if deadline is not None:
                if now >= deadline:
                    break
                wait = min(wait, deadline - now)
            self._clock.sleep(wait)
        data = bytes(self._rx[:size])
        del self._rx[:size]
        return data

    def _push_due_telemetry(self) -> None:
        while self._clock.monotonic() >= self._next_push:
            self.push(_device_frame(TELEMETRY_REGISTER, b"".join(map(pack_f32, self.telemetry))))
            self._next_push += TELEMETRY_PERIOD_S


def _device_frame(register: int, data: bytes) -> bytes:
    return DEVICE_PREFIX + bytes([register, len(data), *data, checksum(register, data)])


def _is_valid_host_frame(frame: bytes) -> bool:
    return (
        len(frame) >= 5
        and frame[0] == HOST_HEADER
        and len(frame) == 5 + frame[3]
        and frame[-1] == checksum(frame[2], frame[4:-1])
    )
