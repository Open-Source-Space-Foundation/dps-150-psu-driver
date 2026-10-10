import json
import multiprocessing
import os
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FACTORY_REGISTERS

from dps150 import cli, state
from dps150.driver import USB_PID, USB_VID
from dps150.lease import write_lease
from dps150.protocol import CMD_GET, CMD_SESSION, CMD_SET, encode, pack_f32, unpack_f32
from dps150.testing import FakeClock, MockSerial

SERIAL = "ABC123"
SESSION_START = encode(CMD_SESSION, 0x00, b"\x01")
OUTPUT_ON = encode(CMD_SET, 0xDB, b"\x01")
OUTPUT_OFF = encode(CMD_SET, 0xDB, b"\x00")
ENV = {"PSU_MAX_V": "10", "PSU_MAX_I": "1.5", "PSU_VOLTAGE": "8.2", "PSU_CURRENT": "1.2"}


class Bench:
    def __init__(self, tmp_path):
        self.port = tmp_path / "ttyACM-fake"
        self.port.touch()
        self.state_dir = tmp_path / "state"
        self.run_dir = tmp_path / "run"
        self.run_dir.mkdir()
        self.clock = FakeClock()
        self.serial = MockSerial(clock=self.clock)
        self.serial.registers.update(FACTORY_REGISTERS)
        self.ids = (USB_VID, USB_PID)
        self.usb_serial = SERIAL

    @property
    def lease_path(self):
        return self.run_dir / f"{SERIAL}.lease"

    @property
    def latch_path(self):
        return self.state_dir / f"{SERIAL}.inhibit"

    def run(self, *argv, env=ENV):
        args = ["-d", str(self.port), "--state-dir", str(self.state_dir)]
        args += ["--run-dir", str(self.run_dir), *argv]
        return cli.main(
            args,
            env=env,
            serial_factory=lambda path: self.serial,
            usb_id=lambda path: self.ids,
            usb_serial=lambda path: self.usb_serial,
            clock=self.clock,
        )

    def sets(self):
        return [(f[2], f[4:-1]) for f in self.serial.written if f[1] == CMD_SET]

    def set_registers(self):
        return [register for register, _ in self.sets()]

    def db_writes(self):
        return [payload[0] for register, payload in self.sets() if register == 0xDB]


@pytest.fixture
def bench(tmp_path):
    return Bench(tmp_path)


def _hold_lock(port, acquired, release):
    with state.port_lock(port, timeout=1):
        acquired.set()
        release.wait(10)


@pytest.fixture
def lock_held(bench):
    ctx = multiprocessing.get_context("spawn")
    acquired, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold_lock, args=(bench.port, acquired, release))
    holder.start()
    assert acquired.wait(10)
    yield
    release.set()
    holder.join(10)


# Korad-compatible flags


def test_output_1_programs_limit_then_setpoints_then_turns_on(bench, capsys):
    """Catches a reordered sequence: output on before protection or setpoints are in place."""
    before = datetime.now(UTC)
    assert bench.run("--output", "1", "--lease", "1200") == cli.EXIT_OK
    assert bench.serial.written[0] == SESSION_START
    assert bench.set_registers() == [0xD1, 0xD2, 0xC1, 0xC2, 0xDB]
    sets = dict(bench.sets())
    assert unpack_f32(sets[0xD1]) == pytest.approx(10.0)
    assert unpack_f32(sets[0xD2]) == pytest.approx(1.5)
    assert unpack_f32(sets[0xC1]) == pytest.approx(8.2)
    assert unpack_f32(sets[0xC2]) == pytest.approx(1.2)
    assert sets[0xDB] == b"\x01"
    record = json.loads(bench.lease_path.read_text())
    assert record["port"] == str(bench.port)
    deadline = datetime.fromisoformat(record["deadline"])
    assert (
        before + timedelta(seconds=1200) <= deadline <= datetime.now(UTC) + timedelta(seconds=1200)
    )
    assert capsys.readouterr().out.startswith("output on: 8.2 V / 1.2 A, lease until ")


def test_lease_file_exists_at_the_moment_output_on_is_written(bench):
    """Catches writing the lease after DB=1: a crash between them leaves power on unsupervised."""
    seen = []
    write = bench.serial.write

    def watching_write(data):
        if bytes(data) == OUTPUT_ON:
            seen.append(bench.lease_path.exists())
        return write(data)

    bench.serial.write = watching_write
    assert bench.run("--output", "1", "--lease", "60") == cli.EXIT_OK
    assert seen == [True]


