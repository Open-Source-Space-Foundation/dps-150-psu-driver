from datetime import UTC, datetime, timedelta

import pytest

from dps150.lease import (
    MAX_LEASE_S,
    clear_lease,
    lease_path,
    list_leases,
    read_lease,
    write_lease,
)

SERIAL = "ABC123"
PORT = "/dev/serial/by-id/usb-FNIRSI_DPS-150_ABC123-if00"
T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def test_write_then_read_gives_the_deadline_and_port(tmp_path):
    written = write_lease(SERIAL, PORT, 1200, tmp_path, now=T0)
    assert written.deadline == T0 + timedelta(seconds=1200)
    assert read_lease(SERIAL, tmp_path) == written
    assert read_lease(SERIAL, tmp_path).port == PORT


def test_lease_is_live_before_and_expired_at_the_deadline(tmp_path):
    lease = write_lease(SERIAL, PORT, 60, tmp_path, now=T0)
    assert not lease.expired(T0 + timedelta(seconds=59))
    assert lease.expired(T0 + timedelta(seconds=60))


def test_renew_replaces_the_deadline(tmp_path):
    write_lease(SERIAL, PORT, 60, tmp_path, now=T0)
    write_lease(SERIAL, PORT, 600, tmp_path, now=T0)
    assert read_lease(SERIAL, tmp_path).deadline == T0 + timedelta(seconds=600)
    assert [p.name for p in tmp_path.iterdir()] == [f"{SERIAL}.lease"]


def test_clear_removes_the_lease_and_reports_whether_one_existed(tmp_path):
    write_lease(SERIAL, PORT, 60, tmp_path, now=T0)
    assert clear_lease(SERIAL, tmp_path) is True
    assert read_lease(SERIAL, tmp_path) is None
    assert clear_lease(SERIAL, tmp_path) is False


def test_write_creates_the_run_dir(tmp_path):
    run_dir = tmp_path / "run" / "dps150"
    write_lease(SERIAL, PORT, 60, run_dir, now=T0)
    assert lease_path(SERIAL, run_dir).exists()


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf"), MAX_LEASE_S + 1])
def test_write_refuses_a_lease_outside_the_allowed_length(tmp_path, seconds):
    """Six hours is GitHub's job maximum; a longer lease is a typo, not a job."""
    with pytest.raises(ValueError):
        write_lease(SERIAL, PORT, seconds, tmp_path, now=T0)
    assert read_lease(SERIAL, tmp_path) is None


def test_write_accepts_the_maximum_lease(tmp_path):
    assert MAX_LEASE_S == 6 * 3600
    lease = write_lease(SERIAL, PORT, MAX_LEASE_S, tmp_path, now=T0)
    assert lease.deadline == T0 + timedelta(hours=6)


@pytest.mark.parametrize("serial", ["", ".", "..", "a/b", "../x"])
def test_serial_cannot_escape_the_run_dir(tmp_path, serial):
    with pytest.raises(ValueError):
        write_lease(serial, PORT, 60, tmp_path, now=T0)


@pytest.mark.parametrize(
    "content",
    ["", "not json", "{}", '{"deadline": "yesterday"}', '{"deadline": "2026-10-09T12:00:00"}'],
)
def test_malformed_lease_reads_as_expired(tmp_path, content):
    lease_path(SERIAL, tmp_path).write_text(content)
    lease = read_lease(SERIAL, tmp_path)
    assert lease.expired(datetime(1970, 1, 1, tzinfo=UTC))


def test_malformed_lease_keeps_a_readable_port(tmp_path):
    lease_path(SERIAL, tmp_path).write_text(f'{{"port": "{PORT}", "deadline": "soon"}}')
    assert read_lease(SERIAL, tmp_path).port == PORT


def test_list_leases_reads_every_lease_and_ignores_other_files(tmp_path):
    write_lease("A", PORT, 60, tmp_path, now=T0)
    write_lease("B", PORT, 60, tmp_path, now=T0)
    (tmp_path / "C.inhibit").write_text("{}")
    (tmp_path / ".D.lease.tmp123").write_text("{}")
    assert [lease.serial for lease in list_leases(tmp_path)] == ["A", "B"]


def test_list_leases_of_a_missing_dir_is_empty(tmp_path):
    assert list_leases(tmp_path / "missing") == []


def test_a_crash_mid_write_leaves_the_old_lease_intact(tmp_path, monkeypatch):
    import json

    write_lease(SERIAL, PORT, 60, tmp_path, now=T0)

    def partial_dump(obj, f, **kwargs):
        f.write('{"port": "')
        raise OSError("disk full")

    monkeypatch.setattr(json, "dump", partial_dump)
    with pytest.raises(OSError):
        write_lease(SERIAL, PORT, 600, tmp_path, now=T0)
    assert read_lease(SERIAL, tmp_path).deadline == T0 + timedelta(seconds=60)
    assert [p.name for p in tmp_path.iterdir()] == [f"{SERIAL}.lease"]


def test_clear_keeps_a_lease_renewed_since_it_was_read(tmp_path):
    """Probe p5: a holder renews between the watchdog's re-read and its clear."""
    seen = write_lease(SERIAL, PORT, 60, tmp_path, now=T0)
    renewed = write_lease(SERIAL, PORT, 3600, tmp_path, now=T0)
    assert clear_lease(SERIAL, tmp_path, only=seen) is False
    assert read_lease(SERIAL, tmp_path) == renewed
    assert clear_lease(SERIAL, tmp_path, only=renewed) is True
    assert read_lease(SERIAL, tmp_path) is None


def test_clear_with_only_removes_a_malformed_lease_it_read(tmp_path):
    lease_path(SERIAL, tmp_path).write_text("garbage")
    seen = read_lease(SERIAL, tmp_path)
    assert clear_lease(SERIAL, tmp_path, only=seen) is True
    assert read_lease(SERIAL, tmp_path) is None
