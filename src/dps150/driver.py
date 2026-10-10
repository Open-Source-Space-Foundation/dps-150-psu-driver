"""The `Dps150` class: one session with one supply, with the ceiling, clamp, and
device-protection guarantees of ADR 0001 layers 1 and 2.

Every write is confirmed by an explicit GET of the register, never by an echo or a
pushed frame. Closing the session leaves the output as it is: only `output(False)`
turns it off.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .protocol import CMD_GET, CMD_SESSION, CMD_SET, FrameDecoder, encode, pack_f32, unpack_f32

USB_VID = 0x2E3C
USB_PID = 0x5740
BAUD = 115200
COMMAND_SPACING_S = 0.05
POLL_TIMEOUT_S = 0.05
REPLY_TIMEOUT_S = 1.0
GET_ATTEMPTS = 3
VOLTAGE_TOLERANCE_V = 0.01
CURRENT_TOLERANCE_A = 0.001

REG_VOLTAGE_SETPOINT = 0xC1
REG_CURRENT_SETPOINT = 0xC2
REG_MEASUREMENT = 0xC3
REG_OVP = 0xD1
REG_OCP = 0xD2
REG_OUTPUT = 0xDB
REG_PROTECTION = 0xDC
REG_CC_CV = 0xDD
REG_MODEL = 0xDE
REG_FIRMWARE = 0xE0
REG_MAX_VOLTAGE = 0xE2

PROTECTION_NAMES = {0: "OK", 1: "OVP", 2: "OCP", 3: "OPP", 4: "OTP", 5: "LVP", 6: "REP"}


@dataclass(frozen=True, slots=True)
class Limit:
    voltage: float
    current: float


CEILING = Limit(10.0, 3.0)


class Dps150Error(Exception):
    pass


class LimitError(Dps150Error, ValueError):
    """No limit configured, or a limit outside (0, ceiling]."""


class ClampError(Dps150Error, ValueError):
    """A setpoint the clamp rejects: above the limit, above the supply's E2, or not a value."""


class WrongDevice(Dps150Error):
    """The port is not a DPS-150 (USB VID:PID 2e3c:5740)."""


class NoReply(Dps150Error):
    """The supply did not answer a GET within the timeout, after retries."""


class ReadbackMismatch(Dps150Error):
    """A register read back a value other than the one written."""


@dataclass(frozen=True, slots=True)
class Measurement:
    voltage: float
    current: float
    power: float


@dataclass(frozen=True, slots=True)
class Status:
    voltage_setpoint: float
    current_setpoint: float
    output: bool
    protection: int
    mode: str

    @property
    def protection_name(self) -> str:
        return PROTECTION_NAMES.get(self.protection, f"UNKNOWN({self.protection})")


class SerialPort(Protocol):
    """The slice of pyserial's Serial that the driver uses."""

    timeout: float | None

    @property
    def in_waiting(self) -> int: ...
    def read(self, size: int = 1) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def reset_input_buffer(self) -> None: ...
    def close(self) -> None: ...


def usb_id(path: str | os.PathLike[str]) -> tuple[int, int] | None:
    """USB (vid, pid) of the serial device at `path`, following by-id symlinks."""
    from serial.tools import list_ports

    device = os.path.realpath(path)
    for info in list_ports.comports():
        if os.path.realpath(info.device) == device and info.vid is not None:
            return (info.vid, info.pid)
    return None


def open_serial(path: str | os.PathLike[str]) -> SerialPort:
    import serial

    # No `exclusive=True`: the port lock in dps150.state flocks its own descriptor.
    return serial.Serial(os.fspath(path), baudrate=BAUD, timeout=POLL_TIMEOUT_S, rtscts=False)


