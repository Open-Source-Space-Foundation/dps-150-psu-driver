"""`dps150`: one-shot commands for a DPS-150, Korad-compatible at its core (`-d PORT --output 0|1`).

Exit codes and the choices behind each sequence are documented in README.md ("CLI").
"""

from __future__ import annotations

import argparse
import enum
import logging
import math
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import state
from .driver import (
    Dps150,
    Dps150Error,
    Limit,
    LimitError,
    ProtectionTripped,
    Snapshot,
    WrongDevice,
    _validated_limit,
    force_output_off,
    open_serial,
    read_status,
    usb_id,
    usb_serial,
)
from .lease import DEFAULT_RUN_DIR, MAX_LEASE_S, clear_lease, read_lease, write_lease
from .state import DEFAULT_STATE_DIR, Inhibited, PortLockTimeout, port_lock

EXIT_OK = 0
EXIT_DEVICE = 1
EXIT_USAGE = 2
EXIT_INHIBITED = 3
EXIT_LOCK_BUSY = 4
EXIT_WRONG_DEVICE = 5
EXIT_LEASE_DIR = 6

LOCK_TIMEOUT_S = state.DEFAULT_LOCK_TIMEOUT_S
KILL_LOCK_TIMEOUT_S = 60.0


class Command(enum.Enum):
    STATUS = "status"
    OUTPUT_ON = "output_on"
    OUTPUT_OFF = "output_off"
    KILL = "kill"
    CLEAR_INHIBIT = "clear_inhibit"


class CliError(Exception):
    code = EXIT_DEVICE


class UsageError(CliError):
    code = EXIT_USAGE


class NoSerial(CliError):
    code = EXIT_WRONG_DEVICE


class MissingPort(CliError):
    code = EXIT_WRONG_DEVICE


class LeaseDirUnusable(CliError):
    code = EXIT_LEASE_DIR


class KillIncomplete(CliError):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


# Order matters: LimitError and WrongDevice are Dps150Errors.
EXIT_CODES: tuple[tuple[type[Exception], int], ...] = (
    (LimitError, EXIT_USAGE),
    (Inhibited, EXIT_INHIBITED),
    (PortLockTimeout, EXIT_LOCK_BUSY),
    (WrongDevice, EXIT_WRONG_DEVICE),
    (Dps150Error, EXIT_DEVICE),
    (OSError, EXIT_DEVICE),
)

HANDLED = (CliError, *(kind for kind, _ in EXIT_CODES))


def exit_code(exc: Exception) -> int:
    if isinstance(exc, CliError):
        return exc.code
    return next((code for kind, code in EXIT_CODES if isinstance(exc, kind)), EXIT_DEVICE)


@dataclass(frozen=True)
class Config:
    port: str
    limit: Limit | None
    """Parsed only for output-on: no other command may fail on a broken limit."""
    voltage: float | None
    current: float | None
    lease_s: float | None
    state_dir: Path
    run_dir: Path