@pytest.mark.parametrize("word", ["1", "on", "ON"])
def test_output_on_accepts_1_and_on(bench, word):
    assert bench.run("--output", word, "--lease", "60") == cli.EXIT_OK
    assert bench.db_writes() == [1]


def test_lease_from_env(bench):
    assert bench.run("--output", "1", env=ENV | {"PSU_LEASE_S": "60"}) == cli.EXIT_OK
    assert bench.lease_path.exists()


def test_flags_override_env_setpoints(bench):
    """Catches -v/-i being ignored in favor of PSU_VOLTAGE/PSU_CURRENT."""
    assert bench.run("-v", "5", "-i", "0.5", "--output", "1", "--lease", "60") == cli.EXIT_OK
    sets = dict(bench.sets())
    assert unpack_f32(sets[0xC1]) == pytest.approx(5.0)
    assert unpack_f32(sets[0xC2]) == pytest.approx(0.5)


@pytest.mark.parametrize("word", ["0", "off"])
def test_output_0_turns_off_without_programming_protection_or_a_limit(bench, word):
    """Catches an output-off that opens a full session: it writes D1/D2 from a limit it lacks."""
    write_lease(SERIAL, bench.port, 60, bench.run_dir)
    bench.serial.registers[0xDB] = b"\x01"
    assert bench.run("--output", word, env={}) == cli.EXIT_OK
    assert bench.set_registers() == [0xDB]
    assert bench.db_writes() == [0]
    assert (
        encode(CMD_GET, 0xDB, b"\x00")
        in bench.serial.written[bench.serial.written.index(OUTPUT_OFF) :]
    )
    assert bench.serial.registers[0xDB] == b"\x00"
    assert not bench.lease_path.exists()


def test_output_0_works_when_the_run_dir_does_not_exist(bench):
    """Catches output-off failing closed on the lease dir: it must always be able to cut power."""
    bench.run_dir.rmdir()
    assert bench.run("--output", "0", env={}) == cli.EXIT_OK
    assert bench.db_writes() == [0]


def test_output_0_without_a_usb_serial_turns_off_then_exits_5(bench):
    """Catches output-off refusing to cut power because the supply has no serial number."""
    bench.usb_serial = None
    assert bench.run("--output", "0", env={}) == cli.EXIT_WRONG_DEVICE
    assert bench.db_writes() == [0]


# Limit


def test_max_voltage_flag_lowers_the_limit_programmed_as_ovp(bench):
    assert bench.run("--max-voltage", "9", "--output", "1", "--lease", "60") == cli.EXIT_OK
    assert unpack_f32(dict(bench.sets())[0xD1]) == pytest.approx(9.0)
    assert unpack_f32(bench.serial.registers[0xD1]) == pytest.approx(9.0)


@pytest.mark.parametrize("flag, value", [("--max-voltage", "10.5"), ("--max-current", "1.6")])
def test_limit_flag_cannot_raise_the_env_limit(bench, capsys, flag, value):
    """Catches a flag that raises the limit above PSU_MAX_V/PSU_MAX_I."""
    assert bench.run(flag, value, "--output", "1", "--lease", "60") == cli.EXIT_USAGE
    assert bench.serial.written == []
    assert "never raise" in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["PSU_MAX_V", "PSU_MAX_I"])
def test_missing_env_limit_is_refused_before_the_port(bench, capsys, missing):
    """Catches running with half a limit, or none (the driver would fall back to nothing)."""
    env = {k: v for k, v in ENV.items() if k != missing}
    assert bench.run("--output", "1", "--lease", "60", env=env) == cli.EXIT_USAGE
    assert bench.serial.written == []
    err = capsys.readouterr().err
    assert "PSU_MAX_V" in err and "PSU_MAX_I" in err


def test_limit_flag_without_env_limit_is_refused(bench):
    """Catches a flag standing in for the env limit: env is the authority."""
    env = {"PSU_VOLTAGE": "1", "PSU_CURRENT": "0.1"}
    assert (
        bench.run(
            "--max-voltage", "5", "--max-current", "1", "--output", "1", "--lease", "60", env=env
        )
        == cli.EXIT_USAGE
    )
    assert bench.serial.written == []


