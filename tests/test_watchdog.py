import fcntl
import logging
import os
import socket
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from dps150.driver import USB_PID, USB_VID, force_output_off
from dps150.lease import lease_path, read_lease, write_lease
from dps150.protocol import CMD_SESSION, encode
from dps150.testing import FakeClock, MockSerial
from dps150.watchdog import Watchdog, attached_dps150_ports, sd_notify

SERIAL = "ABC123"
T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
SESSION_STOP = encode(CMD_SESSION, 0x00, b"\x00")


def dps150_usb_id(path):
    return (USB_VID, USB_PID)


def make_supply_on():
    clock = FakeClock()
    port = MockSerial(clock=clock)
    port.registers[0xDB] = b"\x01"
    return port, clock


class Bench:
    """A run dir, a lockable port path, a mock supply, and a watchdog wired to them."""

    def __init__(self, tmp_path, *, usb_id=dps150_usb_id, serial_factory=None, lock_timeout=0.2):
        self.run_dir = tmp_path / "run"
        self.port_path = tmp_path / "ttyACM0"
        self.port_path.touch()
        self.port, self.clock = make_supply_on()
        self.off_calls = 0
        self.now = T0
        self.ports = {SERIAL: str(self.port_path)}
        """What USB reports: serial number to device path. A serial not listed is unplugged."""
        self.attached: list[str] = []
        """DPS-150 device paths the startup sweep sees."""

        def force_off(port):
            self.off_calls += 1
            force_output_off(
                port,
                serial_factory=serial_factory or (lambda path: self.port),
                usb_id=usb_id,
                clock=self.clock,
            )

        self.watchdog = Watchdog(
            self.run_dir,
            force_output_off=force_off,
            find_port=self.ports.get,
            attached=lambda: list(self.attached),
            usb_serial=lambda port: next((s for s, p in self.ports.items() if p == port), None),
            now=lambda: self.now,
            lock_timeout=lock_timeout,
            notify=lambda state: None,
            sleep=self.clock.sleep,
        )

    def lease(self, seconds, serial=SERIAL, port=None):
        return write_lease(serial, port or self.port_path, seconds, self.run_dir, now=T0)

    def output_is_on(self):
        return self.port.registers[0xDB] == b"\x01"

    def lease_exists(self, serial=SERIAL):
        return read_lease(serial, self.run_dir) is not None


def test_expired_lease_turns_the_output_off_and_removes_the_lease(tmp_path, caplog):
    bench = Bench(tmp_path)
    bench.lease(60)
    bench.now = T0 + timedelta(seconds=60)
    with caplog.at_level(logging.WARNING, logger="dps150.watchdog"):
        bench.watchdog.tick()
    assert not bench.output_is_on()
    assert bench.port.written[-1] == SESSION_STOP
    assert not bench.lease_exists()
    assert "expired" in caplog.text and SERIAL in caplog.text


def test_live_lease_leaves_the_supply_untouched(tmp_path):
    bench = Bench(tmp_path)
    bench.lease(60)
    bench.now = T0 + timedelta(seconds=59)
    bench.watchdog.tick()
    assert bench.output_is_on()
    assert bench.port.written == []
    assert bench.lease_exists()


def test_expiry_does_not_set_the_inhibit_latch(tmp_path):
    bench = Bench(tmp_path)
    bench.lease(60)
    bench.now = T0 + timedelta(hours=1)
    bench.watchdog.tick()
    assert not bench.output_is_on()
    assert not any(p.suffix == ".inhibit" for p in tmp_path.rglob("*"))


def test_malformed_lease_turns_the_output_off(tmp_path):
    bench = Bench(tmp_path)
    bench.run_dir.mkdir()
    lease_path(SERIAL, bench.run_dir).write_text(f'{{"port": "{bench.port_path}"}}')
    bench.watchdog.tick()
    assert not bench.output_is_on()
    assert not bench.lease_exists()


def test_lease_without_a_port_is_resolved_by_serial(tmp_path):
    bench = Bench(tmp_path)
    bench.run_dir.mkdir()
    lease_path(SERIAL, bench.run_dir).write_text("garbage")
    bench.watchdog.tick()
    assert not bench.output_is_on()


def test_unplugged_supply_is_logged_once_and_its_lease_kept(tmp_path, caplog):
    bench = Bench(tmp_path)
    bench.lease(60)
    bench.now = T0 + timedelta(seconds=61)
    del bench.ports[SERIAL]
    with caplog.at_level(logging.DEBUG, logger="dps150.watchdog"):
        bench.watchdog.tick()
        bench.watchdog.tick()
    assert bench.output_is_on()
    assert bench.port.written == []
    assert bench.lease_exists()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "not on USB" in errors[0].getMessage()