@dataclass(frozen=True)
class Device:
    serial_factory: Callable
    usb_id: Callable
    usb_serial: Callable
    clock: object

    def session(self, port: str, limit: Limit) -> Dps150:
        return Dps150(
            port,
            limit.voltage,
            limit.current,
            serial_factory=self.serial_factory,
            usb_id=self.usb_id,
            usb_serial=self.usb_serial,
            clock=self.clock,
        )

    def output_off(self, port: str) -> None:
        force_output_off(
            port, serial_factory=self.serial_factory, usb_id=self.usb_id, clock=self.clock
        )

    def read_status(self, port: str) -> Snapshot:
        return read_status(
            port, serial_factory=self.serial_factory, usb_id=self.usb_id, clock=self.clock
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dps150", description="Control a FNIRSI DPS-150.")
    parser.add_argument("-d", "--device", help="serial port (default: PSU_PORT)")
    parser.add_argument("-v", "--voltage", type=float, help="voltage setpoint (PSU_VOLTAGE)")
    parser.add_argument("-i", "--current", type=float, help="current setpoint (PSU_CURRENT)")
    parser.add_argument("--lease", type=float, help="lease in seconds (PSU_LEASE_S)")
    parser.add_argument("--max-voltage", type=float, help="lower PSU_MAX_V for output on")
    parser.add_argument("--max-current", type=float, help="lower PSU_MAX_I for output on")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--output", type=str.lower, choices=["0", "1", "off", "on"])
    mode.add_argument("--status", action="store_true", help="print status (the default)")
    commands = parser.add_subparsers(dest="command")
    kill = commands.add_parser("kill", help="output off and set the inhibit latch")
    kill.add_argument("--reason", default="kill")
    clear = commands.add_parser("clear-inhibit", help="remove the inhibit latch")
    # The panic command must not fail on option order: `dps150 kill -d PORT` works too.
    for sub in (kill, clear):
        sub.add_argument("-d", "--device", default=argparse.SUPPRESS, help="serial port")
        sub.add_argument("--state-dir", type=Path, default=argparse.SUPPRESS)
        sub.add_argument("--run-dir", type=Path, default=argparse.SUPPRESS)
    return parser


def parse_command(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Command:
    if args.command is not None and (args.output is not None or args.status):
        parser.error(f"{args.command} cannot be combined with --output or --status")
    if args.command == "kill":
        return Command.KILL
    if args.command == "clear-inhibit":
        return Command.CLEAR_INHIBIT
    if args.output in ("1", "on"):
        return Command.OUTPUT_ON
    if args.output in ("0", "off"):
        return Command.OUTPUT_OFF
    return Command.STATUS


def parse_config(args: argparse.Namespace, env: Mapping[str, str], command: Command) -> Config:
    port = args.device or env.get("PSU_PORT")
    if not port:
        raise UsageError("no port: pass -d PORT or set PSU_PORT")
    limit = voltage = current = lease_s = None
    if command is Command.OUTPUT_ON:
        limit = parse_limit(args, env)
        voltage = _setting(args.voltage, env, "PSU_VOLTAGE", "-v")
        current = _setting(args.current, env, "PSU_CURRENT", "-i")
        lease_s = _setting(args.lease, env, "PSU_LEASE_S", "--lease")
        if not 0 < lease_s <= MAX_LEASE_S:
            raise UsageError(f"lease must be between 0 and {MAX_LEASE_S} seconds, got {lease_s:g}")
    return Config(port, limit, voltage, current, lease_s, args.state_dir, args.run_dir)


def parse_limit(args: argparse.Namespace, env: Mapping[str, str]) -> Limit:
    try:
        env_limit = Limit(float(env["PSU_MAX_V"]), float(env["PSU_MAX_I"]))
    except (KeyError, ValueError):
        env_limit = None
    if env_limit is None or not all(
        math.isfinite(x) and x > 0 for x in (env_limit.voltage, env_limit.current)
    ):
        raise UsageError(
            "no limit configured: set PSU_MAX_V and PSU_MAX_I to positive numbers sized to "
            "the connected hardware"
        )
    # A lowering flag must not hide an env limit above the ceiling.
    env_limit = _validated_limit(env_limit.voltage, env_limit.current)
    voltage, current = env_limit.voltage, env_limit.current
    if args.max_voltage is not None:
        if not args.max_voltage <= env_limit.voltage:
            raise UsageError(
                f"--max-voltage {args.max_voltage:g} is above PSU_MAX_V {env_limit.voltage:g}: "
                "flags may lower a limit, never raise it"
            )
        voltage = args.max_voltage
    if args.max_current is not None:
        if not args.max_current <= env_limit.current:
            raise UsageError(
                f"--max-current {args.max_current:g} is above PSU_MAX_I {env_limit.current:g}: "
                "flags may lower a limit, never raise it"
            )
        current = args.max_current
    return Limit(voltage, current)


def _setting(flag: float | None, env: Mapping[str, str], name: str, option: str) -> float:
    if flag is not None:
        return flag
    raw = env.get(name)
    if raw is None:
        raise UsageError(f"--output 1 needs {option} or {name}")
    try:
        return float(raw)
    except ValueError:
        raise UsageError(f"{name}={raw!r} is not a number") from None


def _missing_port(port: str) -> MissingPort | None:
    if os.path.exists(port):
        return None
    return MissingPort(f"port {port} does not exist (supply unplugged or wrong PSU_PORT)")


def require_port(port: str) -> None:
    missing = _missing_port(port)
    if missing is not None:
        raise missing


def require_serial(serial: str | None, port: str) -> str:
    if serial is None:
        raise NoSerial(f"{port}: no USB serial number; cannot key the inhibit latch or lease")
    return serial


def output_on(cfg: Config, serial: str | None, device: Device) -> None:
    require_port(cfg.port)
    serial = require_serial(serial, cfg.port)
    assert cfg.limit and cfg.voltage is not None and cfg.current is not None and cfg.lease_s
    # Checked before the device so a missing watchdog fails closed and a root run
    # cannot create the run dir with root-owned leases the watchdog cannot read.
    if not cfg.run_dir.is_dir() or not os.access(cfg.run_dir, os.W_OK):
        raise LeaseDirUnusable(
            f"{cfg.run_dir} is not a writable directory: "
            "dps150-watchdog not installed or not running (see README)"
        )
    with port_lock(cfg.port, LOCK_TIMEOUT_S):
        state.check_not_inhibited(serial, cfg.state_dir)
        with device.session(cfg.port, cfg.limit) as supply:
            supply.set_voltage(cfg.voltage)
            supply.set_current(cfg.current)
            state.check_not_inhibited(serial, cfg.state_dir)
            ack = state.read_ack(serial, cfg.state_dir)
            lease = write_lease(serial, cfg.port, cfg.lease_s, cfg.run_dir)
            # A failure from here on leaves the lease in place on purpose (README, "CLI").
            # The acknowledgement is spent before DB=1: a trip after this point is a new
            # one, and it must not find an acknowledgement waiting for the next run.
            state.consume_ack(serial, cfg.state_dir)
            try:
                supply.output(True, allow_tripped=None if ack is None else ack.trip)
            except ProtectionTripped as exc:
                try:
                    latch = _latch_trip(serial, exc, cfg.state_dir)
                except OSError as err:
                    exc.add_note(f"inhibit latch not written: {err!r}")
                    raise exc from err
                raise Inhibited(serial, latch) from exc
    print(
        f"output on: {cfg.voltage:g} V / {cfg.current:g} A, "
        f"lease until {lease.deadline.isoformat()}"
    )


def _latch_trip(serial: str, exc: ProtectionTripped, state_dir: Path) -> state.Latch:
    """A device protection trip escalates to the inhibit latch (ADR 0001, 2026-10-10 note)."""
    reason = f"device protection tripped: {exc.name}"
    return state.set_latch(serial, state._current_user(), reason, state_dir, trip=exc.name)


def output_off(cfg: Config, serial: str | None, device: Device) -> None:
    require_port(cfg.port)
    with port_lock(cfg.port, LOCK_TIMEOUT_S):
        device.output_off(cfg.port)
        if serial is not None:
            clear_lease(serial, cfg.run_dir)
    print("output off")
    if serial is None:
        raise NoSerial(f"output off confirmed, but {cfg.port} has no USB serial number")


def kill(cfg: Config, serial: str | None, device: Device, reason: str) -> None:
    confirmed = False

    def confirmed_off() -> None:
        nonlocal confirmed
        device.output_off(cfg.port)
        confirmed = True

    # The latch is set before the port is touched, so a missing port is found only
    # when the lock opens it. The latch must stay: the supply may be plugged back in.
    try:
        state.kill(
            cfg.port,
            serial or "",
            confirmed_off,
            reason=reason,
            state_dir=cfg.state_dir,
            lock_timeout=KILL_LOCK_TIMEOUT_S,
        )
    except Exception as exc:
        if confirmed and serial is not None:
            clear_lease(serial, cfg.run_dir)
        cause = exc if confirmed else _missing_port(cfg.port) or exc
        raise _kill_incomplete(cfg, serial, confirmed, cause) from exc
    clear_lease(serial, cfg.run_dir)
    print(f"killed: output off, inhibit latch set for supply {serial}")


def _kill_incomplete(
    cfg: Config, serial: str | None, confirmed: bool, exc: Exception
) -> KillIncomplete:
    latched = serial is not None and os.path.lexists(cfg.state_dir / f"{serial}.inhibit")
    latch = "INHIBIT LATCH IS SET" if latched else "INHIBIT LATCH IS NOT SET"
    who = f"supply {serial}" if serial is not None else f"{cfg.port} (no USB serial number)"
    code = exit_code(exc) if serial is not None else EXIT_WRONG_DEVICE
    error = "; ".join([str(exc), *getattr(exc, "__notes__", ())])
    if confirmed:
        return KillIncomplete(f"output off confirmed but {latch} for {who}: {error}", code)
    return KillIncomplete(
        f"{latch} for {who} but the OUTPUT MAY STILL BE ON: {error}. "
        "Free the port and run `dps150 kill` again.",
        code,
    )


def clear_inhibit(cfg: Config, serial: str | None) -> None:
    require_port(cfg.port)
    serial = require_serial(serial, cfg.port)
    latch = state.clear_latch(serial, who=state._current_user(), state_dir=cfg.state_dir)
    if latch is None:
        print(f"supply {serial}: no inhibit latch was set")
    else:
        print(
            f"supply {serial}: inhibit latch cleared "
            f"(set by {latch.who} at {latch.when.isoformat()}: {latch.reason})"
        )
        if latch.trip is not None:
            print(
                f"trip {latch.trip} acknowledged: the next output-on may proceed while the "
                f"supply still reports {latch.trip}; output-on is what clears it"
            )


def status(cfg: Config, serial: str | None, device: Device) -> None:
    require_port(cfg.port)
    serial = require_serial(serial, cfg.port)
    with port_lock(cfg.port, LOCK_TIMEOUT_S):
        snap = device.read_status(cfg.port)
    st, m = snap.status, snap.measurement
    latch = state.read_latch(serial, cfg.state_dir)
    ack = state.read_ack(serial, cfg.state_dir)
    lease = read_lease(serial, cfg.run_dir)
    if latch is not None:
        inhibit_line = f"latched by {latch.who} at {latch.when.isoformat()}: {latch.reason}"
    elif ack is not None:
        inhibit_line = (
            f"clear (trip {ack.trip} acknowledged by {ack.who} at {ack.when.isoformat()}; "
            "the next output-on may proceed)"
        )
    else:
        inhibit_line = "clear"
    if lease is None:
        lease_line = "none"
    elif lease.deadline is None:
        lease_line = "unreadable (expired)"
    else:
        expired = " (expired)" if lease.expired(datetime.now(UTC)) else ""
        lease_line = f"until {lease.deadline.isoformat()}{expired}"
    lines = {
        "model": snap.model,
        "firmware": snap.firmware,
        "voltage_setpoint": f"{st.voltage_setpoint:.3f}",
        "current_setpoint": f"{st.current_setpoint:.3f}",
        "output": "on" if st.output else "off",
        "protection": f"{st.protection_name} (tripped)" if st.tripped else st.protection_name,
        "mode": st.mode,
        "measured_voltage": f"{m.voltage:.3f}",
        "measured_current": f"{m.current:.3f}",
        "measured_power": f"{m.power:.3f}",
        "ovp": f"{snap.ovp:.3f}",
        "ocp": f"{snap.ocp:.3f}",
        "max_voltage": f"{snap.max_voltage:.3f}",
        "inhibit": inhibit_line,
        "lease": lease_line,
    }
    for key, value in lines.items():
        print(f"{key}: {value}")


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    serial_factory: Callable = open_serial,
    usb_id: Callable = usb_id,
    usb_serial: Callable = usb_serial,
    clock=time,
) -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="dps150: %(message)s")
    env = os.environ if env is None else env
    device = Device(serial_factory, usb_id, usb_serial, clock)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        command = parse_command(parser, args)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    try:
        cfg = parse_config(args, env, command)
        serial = usb_serial(cfg.port)
        match command:
            case Command.STATUS:
                status(cfg, serial, device)
            case Command.OUTPUT_ON:
                output_on(cfg, serial, device)
            case Command.OUTPUT_OFF:
                output_off(cfg, serial, device)
            case Command.KILL:
                kill(cfg, serial, device, args.reason)
            case Command.CLEAR_INHIBIT:
                clear_inhibit(cfg, serial)
    except HANDLED as exc:
        print(f"dps150: {exc}", file=sys.stderr)
        for note in getattr(exc, "__notes__", ()):
            print(f"dps150: {note}", file=sys.stderr)
        return exit_code(exc)
    return EXIT_OK