class Dps150:
    """A session with one DPS-150.

    `open()` checks the USB identity, starts the session, reads the model and firmware,
    and programs the supply's OVP/OCP from the limit, reading them back. `close()`
    stops the session and does not change the output. Only `output(False)` turns the
    output off.
    """

    def __init__(
        self,
        port: str | os.PathLike[str],
        max_voltage: float | None = None,
        max_current: float | None = None,
        *,
        serial_factory: Callable[[str | os.PathLike[str]], SerialPort] = open_serial,
        usb_id: Callable[[str | os.PathLike[str]], tuple[int, int] | None] = usb_id,
        clock=time,
    ) -> None:
        self.port = port
        self.limit = _validated_limit(max_voltage, max_current)
        self.model: str | None = None
        self.firmware: str | None = None
        self._serial_factory = serial_factory
        self._usb_id = usb_id
        self._clock = clock
        self._serial: SerialPort | None = None
        self._decoder = FrameDecoder()
        self._last_tx = -math.inf
        self._declared_voltage: float | None = None
        self._declared_current: float | None = None

    def __enter__(self) -> Dps150:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._serial is not None

    def open(self) -> None:
        if self._serial is not None:
            return
        ids = self._usb_id(self.port)
        if ids != (USB_VID, USB_PID):
            found = "no USB identity" if ids is None else f"{ids[0]:04x}:{ids[1]:04x}"
            raise WrongDevice(f"{self.port}: expected {USB_VID:04x}:{USB_PID:04x}, found {found}")
        self._serial = self._serial_factory(self.port)
        self._serial.timeout = POLL_TIMEOUT_S
        try:
            self._send(CMD_SESSION, 0x00, b"\x01")
            # Bytes queued before the session can hold a false F0 A1 header that stalls
            # the decoder; drop them once the device has had a command interval to settle.
            self._clock.sleep(COMMAND_SPACING_S)
            self._serial.reset_input_buffer()
            self._decoder = FrameDecoder()
            self.model = self._get_str(REG_MODEL)
            self.firmware = self._get_str(REG_FIRMWARE)
            self._program_protection()
        except Exception:
            self._end_session()
            raise

    def close(self) -> None:
        """Stop the session. The output keeps its state; see `output(False)`."""
        if self._serial is not None:
            self._end_session()

    def set_voltage(self, volts: float) -> None:
        self._require_open()
        _clamp(volts, self.limit.voltage, "V")
        max_voltage = self._get_f32(REG_MAX_VOLTAGE)
        if volts > max_voltage:
            raise ClampError(
                f"{volts} V is above the supply's input-limited maximum {max_voltage:.2f} V"
            )
        self._send(CMD_SET, REG_VOLTAGE_SETPOINT, pack_f32(volts))
        self._verify_f32(REG_VOLTAGE_SETPOINT, volts, VOLTAGE_TOLERANCE_V, "voltage setpoint")
        self._declared_voltage = volts

    def set_current(self, amps: float) -> None:
        self._require_open()
        _clamp(amps, self.limit.current, "A")
        self._send(CMD_SET, REG_CURRENT_SETPOINT, pack_f32(amps))
        self._verify_f32(REG_CURRENT_SETPOINT, amps, CURRENT_TOLERANCE_A, "current setpoint")
        self._declared_current = amps

    def output(self, on: bool) -> None:
        """Switch the output, confirmed by reading DB back.

        Output-on first re-verifies OVP/OCP and the setpoints on the supply. Any failure
        on the way to a confirmed "on" writes DB=0 and raises.
        """
        self._require_open()
        if not on:
            self._send(CMD_SET, REG_OUTPUT, b"\x00")
            self._verify_u8(REG_OUTPUT, 0, "output")
            return
        try:
            self._verify_protection()
            self._verify_setpoints()
            self._send(CMD_SET, REG_OUTPUT, b"\x01")
            self._verify_u8(REG_OUTPUT, 1, "output")
        except Exception:
            self._send(CMD_SET, REG_OUTPUT, b"\x00")
            self._verify_u8(REG_OUTPUT, 0, "output")
            raise

    def measure(self) -> Measurement:
        self._require_open()
        data = self._get(REG_MEASUREMENT)
        if len(data) < 12:
            raise Dps150Error(f"measurement frame has {len(data)} bytes, expected 12")
        return Measurement(unpack_f32(data, 0), unpack_f32(data, 4), unpack_f32(data, 8))

    def status(self) -> Status:
        self._require_open()
        return Status(
            voltage_setpoint=self._get_f32(REG_VOLTAGE_SETPOINT),
            current_setpoint=self._get_f32(REG_CURRENT_SETPOINT),
            output=self._get_u8(REG_OUTPUT) != 0,
            protection=self._get_u8(REG_PROTECTION),
            mode="CC" if self._get_u8(REG_CC_CV) == 0 else "CV",
        )

    def _program_protection(self) -> None:
        self._send(CMD_SET, REG_OVP, pack_f32(self.limit.voltage))
        self._send(CMD_SET, REG_OCP, pack_f32(self.limit.current))
        self._verify_protection()

    def _verify_protection(self) -> None:
        self._verify_f32(REG_OVP, self.limit.voltage, VOLTAGE_TOLERANCE_V, "OVP")
        self._verify_f32(REG_OCP, self.limit.current, CURRENT_TOLERANCE_A, "OCP")

    def _verify_setpoints(self) -> None:
        volts = self._get_f32(REG_VOLTAGE_SETPOINT)
        amps = self._get_f32(REG_CURRENT_SETPOINT)
        max_voltage = self._get_f32(REG_MAX_VOLTAGE)
        for value, declared, tolerance, what in (
            (volts, self._declared_voltage, VOLTAGE_TOLERANCE_V, "voltage setpoint"),
            (amps, self._declared_current, CURRENT_TOLERANCE_A, "current setpoint"),
        ):
            if declared is not None and abs(value - declared) > tolerance:
                raise ReadbackMismatch(f"{what}: declared {declared}, supply holds {value}")
        if volts > self.limit.voltage + VOLTAGE_TOLERANCE_V:
            raise ClampError(f"supply holds {volts} V, above the {self.limit.voltage} V limit")
        if amps > self.limit.current + CURRENT_TOLERANCE_A:
            raise ClampError(f"supply holds {amps} A, above the {self.limit.current} A limit")
        if volts > max_voltage:
            raise ClampError(f"supply holds {volts} V, above its input-limited {max_voltage:.2f} V")

    def _require_open(self) -> None:
        if self._serial is None:
            raise Dps150Error(f"{self.port}: not open")

    def _end_session(self) -> None:
        serial, self._serial = self._serial, None
        assert serial is not None
        try:
            self._space_commands()
            serial.write(encode(CMD_SESSION, 0x00, b"\x00"))
        finally:
            serial.close()

    def _space_commands(self) -> None:
        wait = self._last_tx + COMMAND_SPACING_S - self._clock.monotonic()
        if wait > 0:
            self._clock.sleep(wait)

    def _send(self, command: int, register: int, data: bytes) -> None:
        assert self._serial is not None
        self._space_commands()
        self._serial.write(encode(command, register, data))
        self._last_tx = self._clock.monotonic()

    def _get(self, register: int) -> bytes:
        """GET a register and return the payload of its reply.

        Each attempt starts a fresh decoder: a false F0 A1 header with a large length
        byte in earlier bytes would otherwise hold the real reply back for seconds.
        """
        for _ in range(GET_ATTEMPTS):
            self._decoder = FrameDecoder()
            self._send(CMD_GET, register, b"\x00")
            data = self._await(register, REPLY_TIMEOUT_S)
            if data is not None:
                return data
        raise NoReply(f"register 0x{register:02X}: no reply in {GET_ATTEMPTS} attempts")

    def _await(self, register: int, timeout: float) -> bytes | None:
        assert self._serial is not None
        deadline = self._clock.monotonic() + timeout
        while True:
            chunk = self._serial.read(max(1, self._serial.in_waiting))
            for frame in self._decoder.feed(chunk):
                if frame.register == register:
                    return frame.data
            if self._clock.monotonic() >= deadline:
                return None

    def _get_str(self, register: int) -> str:
        return self._get(register).split(b"\0", 1)[0].decode("ascii", errors="replace")

    def _get_f32(self, register: int) -> float:
        data = self._get(register)
        if len(data) != 4:
            raise Dps150Error(f"register 0x{register:02X} replied {len(data)} bytes, expected 4")
        return unpack_f32(data)

    def _get_u8(self, register: int) -> int:
        data = self._get(register)
        if len(data) != 1:
            raise Dps150Error(f"register 0x{register:02X} replied {len(data)} bytes, expected 1")
        return data[0]

    def _verify_f32(self, register: int, expected: float, tolerance: float, what: str) -> None:
        actual = self._get_f32(register)
        if abs(actual - expected) > tolerance:
            raise ReadbackMismatch(f"{what}: wrote {expected}, supply reads {actual}")

    def _verify_u8(self, register: int, expected: int, what: str) -> None:
        actual = self._get_u8(register)
        if actual != expected:
            raise ReadbackMismatch(f"{what}: wrote {expected}, supply reads {actual}")


def _validated_limit(max_voltage: float | None, max_current: float | None) -> Limit:
    if max_voltage is None or max_current is None:
        raise LimitError("no limit configured: pass max_voltage and max_current")
    checks = ((max_voltage, CEILING.voltage, "V"), (max_current, CEILING.current, "A"))
    for value, ceiling, unit in checks:
        if not math.isfinite(value) or value <= 0:
            raise LimitError(f"limit {value} {unit} is not a positive number")
        if value > ceiling:
            raise LimitError(f"limit {value} {unit} is above the {ceiling} {unit} ceiling")
    return Limit(float(max_voltage), float(max_current))


def _clamp(value: float, limit: float, unit: str) -> None:
    if not math.isfinite(value) or value < 0:
        raise ClampError(f"setpoint {value} {unit} is not a non-negative number")
    if value > limit:
        raise ClampError(f"setpoint {value} {unit} is above the {limit} {unit} limit")