@pytest.mark.parametrize("value", ["abc", "nan", "inf", "-1", "0"])
def test_unparseable_env_limit_is_refused(bench, value):
    assert (
        bench.run("--output", "1", "--lease", "60", env=ENV | {"PSU_MAX_V": value})
        == cli.EXIT_USAGE
    )
    assert bench.serial.written == []


def test_env_limit_above_the_ceiling_is_refused(bench):
    """Catches a limit above the 18 V ceiling reaching the supply."""
    assert (
        bench.run("--output", "1", "--lease", "60", env=ENV | {"PSU_MAX_V": "18.5"})
        == cli.EXIT_USAGE
    )
    assert bench.serial.written == []


# Output-on refusals


def test_output_1_without_a_lease_is_refused(bench):
    """Catches output-on with no lease: nothing would turn it off if the job dies (ADR L4)."""
    assert bench.run("--output", "1") == cli.EXIT_USAGE
    assert SESSION_START not in bench.serial.written
    assert list(bench.run_dir.iterdir()) == []


@pytest.mark.parametrize("seconds", ["0", "-5", str(6 * 3600 + 1), "nan"])
def test_lease_out_of_range_is_refused_before_the_port(bench, seconds):
    assert bench.run("--output", "1", "--lease", seconds) == cli.EXIT_USAGE
    assert bench.serial.written == []


@pytest.mark.parametrize("missing", ["PSU_VOLTAGE", "PSU_CURRENT"])
def test_missing_setpoint_is_refused(bench, missing):
    env = {k: v for k, v in ENV.items() if k != missing}
    assert bench.run("--output", "1", "--lease", "60", env=env) == cli.EXIT_USAGE
    assert bench.serial.written == []


def test_latch_refuses_output_on_before_the_port_is_touched(bench, capsys):
    """Catches output-on that ignores the inhibit latch a kill left behind."""
    state.set_latch(SERIAL, who="thermal", reason="hot", state_dir=bench.state_dir)
    assert bench.run("--output", "1", "--lease", "60") == cli.EXIT_INHIBITED
    assert bench.serial.written == []
    assert "clear-inhibit" in capsys.readouterr().err
    assert not bench.lease_path.exists()


def test_latch_set_during_setup_refuses_output_on(bench):
    """Catches a single latch check before setup: a kill that latches mid-setup is ignored."""
    write = bench.serial.write

    def kill_during_setup(data):
        if bytes(data)[1:3] == bytes([CMD_SET, 0xC2]):
            state.set_latch(SERIAL, who="thermal", reason="hot", state_dir=bench.state_dir)
        return write(data)

    bench.serial.write = kill_during_setup
    assert bench.run("--output", "1", "--lease", "60") == cli.EXIT_INHIBITED
    assert 1 not in bench.db_writes()
    assert not bench.lease_path.exists()


def test_missing_run_dir_refuses_output_on(bench, capsys):
    """Catches output-on without the watchdog, or a run that creates /run/dps150 itself."""
    bench.run_dir.rmdir()
    assert bench.run("--output", "1", "--lease", "60") == cli.EXIT_LEASE_DIR
    assert bench.serial.written == []
    assert not bench.run_dir.exists()
    assert "dps150-watchdog not installed or not running" in capsys.readouterr().err


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_run_dir_refuses_output_on(bench):
    """Catches output-on that finds the lease unwritable only after touching the device."""
    os.chmod(bench.run_dir, 0o500)
    try:
        assert bench.run("--output", "1", "--lease", "60") == cli.EXIT_LEASE_DIR
    finally:
        os.chmod(bench.run_dir, 0o700)
    assert bench.serial.written == []


def test_unconfirmed_output_on_writes_off_and_keeps_the_lease(bench):
    """Catches clearing the lease after a failed output-on: an unconfirmed off stays supervised."""
    bench.serial.drop_sets = {0xDB}
    assert bench.run("--output", "1", "--lease", "60") == cli.EXIT_DEVICE
    assert bench.db_writes()[-1] == 0
    assert bench.lease_path.exists()


def test_setpoint_above_the_limit_never_turns_on(bench):
    """Catches a setpoint above the limit reaching the supply's output."""
    assert bench.run("-v", "10.5", "--output", "1", "--lease", "60") == cli.EXIT_DEVICE
    assert 1 not in bench.db_writes()
    assert (0xC1, pack_f32(10.5)) not in bench.sets()