def test_recorded_port_is_never_used_to_reach_another_supply(tmp_path):
    """Supply A (expired lease, recorded on ttyACM0) is unplugged; supply B, with a live
    lease, now sits on ttyACM0. The watchdog must not write to B through A's stale path."""
    bench = Bench(tmp_path)
    bench.lease(60, serial="A")
    bench.lease(3600, serial="B")
    bench.ports = {"B": str(bench.port_path)}
    bench.watchdog._find_port = bench.ports.get
    bench.now = T0 + timedelta(seconds=61)
    bench.watchdog.tick()
    assert bench.output_is_on()
    assert bench.port.written == []
    assert bench.lease_exists("A") and bench.lease_exists("B")


@pytest.mark.parametrize(
    "fault",
    [
        {"usb_id": lambda path: None},
        {"serial_factory": lambda path: (_ for _ in ()).throw(OSError("No such device"))},
    ],
    ids=["usb_id_none", "serial_factory_raises"],
)
def test_unplugged_supply_is_logged_and_the_loop_keeps_going(tmp_path, caplog, fault):
    bench = Bench(tmp_path, **fault)
    bench.lease(60)
    bench.lease(60, serial="OTHER")
    bench.ports["OTHER"] = str(bench.port_path)
    bench.now = T0 + timedelta(seconds=61)
    with caplog.at_level(logging.DEBUG, logger="dps150.watchdog"):
        bench.watchdog.tick()
        bench.watchdog.tick()
    assert bench.off_calls == 4
    assert bench.lease_exists() and bench.lease_exists("OTHER")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert {r.getMessage().split(":")[0] for r in errors} == {
        f"could not enforce lease for supply {SERIAL}",
        "could not enforce lease for supply OTHER",
    }
    assert len(errors) == 2, "a repeated identical failure is logged once at ERROR"


def test_wrong_device_on_the_recorded_port_is_refused_without_writing(tmp_path):
    bench = Bench(tmp_path, usb_id=lambda path: (0x1234, 0x5678))
    bench.lease(60)
    bench.now = T0 + timedelta(seconds=61)
    bench.watchdog.tick()
    assert bench.port.written == []
    assert bench.output_is_on()


def test_recovery_after_a_failure_is_logged_once(tmp_path, caplog):
    bench = Bench(tmp_path, usb_id=lambda path: None)
    bench.lease(60)
    bench.now = T0 + timedelta(seconds=61)
    bench.watchdog.tick()
    bench.watchdog._force_output_off = lambda port: force_output_off(
        port, serial_factory=lambda p: bench.port, usb_id=dps150_usb_id, clock=bench.clock
    )
    with caplog.at_level(logging.WARNING, logger="dps150.watchdog"):
        bench.watchdog.tick()
        bench.watchdog.tick()
    assert not bench.output_is_on()
    assert caplog.text.count("earlier failure cleared") == 1


def test_busy_port_lock_defers_to_the_next_poll(tmp_path, caplog):
    bench = Bench(tmp_path, lock_timeout=0.1)
    bench.lease(60)
    bench.now = T0 + timedelta(seconds=61)
    holder = os.open(bench.port_path, os.O_RDONLY)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with caplog.at_level(logging.ERROR, logger="dps150.watchdog"):
            bench.watchdog.tick()
        assert bench.output_is_on()
        assert bench.port.written == []
        assert bench.lease_exists()
        assert "locked by another dps150 process" in caplog.text
    finally:
        os.close(holder)
    bench.watchdog.tick()
    assert not bench.output_is_on()
    assert not bench.lease_exists()


def test_lease_renewed_while_waiting_for_the_lock_is_honoured(tmp_path):
    bench = Bench(tmp_path, lock_timeout=5.0)
    bench.lease(60)
    bench.now = T0 + timedelta(seconds=61)
    holder = os.open(bench.port_path, os.O_RDONLY)
    fcntl.flock(holder, fcntl.LOCK_EX)

    def renew_then_release():
        time.sleep(0.2)
        write_lease(SERIAL, bench.port_path, 600, bench.run_dir, now=bench.now)
        os.close(holder)

    thread = threading.Thread(target=renew_then_release)
    thread.start()
    bench.watchdog.tick()
    thread.join()
    assert bench.output_is_on()
    assert bench.port.written == []
    assert read_lease(SERIAL, bench.run_dir).deadline == bench.now + timedelta(seconds=600)


def test_one_failing_supply_does_not_block_another(tmp_path):
    bench = Bench(tmp_path)
    bench.run_dir.mkdir()
    lease_path("GHOST", bench.run_dir).write_text('{"port": "/nonexistent/tty", "deadline": "x"}')
    bench.lease(60)
    bench.now = T0 + timedelta(seconds=61)
    bench.watchdog.tick()
    assert not bench.output_is_on()
    assert bench.lease_exists("GHOST")


