import fcntl
import logging
import multiprocessing
import os
import time

import pytest

from dps150 import state
from dps150.state import Inhibited, PortLockTimeout

SERIAL = "A1B2C3"


@pytest.fixture
def port(tmp_path):
    device = tmp_path / "ttyACM-fake"
    device.touch()
    return device


def _hold_lock(port, acquired, release, released=None):
    with state.port_lock(port, timeout=1):
        acquired.set()
        release.wait(10)
        if released is not None:
            released.value = 1


def _try_lock(port, timeout, result, released=None, trying=None):
    if trying is not None:
        trying.set()
    try:
        with state.port_lock(port, timeout=timeout):
            result.put(("acquired", released.value if released is not None else None))
    except PortLockTimeout as exc:
        result.put(("timeout", str(exc)))


@pytest.fixture
def ctx():
    return multiprocessing.get_context("spawn")


def test_second_process_times_out_while_first_holds_the_lock(ctx, port):
    acquired, release, result = ctx.Event(), ctx.Event(), ctx.Queue()
    holder = ctx.Process(target=_hold_lock, args=(port, acquired, release))
    holder.start()
    try:
        assert acquired.wait(10)
        contender = ctx.Process(target=_try_lock, args=(port, 0.3, result))
        contender.start()
        contender.join(10)
        outcome, detail = result.get(timeout=5)
        assert outcome == "timeout"
        assert str(port) in detail
    finally:
        release.set()
        holder.join(10)


def test_second_process_waits_and_gets_the_lock_once_the_first_releases(ctx, port):
    acquired, release, trying = ctx.Event(), ctx.Event(), ctx.Event()
    released, result = ctx.Value("i", 0), ctx.Queue()
    holder = ctx.Process(target=_hold_lock, args=(port, acquired, release, released))
    holder.start()
    assert acquired.wait(10)
    contender = ctx.Process(target=_try_lock, args=(port, 10, result, released, trying))
    contender.start()
    assert trying.wait(10)
    time.sleep(0.3)
    release.set()
    holder.join(10)
    contender.join(10)
    outcome, holder_had_released = result.get(timeout=5)
    assert outcome == "acquired"
    assert holder_had_released == 1


def test_lock_is_released_when_the_block_raises(port):
    with pytest.raises(RuntimeError), state.port_lock(port, timeout=1):
        raise RuntimeError("boom")
    with state.port_lock(port, timeout=0):
        pass


def test_no_latch_means_output_on_is_allowed(tmp_path):
    assert state.read_latch(SERIAL, state_dir=tmp_path) is None
    state.check_not_inhibited(SERIAL, state_dir=tmp_path)


def test_latch_records_who_when_reason_and_persists_on_disk(tmp_path):
    written = state.set_latch(SERIAL, who="thermal", reason="board at 85C", state_dir=tmp_path)
    assert (tmp_path / f"{SERIAL}.inhibit").exists()
    assert list(tmp_path.iterdir()) == [tmp_path / f"{SERIAL}.inhibit"]
    read_back = state.read_latch(SERIAL, state_dir=tmp_path)
    assert read_back == written
    assert read_back.who == "thermal"
    assert read_back.reason == "board at 85C"
    assert read_back.when.tzinfo is not None


def test_output_on_refused_under_latch(tmp_path):
    state.set_latch(SERIAL, who="ci", reason="overtemp", state_dir=tmp_path)
    with pytest.raises(Inhibited) as excinfo:
        state.check_not_inhibited(SERIAL, state_dir=tmp_path)
    assert excinfo.value.latch.reason == "overtemp"
    assert "clear-inhibit" in str(excinfo.value)


def test_latch_is_per_serial(tmp_path):
    state.set_latch(SERIAL, who="ci", reason="x", state_dir=tmp_path)
    state.check_not_inhibited("OTHER", state_dir=tmp_path)


def test_unreadable_latch_file_still_inhibits(tmp_path):
    (tmp_path / f"{SERIAL}.inhibit").write_text("{not json")
    with pytest.raises(Inhibited):
        state.check_not_inhibited(SERIAL, state_dir=tmp_path)


def test_set_latch_creates_missing_state_dir(tmp_path):
    state_dir = tmp_path / "var" / "lib" / "dps150"
    state.set_latch(SERIAL, who="ci", reason="x", state_dir=state_dir)
    assert state.read_latch(SERIAL, state_dir=state_dir) is not None


def test_clear_latch_removes_it_and_logs_who_cleared(tmp_path, caplog):
    state.set_latch(SERIAL, who="thermal", reason="overtemp", state_dir=tmp_path)
    with caplog.at_level(logging.WARNING, logger="dps150"):
        cleared = state.clear_latch(SERIAL, who="michael", state_dir=tmp_path)
    assert cleared.reason == "overtemp"
    state.check_not_inhibited(SERIAL, state_dir=tmp_path)
    assert "michael" in caplog.text
    assert SERIAL in caplog.text