def test_wrong_device_is_refused_before_anything_is_written(bench):
    """Catches talking DPS-150 protocol to some other USB serial device."""
    bench.ids = (0x2341, 0x0043)
    assert bench.run("--output", "1", "--lease", "60") == cli.EXIT_WRONG_DEVICE
    assert bench.serial.written == []


def test_output_on_without_a_usb_serial_is_refused(bench):
    """Catches output-on with no key for the latch or the lease."""
    bench.usb_serial = None
    assert bench.run("--output", "1", "--lease", "60") == cli.EXIT_WRONG_DEVICE
    assert bench.serial.written == []


def test_busy_port_lock_exits_4(bench, lock_held, monkeypatch):
    monkeypatch.setattr(cli, "LOCK_TIMEOUT_S", 0.2)
    assert bench.run("--output", "1", "--lease", "60") == cli.EXIT_LOCK_BUSY
    assert bench.serial.written == []


def test_output_and_a_subcommand_together_is_a_usage_error(bench):
    assert bench.run("--output", "1", "kill") == cli.EXIT_USAGE
    assert bench.serial.written == []


# Kill


def test_kill_latches_turns_off_and_clears_the_lease(bench):
    write_lease(SERIAL, bench.port, 60, bench.run_dir)
    bench.serial.registers[0xDB] = b"\x01"
    assert bench.run("kill", "--reason", "board hot", env={}) == cli.EXIT_OK
    assert state.read_latch(SERIAL, bench.state_dir).reason == "board hot"
    assert bench.db_writes() == [0]
    assert bench.serial.registers[0xDB] == b"\x00"
    assert not bench.lease_path.exists()


def test_kill_with_the_port_locked_latches_and_says_output_may_be_on(
    bench, lock_held, monkeypatch, capsys
):
    """Catches a kill that times out quietly, or without leaving the latch behind."""
    monkeypatch.setattr(cli, "KILL_LOCK_TIMEOUT_S", 0.2)
    write_lease(SERIAL, bench.port, 60, bench.run_dir)
    assert bench.run("kill") == cli.EXIT_LOCK_BUSY
    assert bench.latch_path.exists()
    assert bench.lease_path.exists()
    err = capsys.readouterr().err
    assert f"INHIBIT LATCH IS SET for supply {SERIAL}" in err
    assert "OUTPUT MAY STILL BE ON" in err


def test_kill_with_unconfirmed_off_keeps_latch_and_lease(bench, capsys):
    """Catches clearing the lease before the off is confirmed."""
    write_lease(SERIAL, bench.port, 60, bench.run_dir)
    bench.serial.registers[0xDB] = b"\x01"
    bench.serial.drop_sets = {0xDB}
    assert bench.run("kill") == cli.EXIT_DEVICE
    assert bench.latch_path.exists()
    assert bench.lease_path.exists()
    assert "OUTPUT MAY STILL BE ON" in capsys.readouterr().err


def test_kill_without_a_usb_serial_still_turns_off(bench, capsys):
    bench.usb_serial = None
    assert bench.run("kill") == cli.EXIT_WRONG_DEVICE
    assert bench.db_writes() == [0]
    assert "INHIBIT LATCH IS NOT SET" in capsys.readouterr().err


def test_kill_without_a_usb_serial_reports_an_unconfirmed_off(bench, capsys):
    """Catches the off failure hiding behind the latch failure when both fail."""
    bench.usb_serial = None
    bench.serial.registers[0xDB] = b"\x01"
    bench.serial.drop_sets = {0xDB}
    assert bench.run("kill") == cli.EXIT_WRONG_DEVICE
    err = capsys.readouterr().err
    assert "INHIBIT LATCH IS NOT SET" in err
    assert "OUTPUT MAY STILL BE ON" in err
    assert "ReadbackMismatch" in err


# Clear-inhibit


def test_clear_inhibit_removes_the_latch_and_names_who_set_it(bench, capsys):
    state.set_latch(SERIAL, who="thermal", reason="hot", state_dir=bench.state_dir)
    assert bench.run("clear-inhibit", env={}) == cli.EXIT_OK
    assert not bench.latch_path.exists()
    assert "set by thermal" in capsys.readouterr().out
    assert bench.serial.written == []