def test_run_notifies_ready_pets_each_poll_and_announces_stopping(tmp_path):
    notified = []
    stop = threading.Event()
    ticks = []
    bench = Bench(tmp_path)
    bench.attached.append(str(bench.port_path))
    bench.watchdog._notify = notified.append
    bench.watchdog.tick = lambda: ticks.append(1)

    def sleep(seconds):
        if len(ticks) == 3:
            stop.set()

    bench.watchdog._sleep = sleep
    bench.watchdog.run(stop, poll_s=1.0)
    sweep_pet, poll_pets = "WATCHDOG=1", ["WATCHDOG=1"] * 3
    assert notified == ["READY=1", sweep_pet, *poll_pets, "STOPPING=1"]
    assert not bench.output_is_on(), "the startup sweep ran before the first poll"


def test_run_announces_stopping_when_interrupted(tmp_path):
    notified = []
    bench = Bench(tmp_path)
    bench.watchdog._notify = notified.append

    def interrupt(seconds):
        raise KeyboardInterrupt

    bench.watchdog._sleep = interrupt
    with pytest.raises(KeyboardInterrupt):
        bench.watchdog.run(threading.Event())
    assert notified[-1] == "STOPPING=1"


def test_sd_notify_sends_the_datagram_to_notify_socket(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="sd") as d:
        path = os.path.join(d, "notify")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
            server.bind(path)
            server.settimeout(1)
            monkeypatch.setenv("NOTIFY_SOCKET", path)
            assert sd_notify("READY=1") is True
            assert sd_notify("WATCHDOG=1") is True
            assert server.recv(64) == b"READY=1"
            assert server.recv(64) == b"WATCHDOG=1"


def test_sd_notify_is_a_no_op_outside_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sd_notify("READY=1") is False


def test_force_output_off_is_the_default_off_path():
    from dps150 import driver

    assert Watchdog()._force_output_off is driver.force_output_off


def test_live_lease_does_not_touch_the_port_lock(tmp_path, caplog):
    bench = Bench(tmp_path)
    bench.lease(60)
    bench.now = T0 + timedelta(seconds=1)
    holder = os.open(bench.port_path, os.O_RDONLY)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with caplog.at_level(logging.DEBUG, logger="dps150.watchdog"):
            bench.watchdog.tick()
    finally:
        os.close(holder)
    assert caplog.records == []
    assert bench.output_is_on()


def test_tick_pets_systemd_after_each_lease(tmp_path):
    bench = Bench(tmp_path)
    pets = []
    bench.watchdog._notify = pets.append
    bench.lease(60)
    bench.lease(60, serial="OTHER")
    bench.ports["OTHER"] = str(bench.port_path)
    bench.now = T0 + timedelta(seconds=61)
    bench.watchdog.tick()
    assert pets == ["WATCHDOG=1", "WATCHDOG=1"]


def test_a_timed_out_write_on_one_supply_does_not_block_the_next(tmp_path, caplog):
    """Probe p2: a wedged CDC write. With open_serial's write timeout pyserial raises
    SerialTimeoutException; the lease stays for the next poll and the tick goes on."""
    import serial

    class WedgedSerial(MockSerial):
        def write(self, data):
            raise serial.SerialTimeoutException("Write timeout")

    wedged = WedgedSerial()
    bench = Bench(tmp_path, serial_factory=lambda path: wedged)
    pets = []
    bench.watchdog._notify = pets.append
    bench.lease(60, serial="WEDGED")
    bench.ports["WEDGED"] = str(bench.port_path)
    bench.lease(60)
    bench.ports[SERIAL] = str(tmp_path / "ttyACM1")
    (tmp_path / "ttyACM1").touch()
    healthy, clock = make_supply_on()
    bench.watchdog._force_output_off = lambda port: force_output_off(
        port,
        serial_factory=lambda path: wedged if path == str(bench.port_path) else healthy,
        usb_id=dps150_usb_id,
        clock=clock,
    )
    bench.now = T0 + timedelta(seconds=61)
    with caplog.at_level(logging.ERROR, logger="dps150.watchdog"):
        bench.watchdog.tick()
    assert healthy.registers[0xDB] == b"\x00"
    assert not bench.lease_exists()
    assert bench.lease_exists("WEDGED")
    assert "Write timeout" in caplog.text
    assert pets == ["WATCHDOG=1", "WATCHDOG=1"]


