"""`dps150-watchdog`: turn the output off when a lease expires (ADR 0001 layer 4).

Each poll reads every lease in the run dir. For an expired one it takes the port lock,
re-reads the lease (the holder may have renewed while we waited), turns the output off
with `force_output_off`, and removes the lease. Expiry never sets the inhibit latch.
Any failure is logged and retried on the next poll; the loop itself never dies on one.

systemd is petted after every lease, so `WatchdogSec` only has to cover one supply's
worst case: the lock timeout, three unanswered GETs, and a timed-out write (about 6 s).

On start, before the first poll, a sweep visits every DPS-150 on USB and turns off any
whose lease is missing or expired: after a host reboot `/run` is empty, so a supply left
on through the reboot would otherwise have nothing to enforce.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from . import driver
from .lease import DEFAULT_RUN_DIR, Lease, clear_lease, list_leases, read_lease
from .state import port_lock

DEFAULT_POLL_S = 1.0
# Short on purpose: a held lock means a CI job is mid-session, and the loop must keep
# petting systemd. The next poll tries again.
DEFAULT_LOCK_TIMEOUT_S = 1.0

log = logging.getLogger(__name__)


def sd_notify(state: str, notify_socket: str | None = None) -> bool:
    """Send one sd_notify(3) message. Returns False when systemd gave us no socket."""
    path = os.environ.get("NOTIFY_SOCKET") if notify_socket is None else notify_socket
    if not path:
        return False
    if path.startswith("@"):
        path = "\0" + path[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(path)
        sock.send(state.encode())
    return True


def find_port_by_serial(serial: str) -> str | None:
    from serial.tools import list_ports

    for info in list_ports.comports():
        if info.serial_number != serial:
            continue
        if (info.vid, info.pid) == (driver.USB_VID, driver.USB_PID):
            return info.device
    return None


def attached_dps150_ports() -> list[str]:
    """Device paths of every DPS-150 (by USB VID:PID) on the host."""
    from serial.tools import list_ports

    return [
        info.device
        for info in list_ports.comports()
        if (info.vid, info.pid) == (driver.USB_VID, driver.USB_PID)
    ]


class Watchdog:
    def __init__(
        self,
        run_dir: Path = DEFAULT_RUN_DIR,
        *,
        force_output_off: Callable[[str], None] = driver.force_output_off,
        find_port: Callable[[str], str | None] = find_port_by_serial,
        attached: Callable[[], list[str]] = attached_dps150_ports,
        usb_serial: Callable[[str], str | None] = driver.usb_serial,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT_S,
        notify: Callable[[str], object] = sd_notify,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.run_dir = Path(run_dir)
        self._force_output_off = force_output_off
        self._find_port = find_port
        self._attached = attached
        self._usb_serial = usb_serial
        self._now = now
        self._lock_timeout = lock_timeout
        self._notify = notify
        self._sleep = sleep
        self._failing: dict[str, str] = {}

    def tick(self) -> None:
        """One poll over every lease. Never raises on a single supply's failure."""
        for lease in self._rotated(list_leases(self.run_dir)):
            try:
                self._enforce(lease)
            except Exception as exc:
                self._report_failure(lease.serial, f"{type(exc).__name__}: {exc}")
            else:
                self._report_recovery(lease.serial)
            self._notify("WATCHDOG=1")

    def _rotated(self, leases: list[Lease]) -> list[Lease]:
        """Leases in an order that starts somewhere else each second.

        A supply whose enforcement hangs gets the service killed by systemd, which
        restarts it with no memory of the culprit. Rotating on the clock, not on
        in-process state, keeps that supply from heading the list on every restart.
        """
        if not leases:
            return leases
        start = int(self._now().timestamp()) % len(leases)
        return leases[start:] + leases[:start]

    def _enforce(self, lease: Lease) -> None:
        if not lease.expired(self._now()):
            return
        # Only the USB serial number identifies the supply. The path the lease records
        # can belong to a different DPS-150 after a replug, and force_output_off checks
        # VID:PID alone, so following it could cut a supply that holds a live lease.
        port = self._find_port(lease.serial)
        if port is None:
            raise LookupError(f"supply {lease.serial} is not on USB; lease kept")
        with port_lock(port, self._lock_timeout):
            self._enforce_locked(lease.serial, port)

    def sweep(self) -> None:
        """Turn off every attached DPS-150 whose lease is missing or expired.

        Runs once at startup. A port whose lock is held belongs to a job mid-command;
        it is skipped and left to that job's lease.
        """
        for port in self._attached():
            try:
                with port_lock(port, self._lock_timeout):
                    serial = self._usb_serial(port)
                    if serial is None:
                        raise LookupError("no USB serial number")
                    self._enforce_locked(serial, port)
            except Exception as exc:
                log.error("startup sweep skipped %s: %s: %s", port, type(exc).__name__, exc)
            self._notify("WATCHDOG=1")

    def _enforce_locked(self, serial: str, port: str) -> None:
        """Under the port lock: re-read the lease, and turn the output off unless it is live."""
        current = read_lease(serial, self.run_dir)
        if current is not None and not current.expired(self._now()):
            return
        if current is None:
            state = "no lease"
        elif current.deadline is None:
            state = "lease unreadable"
        else:
            state = f"lease expired at {current.deadline.isoformat()}"
        log.log(
            logging.DEBUG if serial in self._failing else logging.WARNING,
            "supply %s on %s: %s; turning output off",
            serial,
            port,
            state,
        )
        self._force_output_off(port)
        if current is not None:
            clear_lease(serial, self.run_dir, only=current)
        log.warning("output off confirmed on supply %s", serial)

    def _report_failure(self, serial: str, message: str) -> None:
        if self._failing.get(serial) == message:
            log.debug("supply %s still failing: %s", serial, message)
            return
        self._failing[serial] = message
        log.error("could not enforce lease for supply %s: %s", serial, message)

    def _report_recovery(self, serial: str) -> None:
        if self._failing.pop(serial, None) is not None:
            log.warning("lease for supply %s enforced or renewed; earlier failure cleared", serial)

    def run(self, stop: threading.Event, poll_s: float = DEFAULT_POLL_S) -> None:
        self._notify("READY=1")
        try:
            self.sweep()
            while not stop.is_set():
                self.tick()
                self._notify("WATCHDOG=1")
                self._sleep(poll_s)
        finally:
            self._notify("STOPPING=1")


def _poll_interval(requested: float) -> float:
    """At most half the systemd watchdog period, so a slow poll cannot miss a pet."""
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec:
        return requested
    try:
        return min(requested, int(usec) / 1_000_000 / 2)
    except ValueError:
        return requested


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dps150-watchdog", description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_S, help="seconds between polls")
    parser.add_argument("--lock-timeout", type=float, default=DEFAULT_LOCK_TIMEOUT_S)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda signum, frame: stop.set())
    poll = _poll_interval(args.poll)
    log.info("watching %s every %g s", args.run_dir, poll)
    Watchdog(args.run_dir, lock_timeout=args.lock_timeout).run(stop, poll)
    return 0


if __name__ == "__main__":
    sys.exit(main())