def test_clear_inhibit_without_a_latch_says_so(bench, capsys):
    assert bench.run("clear-inhibit", env={}) == cli.EXIT_OK
    assert "no inhibit latch" in capsys.readouterr().out


def test_clear_inhibit_without_a_usb_serial_exits_5(bench):
    bench.usb_serial = None
    assert bench.run("clear-inhibit", env={}) == cli.EXIT_WRONG_DEVICE


# Status


def _status_lines(out):
    return dict(line.split(": ", 1) for line in out.splitlines())


@pytest.mark.parametrize("argv", [(), ("--status",)])
def test_status_reports_the_supply_and_the_local_state(bench, capsys, argv):
    bench.serial.telemetry = (8.2, 0.5, 4.1)
    assert bench.run(*argv) == cli.EXIT_OK
    lines = _status_lines(capsys.readouterr().out)
    assert lines == {
        "model": "DPS-150",
        "firmware": "V1.2",
        "voltage_setpoint": "0.000",
        "current_setpoint": "0.000",
        "output": "off",
        "protection": "LVP",
        "mode": "CV",
        "measured_voltage": "8.200",
        "measured_current": "0.500",
        "measured_power": "4.100",
        "ovp": "30.000",
        "ocp": "5.100",
        "max_voltage": "11.800",
        "inhibit": "clear",
        "lease": "none",
    }
    assert bench.sets() == []


def test_status_shows_latch_and_lease(bench, capsys):
    latch = state.set_latch(SERIAL, who="thermal", reason="hot", state_dir=bench.state_dir)
    lease = write_lease(SERIAL, bench.port, 60, bench.run_dir)
    assert bench.run() == cli.EXIT_OK
    lines = _status_lines(capsys.readouterr().out)
    assert lines["inhibit"] == f"latched by thermal at {latch.when.isoformat()}: hot"
    assert lines["lease"] == f"until {lease.deadline.isoformat()}"


def test_status_marks_an_expired_lease(bench, capsys):
    past = datetime.now(UTC) - timedelta(hours=1)
    lease = write_lease(SERIAL, bench.port, 60, bench.run_dir, now=past)
    assert bench.run() == cli.EXIT_OK
    assert _status_lines(capsys.readouterr().out)["lease"] == (
        f"until {lease.deadline.isoformat()} (expired)"
    )


def test_status_works_without_a_run_dir(bench, capsys):
    bench.run_dir.rmdir()
    assert bench.run() == cli.EXIT_OK
    assert _status_lines(capsys.readouterr().out)["lease"] == "none"


def test_status_needs_no_limit_in_the_environment(bench):
    assert bench.run(env={}) == cli.EXIT_OK


def test_status_after_a_ci_output_on_leaves_the_supply_protection_alone(bench):
    """Catches status reprogramming OVP/OCP from the caller's env on a live supply."""
    assert bench.run("--output", "1", "--lease", "600") == cli.EXIT_OK
    assert bench.run("--status", env={"PSU_MAX_V": "18", "PSU_MAX_I": "3"}) == cli.EXIT_OK
    assert unpack_f32(bench.serial.registers[0xD1]) == pytest.approx(10.0)
    assert unpack_f32(bench.serial.registers[0xD2]) == pytest.approx(1.5)
    assert bench.serial.registers[0xDB] == b"\x01"


def test_status_with_a_limit_flag_writes_nothing(bench):
    """Catches status lowering OVP below the running setpoint so the supply trips."""
    assert bench.run("--output", "1", "--lease", "600") == cli.EXIT_OK
    before = len(bench.serial.written)
    assert bench.run("--status", "--max-voltage", "5") == cli.EXIT_OK
    assert [f for f in bench.serial.written[before:] if f[1] == CMD_SET] == []
    assert unpack_f32(bench.serial.registers[0xD1]) == pytest.approx(10.0)


def test_status_get_failure_leaves_the_output_on_and_exits_1(bench):
    """Catches the session-open recovery turning a live output off because a GET failed."""
    assert bench.run("--output", "1", "--lease", "600") == cli.EXIT_OK
    bench.serial.silent.add(0xDE)
    assert bench.run("--status") == cli.EXIT_DEVICE
    assert bench.serial.registers[0xDB] == b"\x01"
    assert bench.db_writes() == [1]


