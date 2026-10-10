"""The lease: a wall-clock deadline that output-on requires and the watchdog enforces.

A lease is `<run_dir>/<serial>.lease`, a JSON record of the deadline (UTC) and the port
path the holder used. The serial number is the key; the port is for logs only, since the
watchdog reaches a supply through its USB serial number and never through a recorded path.
Write it under the port lock, before output-on; rewrite it to renew; remove it after
output-off. See CONTEXT.md and ADR 0001 layer 4.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_RUN_DIR = Path("/run/dps150")


@dataclass(frozen=True)
class Lease:
    serial: str
    port: str | None
    deadline: datetime | None
    """`None` when the file is malformed or unreadable: such a lease counts as expired."""

    def expired(self, now: datetime) -> bool:
        return self.deadline is None or not now < self.deadline


def lease_path(serial: str, run_dir: Path = DEFAULT_RUN_DIR) -> Path:
    if not serial or serial in (".", "..") or "/" in serial or "\0" in serial:
        raise ValueError(f"invalid supply serial number: {serial!r}")
    return Path(run_dir) / f"{serial}.lease"


def write_lease(
    serial: str,
    port: str | os.PathLike[str],
    seconds: float,
    run_dir: Path = DEFAULT_RUN_DIR,
    *,
    now: datetime | None = None,
) -> Lease:
    """Write the lease atomically, replacing any existing one. Output-on and renew both use it."""
    if not seconds > 0:
        raise ValueError(f"lease must be a positive number of seconds, got {seconds!r}")
    path = lease_path(serial, run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    start = now if now is not None else datetime.now(UTC)
    lease = Lease(serial, os.fspath(port), start + timedelta(seconds=seconds))
    record = {"port": lease.port, "deadline": lease.deadline.isoformat()}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return lease


def read_lease(serial: str, run_dir: Path = DEFAULT_RUN_DIR) -> Lease | None:
    """The lease on disk, or `None` when there is none. A malformed file reads as expired."""
    path = lease_path(serial, run_dir)
    if not path.exists():
        return None
    return _parse(serial, path)


def clear_lease(serial: str, run_dir: Path = DEFAULT_RUN_DIR) -> bool:
    """Remove the lease. Returns whether one existed."""
    path = lease_path(serial, run_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def list_leases(run_dir: Path = DEFAULT_RUN_DIR) -> list[Lease]:
    try:
        paths = sorted(Path(run_dir).glob("*.lease"))
    except OSError:
        return []
    return [_parse(path.name.removesuffix(".lease"), path) for path in paths]


def _parse(serial: str, path: Path) -> Lease:
    port = None
    try:
        data = json.loads(path.read_text())
        port = data["port"] if isinstance(data.get("port"), str) else None
        deadline = datetime.fromisoformat(data["deadline"])
        if deadline.tzinfo is None:
            raise ValueError("naive deadline")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return Lease(serial, port, None)
    return Lease(serial, port, deadline)