def test_lease_order_rotates_with_the_clock_so_a_hanging_lease_cannot_starve_the_rest(tmp_path):
    """systemd kills a wedged tick and restarts the service with no memory of which lease
    hung, so the rotation must come from something that survives a restart: the clock."""
    bench = Bench(tmp_path)
    for serial in ("A", "B", "C"):
        bench.lease(60, serial=serial)
    attempted = []
    bench.watchdog._find_port = lambda serial: attempted.append(serial)
    orders = set()
    for second in range(61, 67):
        attempted.clear()
        bench.now = T0 + timedelta(seconds=second)
        bench.watchdog.tick()
        orders.add(tuple(attempted))
    assert orders == {("A", "B", "C"), ("B", "C", "A"), ("C", "A", "B")}


def test_startup_sweep_turns_off_an_attached_supply_with_no_lease(tmp_path, caplog):
    """After a host reboot /run is empty, so a supply left on has no lease to expire."""
    bench = Bench(tmp_path)
    bench.attached.append(str(bench.port_path))
    with caplog.at_level(logging.WARNING, logger="dps150.watchdog"):
        bench.watchdog.sweep()
    assert not bench.output_is_on()
    assert bench.port.written[-1] == SESSION_STOP
    assert not bench.lease_exists()
    assert "no lease" in caplog.text and SERIAL in caplog.text


def test_startup_sweep_turns_off_an_expired_lease_and_removes_it(tmp_path):
    bench = Bench(tmp_path)
    bench.attached.append(str(bench.port_path))
    bench.lease(60)
    bench.now = T0 + timedelta(seconds=61)
    bench.watchdog.sweep()
    assert not bench.output_is_on()
    assert not bench.lease_exists()


def test_startup_sweep_leaves_a_live_lease_untouched(tmp_path):
    bench = Bench(tmp_path)
    bench.attached.append(str(bench.port_path))
    bench.lease(60)
    bench.now = T0 + timedelta(seconds=59)
    bench.watchdog.sweep()
    assert bench.output_is_on()
    assert bench.port.written == []
    assert bench.lease_exists()


def test_startup_sweep_skips_a_port_another_process_holds(tmp_path, caplog):
    bench = Bench(tmp_path, lock_timeout=0.1)
    bench.attached.append(str(bench.port_path))
    holder = os.open(bench.port_path, os.O_RDONLY)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with caplog.at_level(logging.ERROR, logger="dps150.watchdog"):
            bench.watchdog.sweep()
    finally:
        os.close(holder)
    assert bench.output_is_on()
    assert bench.port.written == []
    assert "locked by another dps150 process" in caplog.text


def test_startup_sweep_writes_nothing_to_a_device_that_is_not_a_dps150(tmp_path):
    bench = Bench(tmp_path, usb_id=lambda path: (0x1234, 0x5678))
    bench.attached.append(str(bench.port_path))
    bench.watchdog.sweep()
    assert bench.output_is_on()
    assert bench.port.written == []


def test_startup_sweep_pets_systemd_after_each_port_and_survives_a_failure(tmp_path):
    bench = Bench(tmp_path)
    bench.attached += [str(tmp_path / "missing"), str(bench.port_path)]
    pets = []
    bench.watchdog._notify = pets.append
    bench.watchdog.sweep()
    assert not bench.output_is_on()
    assert pets == ["WATCHDOG=1", "WATCHDOG=1"]


def test_attached_dps150_ports_filters_on_vid_pid(monkeypatch):
    from types import SimpleNamespace

    from serial.tools import list_ports

    infos = [
        SimpleNamespace(device="/dev/ttyACM0", vid=USB_VID, pid=USB_PID),
        SimpleNamespace(device="/dev/ttyACM1", vid=0x1234, pid=0x5678),
        SimpleNamespace(device="/dev/ttyS0", vid=None, pid=None),
        SimpleNamespace(device="/dev/ttyACM2", vid=USB_VID, pid=USB_PID),
    ]
    monkeypatch.setattr(list_ports, "comports", lambda: infos)
    assert attached_dps150_ports() == ["/dev/ttyACM0", "/dev/ttyACM2"]


def test_sweep_defaults_use_the_real_usb_lookups():
    from dps150 import driver

    watchdog = Watchdog()
    assert watchdog._attached is attached_dps150_ports
    assert watchdog._usb_serial is driver.usb_serial


def test_renew_written_outside_the_lock_during_enforcement_is_kept(tmp_path):
    """Probe p5. The lease API does not enforce the lock, so a holder may renew between
    the watchdog's re-read and its clear; the fresh lease must survive."""
    bench = Bench(tmp_path)
    bench.lease(60)
    bench.now = T0 + timedelta(seconds=61)
    original = bench.watchdog._force_output_off

    def off_then_holder_renews(port):
        original(port)
        write_lease(SERIAL, bench.port_path, 3600, bench.run_dir, now=bench.now)

    bench.watchdog._force_output_off = off_then_holder_renews
    bench.watchdog.tick()
    assert read_lease(SERIAL, bench.run_dir).deadline == bench.now + timedelta(seconds=3600)