def test_status_sends_no_set_frame(bench):
    assert bench.run() == cli.EXIT_OK
    assert [f for f in bench.serial.written if f[1] == CMD_SET] == []


def test_missing_port_is_a_usage_error():
    assert cli.main([], env=ENV) == cli.EXIT_USAGE


# Option order and a missing port path


@pytest.mark.parametrize("command", ["kill", "clear-inhibit"])
def test_kill_and_clear_inhibit_accept_global_options_after_the_subcommand(bench, command):
    """Catches the panic command failing on `dps150 kill -d PORT`."""
    bench.serial.registers[0xDB] = b"\x01"
    args = [command, "-d", str(bench.port), "--state-dir", str(bench.state_dir)]
    args += ["--run-dir", str(bench.run_dir)]
    code = cli.main(
        args,
        env={},
        serial_factory=lambda path: bench.serial,
        usb_id=lambda path: bench.ids,
        usb_serial=lambda path: bench.usb_serial,
        clock=bench.clock,
    )
    assert code == cli.EXIT_OK
    if command == "kill":
        assert bench.latch_path.exists()
        assert bench.db_writes() == [0]


@pytest.mark.parametrize("argv", [(), ("--output", "1", "--lease", "60"), ("--output", "0")])
def test_missing_port_path_exits_5_before_the_wire(bench, capsys, argv):
    """Catches three different messages and codes for one cause: the supply is unplugged."""
    bench.port.unlink()
    assert bench.run(*argv) == cli.EXIT_WRONG_DEVICE
    assert bench.serial.written == []
    assert f"port {bench.port} does not exist (supply unplugged" in capsys.readouterr().err


def test_kill_with_a_missing_port_path_still_latches(bench, capsys):
    bench.port.unlink()
    assert bench.run("kill") == cli.EXIT_WRONG_DEVICE
    assert bench.latch_path.exists()
    assert bench.serial.written == []
    err = capsys.readouterr().err
    assert "INHIBIT LATCH IS SET" in err and "does not exist" in err


def test_env_limit_above_the_ceiling_is_refused_even_with_a_lowering_flag(bench, capsys):
    """Catches a lowering flag masking a misconfigured PSU_MAX_V."""
    env = ENV | {"PSU_MAX_V": "100"}
    code = bench.run("--max-voltage", "10", "--output", "1", "--lease", "60", env=env)
    assert code == cli.EXIT_USAGE
    assert bench.serial.written == []
    assert "ceiling" in capsys.readouterr().err


# Device protection trip (bench #13: DC stays latched after a trip until SET DB=1)


def _trip(bench, code):
    bench.serial.registers[0xDC] = bytes([code])


def _on(bench):
    return bench.run("--output", "1", "--lease", "60")


@pytest.mark.parametrize(
    "code, name", [(1, "OVP"), (2, "OCP"), (3, "OPP"), (4, "OTP"), (6, "REP"), (9, "UNKNOWN(9)")]
)
def test_tripped_protection_latches_and_refuses_output_on(bench, capsys, code, name):
    """Catches re-energizing a faulted board: DB=1 would clear the trip and turn the output on."""
    _trip(bench, code)
    assert _on(bench) == cli.EXIT_INHIBITED
    assert 1 not in bench.db_writes()
    assert bench.serial.registers[0xDB] == b"\x00"
    assert bench.serial.registers[0xDC] == bytes([code])
    latch = state.read_latch(SERIAL, bench.state_dir)
    assert latch.reason == f"device protection tripped: {name}"
    err = capsys.readouterr().err
    assert name in err and "clear-inhibit" in err


def test_lvp_does_not_block_output_on(bench):
    """Catches treating LVP as a trip: a healthy unit on a 5 V USB input reports it (#13)."""
    _trip(bench, 5)
    assert _on(bench) == cli.EXIT_OK
    assert bench.db_writes() == [1]
    assert not bench.latch_path.exists()


