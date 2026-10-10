"""Hardware-free stand-ins for driver tests: a fake clock and a mock DPS-150 serial port."""

import time
from typing import Protocol

from .protocol import (
    ALLOWED_COMMANDS,
    CMD_GET,
    CMD_SET,
    DEVICE_PREFIX,
    HOST_HEADER,
    checksum,
    pack_f32,
)

TELEMETRY_REGISTER = 0xC3
OUTPUT_REGISTER = 0xDB
PROTECTION_REGISTER = 0xDC
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
    A host frame whose command is outside ALLOWED_COMMANDS (0xC0 enters DFU on the
    real device) raises AssertionError so the test sending it fails.

    Fault injection: a SET of a register in `drop_sets` is recorded but not stored,
    like a device that refuses the write; a GET of a register in `silent` gets no reply;
    with `echo_sets` every valid SET is echoed as a device frame carrying the written
    payload, stored or not, like a device whose echo says nothing about acceptance.

    A stored `SET DB=1` resets the protection register DC to 0, as the DPS-150 V1.2 does
    after a trip (bench #13); `SET DB=0` leaves DC as it is.
    """

    def __init__(
        self, clock: Clock = time, timeout: float | None = 1.0, *, echo_sets: bool = False
    ) -> None:
        self.timeout = timeout
        self.echo_sets = echo_sets
        self.telemetry = (0.0, 0.0, 0.0)
        self.written: list[bytes] = []
        self.registers: dict[int, bytes] = {}
        self.drop_sets: set[int] = set()
        self.silent: set[int] = set()
        self.closed = False
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
        if len(data) >= 2 and data[0] == HOST_HEADER and data[1] not in ALLOWED_COMMANDS:
            raise AssertionError(f"host sent forbidden command 0x{data[1]:02X}: {data.hex(' ')}")
        if _is_valid_host_frame(data):
            command, register, payload = data[1], data[2], data[4:-1]
            if command == CMD_SET:
                if register not in self.drop_sets:
                    self.registers[register] = payload
                    if register == OUTPUT_REGISTER and payload == b"\x01":
                        self.registers[PROTECTION_REGISTER] = b"\x00"
                if self.echo_sets:
                    self.push(_device_frame(register, payload))
            elif command == CMD_GET and register not in self.silent:
                self._reply(register)
        return len(data)

    def _reply(self, register: int) -> None:
        if register == TELEMETRY_REGISTER:
            self.push(_device_frame(register, _telemetry_payload(self.telemetry)))
        elif register in self.registers:
            self.push(_device_frame(register, self.registers[register]))

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

    def reset_input_buffer(self) -> None:
        self._rx.clear()

    def close(self) -> None:
        self.closed = True

    def _push_due_telemetry(self) -> None:
        while self._clock.monotonic() >= self._next_push:
            self.push(_device_frame(TELEMETRY_REGISTER, _telemetry_payload(self.telemetry)))
            self._next_push += TELEMETRY_PERIOD_S


def _telemetry_payload(telemetry: tuple[float, float, float]) -> bytes:
    return b"".join(map(pack_f32, telemetry))


def _device_frame(register: int, data: bytes) -> bytes:
    return DEVICE_PREFIX + bytes([register, len(data), *data, checksum(register, data)])


def _is_valid_host_frame(frame: bytes) -> bool:
    return (
        len(frame) >= 5
        and frame[0] == HOST_HEADER
        and len(frame) == 5 + frame[3]
        and frame[-1] == checksum(frame[2], frame[4:-1])
    )