def test_clear_latch_without_a_latch_is_a_noop(tmp_path):
    assert state.clear_latch(SERIAL, who="michael", state_dir=tmp_path) is None


@pytest.mark.parametrize("bad", ["", "..", "a/b", "../etc"])
def test_serial_that_is_not_a_plain_filename_is_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        state.set_latch(bad, who="ci", reason="x", state_dir=tmp_path)


def test_kill_turns_output_off_under_the_lock_and_latches(tmp_path, port):
    lock_held_during_off = []

    def output_off():
        fd = os.open(port, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_held_during_off.append(False)
        except BlockingIOError:
            lock_held_during_off.append(True)
        finally:
            os.close(fd)

    latch = state.kill(
        port, SERIAL, output_off, who="thermal", reason="overtemp", state_dir=tmp_path
    )
    assert lock_held_during_off == [True]
    assert latch.reason == "overtemp"
    with pytest.raises(Inhibited):
        state.check_not_inhibited(SERIAL, state_dir=tmp_path)


def test_kill_latches_even_when_output_off_fails(tmp_path, port):
    def output_off():
        raise OSError("device gone")

    with pytest.raises(OSError):
        state.kill(port, SERIAL, output_off, who="ci", reason="x", state_dir=tmp_path)
    with pytest.raises(Inhibited):
        state.check_not_inhibited(SERIAL, state_dir=tmp_path)


def test_kill_latches_even_when_the_lock_is_busy(ctx, tmp_path, port):
    acquired, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold_lock, args=(port, acquired, release))
    holder.start()
    try:
        assert acquired.wait(10)
        with pytest.raises(PortLockTimeout):
            state.kill(
                port,
                SERIAL,
                lambda: None,
                who="ci",
                reason="x",
                state_dir=tmp_path,
                lock_timeout=0.2,
            )
        with pytest.raises(Inhibited):
            state.check_not_inhibited(SERIAL, state_dir=tmp_path)
    finally:
        release.set()
        holder.join(10)


def test_kill_turns_output_off_when_the_state_dir_is_not_writable(tmp_path, port):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    os.chmod(state_dir, 0o500)
    calls = []
    try:
        with pytest.raises(PermissionError):
            state.kill(port, SERIAL, lambda: calls.append("off"), who="ci", state_dir=state_dir)
    finally:
        os.chmod(state_dir, 0o700)
    assert calls == ["off"]


def test_kill_turns_output_off_when_the_state_dir_is_a_file(tmp_path, port):
    state_dir = tmp_path / "state"
    state_dir.write_text("x")
    calls = []
    with pytest.raises(OSError):
        state.kill(port, SERIAL, lambda: calls.append("off"), who="ci", state_dir=state_dir)
    assert calls == ["off"]


def test_kill_turns_output_off_when_the_serial_is_invalid(tmp_path, port):
    calls = []
    with pytest.raises(ValueError):
        state.kill(port, "A/B", lambda: calls.append("off"), who="ci", state_dir=tmp_path)
    assert calls == ["off"]


def test_kill_reports_both_errors_when_latch_and_output_off_fail(tmp_path, port):
    state_dir = tmp_path / "state"
    state_dir.write_text("x")

    def output_off():
        raise TimeoutError("device gone")

    with pytest.raises(OSError) as excinfo:
        state.kill(port, SERIAL, output_off, who="ci", state_dir=state_dir)
    assert not isinstance(excinfo.value, TimeoutError)
    assert any("device gone" in note for note in excinfo.value.__notes__)


def test_kill_without_who_works_when_the_user_cannot_be_looked_up(tmp_path, port, monkeypatch):
    def no_user():
        raise KeyError("getpwuid(): uid not found")

    monkeypatch.setattr(state.getpass, "getuser", no_user)
    calls = []
    latch = state.kill(port, SERIAL, lambda: calls.append("off"), state_dir=tmp_path)
    assert calls == ["off"]
    assert latch.who
    with pytest.raises(Inhibited):
        state.check_not_inhibited(SERIAL, state_dir=tmp_path)


def test_dangling_symlink_latch_still_inhibits(tmp_path):
    (tmp_path / f"{SERIAL}.inhibit").symlink_to(tmp_path / "gone")
    with pytest.raises(Inhibited):
        state.check_not_inhibited(SERIAL, state_dir=tmp_path)


def test_latch_path_that_is_a_directory_still_inhibits(tmp_path):
    (tmp_path / f"{SERIAL}.inhibit").mkdir()
    with pytest.raises(Inhibited):
        state.check_not_inhibited(SERIAL, state_dir=tmp_path)
