"""Per-supply state that lives outside the device: the port lock and the inhibit latch.

All state is keyed by the supply's USB serial number. See CONTEXT.md and ADR 0001.
"""

from __future__ import annotations

import fcntl
import getpass
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_STATE_DIR = Path("/var/lib/dps150")
DEFAULT_LOCK_TIMEOUT_S = 10.0

log = logging.getLogger(__name__)


class PortLockTimeout(Exception):
    pass


@dataclass(frozen=True)
class Latch:
    who: str
    when: datetime
    reason: str


class Inhibited(Exception):
    def __init__(self, serial: str, latch: Latch):
        self.latch = latch
        super().__init__(
            f"supply {serial} is inhibited: latched by {latch.who} at "
            f"{latch.when.isoformat()} ({latch.reason}); "
            f"a human must run `dps150 clear-inhibit` before output-on"
        )


@contextmanager
def port_lock(
    port: str | os.PathLike[str], timeout: float = DEFAULT_LOCK_TIMEOUT_S
) -> Iterator[None]:
    """Hold an exclusive flock on the serial device for the duration of the block.

    The lock lives on its own file descriptor, so do not also open the port with
    pyserial's `exclusive=True`: that flocks a second descriptor and fails against this one.
    """
    fd = os.open(port, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PortLockTimeout(
                        f"{os.fspath(port)} is locked by another dps150 process; "
                        f"gave up after {timeout:g} s"
                    ) from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _latch_path(serial: str, state_dir: Path) -> Path:
    if not serial or serial in (".", "..") or "/" in serial or "\0" in serial:
        raise ValueError(f"invalid supply serial number: {serial!r}")
    return Path(state_dir) / f"{serial}.inhibit"


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_latch(serial: str, state_dir: Path = DEFAULT_STATE_DIR) -> Latch | None:
    path = _latch_path(serial, state_dir)
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(path.read_text())
        return Latch(data["who"], datetime.fromisoformat(data["when"]), data["reason"])
    except (OSError, ValueError, KeyError, TypeError):
        # Any directory entry is the latch; a corrupt or unreadable one must still inhibit.
        mtime = datetime.fromtimestamp(entry.st_mtime, UTC)
        return Latch("unknown", mtime, f"unreadable latch file {path}")


def check_not_inhibited(serial: str, state_dir: Path = DEFAULT_STATE_DIR) -> None:
    """Raise Inhibited if the latch is set. Call it under the port lock, right before output-on."""
    latch = read_latch(serial, state_dir)
    if latch is not None:
        raise Inhibited(serial, latch)


def set_latch(serial: str, who: str, reason: str, state_dir: Path = DEFAULT_STATE_DIR) -> Latch:
    """Write the latch atomically and durably, replacing any existing one."""
    path = _latch_path(serial, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    latch = Latch(who, datetime.now(UTC), reason)
    record = {"who": latch.who, "when": latch.when.isoformat(), "reason": latch.reason}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)
    log.warning("inhibit latch set for supply %s by %s: %s", serial, who, reason)
    return latch


def clear_latch(serial: str, who: str, state_dir: Path = DEFAULT_STATE_DIR) -> Latch | None:
    """Remove the latch and log who cleared it. Returns the cleared latch, or None if unset."""
    latch = read_latch(serial, state_dir)
    if latch is None:
        return None
    path = _latch_path(serial, state_dir)
    # A latch set between the read above and this unlink is removed unlogged; see #12.
    path.unlink(missing_ok=True)
    _fsync_dir(path.parent)
    log.warning(
        "inhibit latch cleared for supply %s by %s (was set by %s at %s: %s)",
        serial,
        who,
        latch.who,
        latch.when.isoformat(),
        latch.reason,
    )
    return latch


def kill(
    port: str | os.PathLike[str],
    serial: str,
    output_off: Callable[[], None],
    who: str | None = None,
    reason: str = "kill",
    state_dir: Path = DEFAULT_STATE_DIR,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_S,
) -> Latch:
    """Set the inhibit latch, then take the port lock and turn the output off.

    The latch goes first so it blocks every later output-on even if the lock
    times out or the device call fails. Each step runs even if the other fails.
    The first error is raised, with any second error attached as a note.
    """
    errors: list[BaseException] = []
    latch = None
    try:
        latch = set_latch(serial, who or _current_user(), reason, state_dir)
    except BaseException as exc:
        errors.append(exc)
    try:
        with port_lock(port, lock_timeout):
            output_off()
    except BaseException as exc:
        errors.append(exc)
    if errors:
        first, *rest = errors
        for later in rest:
            first.add_note(f"output-off also failed: {later!r}")
        raise first
    assert latch is not None
    return latch


def _current_user() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return f"uid {os.getuid()}"