def test_clear_inhibit_acknowledges_the_trip_for_one_output_on(bench):
    """Catches a latch that can never be cleared: only DB=1 clears DC, so the acknowledged
    output-on must proceed with DC still tripped, and the next trip must latch again."""
    _trip(bench, 1)
    assert _on(bench) == cli.EXIT_INHIBITED
    assert bench.run("clear-inhibit", env={}) == cli.EXIT_OK
    assert _on(bench) == cli.EXIT_OK
    assert bench.serial.registers[0xDB] == b"\x01"
    assert bench.serial.registers[0xDC] == b"\x00"
    assert not list(bench.state_dir.iterdir())
    assert bench.run("--output", "0", env={}) == cli.EXIT_OK
    _trip(bench, 2)
    assert _on(bench) == cli.EXIT_INHIBITED
    assert bench.db_writes().count(1) == 1
    assert state.read_latch(SERIAL, bench.state_dir).reason == "device protection tripped: OCP"


def test_acknowledgement_covers_only_the_trip_that_was_cleared(bench):
    """Catches an OVP acknowledgement waving through a trip that appeared after the clear."""
    _trip(bench, 1)
    assert _on(bench) == cli.EXIT_INHIBITED
    assert bench.run("clear-inhibit", env={}) == cli.EXIT_OK
    _trip(bench, 4)
    assert _on(bench) == cli.EXIT_INHIBITED
    assert 1 not in bench.db_writes()
    assert state.read_latch(SERIAL, bench.state_dir).reason == "device protection tripped: OTP"


def test_clearing_a_kill_latch_does_not_acknowledge_a_trip(bench):
    """Catches any clear-inhibit counting as a trip acknowledgement."""
    state.set_latch(SERIAL, who="thermal", reason="hot", state_dir=bench.state_dir)
    assert bench.run("clear-inhibit", env={}) == cli.EXIT_OK
    _trip(bench, 1)
    assert _on(bench) == cli.EXIT_INHIBITED
    assert 1 not in bench.db_writes()


def test_unreadable_acknowledgement_is_no_acknowledgement(bench):
    _trip(bench, 1)
    assert _on(bench) == cli.EXIT_INHIBITED
    assert bench.run("clear-inhibit", env={}) == cli.EXIT_OK
    (bench.state_dir / f"{SERIAL}.ack").write_text("{not json")
    assert _on(bench) == cli.EXIT_INHIBITED
    assert 1 not in bench.db_writes()


def test_acknowledgement_is_consumed_by_a_confirmed_output_on_even_when_unused(bench):
    """Catches a stale acknowledgement permitting a trip weeks later without a human."""
    _trip(bench, 1)
    assert _on(bench) == cli.EXIT_INHIBITED
    assert bench.run("clear-inhibit", env={}) == cli.EXIT_OK
    _trip(bench, 0)
    assert _on(bench) == cli.EXIT_OK
    assert not (bench.state_dir / f"{SERIAL}.ack").exists()
    assert bench.run("--output", "0", env={}) == cli.EXIT_OK
    _trip(bench, 1)
    assert _on(bench) == cli.EXIT_INHIBITED


def test_acknowledgement_survives_a_failed_output_on(bench):
    """Catches consuming the acknowledgement before the output is confirmed on."""
    _trip(bench, 1)
    assert _on(bench) == cli.EXIT_INHIBITED
    assert bench.run("clear-inhibit", env={}) == cli.EXIT_OK
    bench.serial.drop_sets = {0xDB}
    assert _on(bench) == cli.EXIT_DEVICE
    bench.serial.drop_sets = set()
    assert _on(bench) == cli.EXIT_OK


def test_kill_discards_an_acknowledgement(bench):
    _trip(bench, 1)
    assert _on(bench) == cli.EXIT_INHIBITED
    assert bench.run("clear-inhibit", env={}) == cli.EXIT_OK
    assert bench.run("kill", env={}) == cli.EXIT_OK
    assert bench.run("clear-inhibit", env={}) == cli.EXIT_OK
    assert _on(bench) == cli.EXIT_INHIBITED


def test_status_shows_the_trip_and_the_acknowledgement(bench, capsys):
    _trip(bench, 1)
    assert bench.run() == cli.EXIT_OK
    assert _status_lines(capsys.readouterr().out)["protection"] == "OVP (tripped)"
    assert _on(bench) == cli.EXIT_INHIBITED
    assert bench.run("clear-inhibit", env={}) == cli.EXIT_OK
    assert "next output-on" in capsys.readouterr().out
    assert bench.run() == cli.EXIT_OK
    lines = _status_lines(capsys.readouterr().out)
    assert lines["protection"] == "OVP (tripped)"
    assert lines["inhibit"].startswith("clear (trip OVP acknowledged by ")
