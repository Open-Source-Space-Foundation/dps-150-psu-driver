# dps-150-psu-driver

Python driver and CLI for the FNIRSI DPS-150 programmable power supply, with safety guardrails for powering flight hardware on PROVES CI benches.

Status: in development. Track the work on the [wayfinding map](https://github.com/Open-Source-Space-Foundation/dps-150-psu-driver/issues?q=label%3Awayfinder%3Amap).

Protocol reference: [cho45/fnirsi-dps-150](https://github.com/cho45/fnirsi-dps-150) (MIT).

## Limits

The driver compiles in a ceiling of 18 V / 3 A. No config or flag can raise it. The ceiling only catches typos and misconfiguration. The protection sized to your hardware is the per-runner limit, `PSU_MAX_V` and `PSU_MAX_I`. Each runner must set a limit sized to the board it powers. The driver refuses any setpoint above the limit and programs the limit as the supply's OVP/OCP trip. See [ADR 0001](docs/adr/0001-layered-fail-safe.md).

The supply outputs at most about 0.2 V below its USB-PD input (register `E2`). Reaching 18 V needs a USB-PD source that negotiates at least 18.2 V.

Example runner `.env` for the CI flight controller:

```sh
PSU_VOLTAGE=8.2  # Output voltage setpoint (CV): on-orbit nominal for a power-positive satellite with nearly full batteries.
PSU_CURRENT=1.2  # Current-limit setpoint (CC): the board draws what it needs; above 1.2 A the supply holds current and lets voltage sag.
PSU_MAX_V=10     # Voltage limit: setpoints above it are refused, and it is programmed as the supply's OVP trip.
PSU_MAX_I=1.5    # Current limit: programmed as the supply's OCP trip, 0.3 A above PSU_CURRENT so a short current-limits before it trips.
```

## Lease and watchdog

Output-on requires a lease (ADR 0001 layer 4). The holder writes `<run_dir>/<serial>.lease`
(default `/run/dps150`) under the port lock before output-on, rewrites it to renew, and removes
it after output-off. `dps150-watchdog` polls the directory and, when a lease has expired, takes
the port lock and turns the output off with `force_output_off`. Expiry never sets the inhibit
latch. On start it also sweeps every DPS-150 on USB and turns off any without a live lease.
Install the unit from `packaging/systemd/dps150-watchdog.service`; its comments say what to
edit for the runner. On the Pi500 bench: `User=proves` and
`ExecStart=/home/proves/dps150/.venv/bin/dps150-watchdog` (systemd does not expand `~`).

Rules the lease depends on:

- Never run `dps150` under `sudo`. A lease root owns with mode 0600 is unreadable to the
  watchdog, reads as malformed, and gets the output forced off.
- Output-on fails closed when `/run/dps150` does not exist, which means the watchdog unit
  is not installed. Install it before powering anything.
- The lease is only as strong as lock holders releasing the port. The watchdog waits 1 s for
  the port lock and retries on the next poll, so a process that holds `port_lock` for a long
  time delays the forced off for as long as it holds it. Hold the lock for one command, never
  for a session.

## CLI

`dps150` runs one command against one supply and exits. Its core is Korad-compatible:
`dps150 -d PORT --output 1` and `dps150 -d PORT --output 0`.

```sh
dps150 -d PORT                          # status (also --status)
dps150 -d PORT --output 1 --lease 1200  # output on; also --output on
dps150 -d PORT --output 0               # output off; also --output off
dps150 -d PORT kill --reason "board hot"
dps150 kill -d PORT                     # same: kill and clear-inhibit take -d, --state-dir
dps150 -d PORT clear-inhibit            # and --run-dir on either side of the subcommand
```

`--output` and a subcommand together is a usage error.

| Option | Environment | Used by |
|---|---|---|
| `-d`, `--device` | `PSU_PORT` | every command |
| | `PSU_MAX_V`, `PSU_MAX_I` | output on |
| `--max-voltage`, `--max-current` | | output on: lowers the limit for this command |
| `-v`, `--voltage` | `PSU_VOLTAGE` | output on |
| `-i`, `--current` | `PSU_CURRENT` | output on |
| `--lease SECONDS` | `PSU_LEASE_S` | output on, required, at most 6 h |
| `--state-dir` (default `/var/lib/dps150`) | | where the inhibit latch lives |
| `--run-dir` (default `/run/dps150`) | | where the lease lives |
| `kill --reason TEXT` (default `kill`) | | kill |

A flag overrides its environment variable. The limit comes only from the environment:
`--max-voltage` and `--max-current` may lower it and never raise it, and they are refused
when the environment sets no limit. An environment limit above the ceiling is a
configuration error even when a flag lowers it. Only output on needs a limit, so status,
output off, and kill still work when the limit configuration is missing or broken.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Done. |
| 1 | The supply did not do what was asked: no reply, a read-back mismatch, a setpoint the clamp rejects, or an error on the port. |
| 2 | Usage or configuration: a bad flag, no port, a missing, unparseable, or above-ceiling limit, a flag that raises the limit, a missing setpoint, a missing lease, or a lease out of range. |
| 3 | The supply is inhibited. A human must run `dps150 clear-inhibit`. |
| 4 | Another process holds the port lock. |
| 5 | No supply at the port: the port path does not exist (unplugged, or the wrong `PSU_PORT`), it is not a DPS-150, or the supply reports no USB serial number. |
| 6 | The run dir is missing or not writable: `dps150-watchdog` is not installed or not running. |

Status goes to stdout. Every error and warning goes to stderr with the prefix `dps150: `.
The messages that need action are these:

- `no limit configured: set PSU_MAX_V and PSU_MAX_I ...` (code 2).
- `port PATH does not exist (supply unplugged or wrong PSU_PORT)` (code 5). Every command
  checks this before it touches anything, except kill, which sets the latch first and then
  reports it with `OUTPUT MAY STILL BE ON`.
- `--max-voltage ... flags may lower a limit, never raise it` (code 2).
- ``supply SERIAL is inhibited: latched by WHO at WHEN (REASON); a human must run
  `dps150 clear-inhibit` before output-on`` (code 3).
- `RUN_DIR is not a writable directory: dps150-watchdog not installed or not running (see
  README)` (code 6).
- ``INHIBIT LATCH IS SET for supply SERIAL but the OUTPUT MAY STILL BE ON: ERROR. Free the
  port and run `dps150 kill` again.`` A kill could not confirm the output off.
- `output off confirmed but INHIBIT LATCH IS NOT SET for supply SERIAL: ERROR`. A kill
  turned the output off but could not write the latch.

### What each command does

**Output on** checks that the run dir exists and is writable before it touches the
device, then takes the port lock and checks the inhibit latch. It opens a session, which
programs OVP/OCP from the limit and reads them back, then writes and reads back the
setpoints. It checks the latch a second time, writes the lease, and turns the output on.
It prints `output on: V V / I A, lease until DEADLINE`.

If anything fails after the lease is written, the lease stays in place. The driver already
tries to turn the output off and confirm it when output-on fails, but the CLI cannot tell a
confirmed off from an unconfirmed one. A lease left behind makes the watchdog force the
output off at expiry. That is harmless on a supply that is already off, and it is the right
action on one that is not.

**Output off** takes the port lock and writes only the output register (`DB=0`), then
reads it back. It programs no protection and needs no limit. It then removes the lease. A
missing run dir is not an error here. With no USB serial number it still turns the output
off, then exits 5.

**Kill** sets the inhibit latch first, then waits up to 60 s for the port lock and turns
the output off. The 60 s wait lets a long command from another process finish. It removes
the lease only after the output off is confirmed. If the lock wait times out or the off
fails, kill exits non-zero with the loud `OUTPUT MAY STILL BE ON` message and keeps the
lease, so the watchdog still turns the output off at expiry.

**Clear-inhibit** removes the latch and prints who set it, when, and why. It does not touch
the device. `dps150.state` also logs the clear, with who cleared it, at WARNING on stderr.
If a kill sets a new latch between the moment clear-inhibit reads the latch and the moment
it removes the file, that new latch is removed without a log line. The window is one
`lstat` wide.

**Status** is read-only. It takes the port lock and reads the supply with `read_status`,
which sends GETs only: it never writes a setpoint, the protection, or the output register,
and a failed read leaves the output as it was. It is safe against a supply another job is
using, and it needs no limit. It prints one `key: value` line each for `model`, `firmware`,
`voltage_setpoint`, `current_setpoint`, `output`, `protection`, `mode`, `measured_voltage`,
`measured_current`, `measured_power`, `ovp`, `ocp`, `max_voltage`, `inhibit`, and `lease`.
`ovp` and `ocp` are the trips the supply holds right now, which the last output-on
programmed; `max_voltage` is the supply's input-limited maximum (register `E2`). It does
not need the run dir; with none, `lease` is `none`.

### Run as the runner account

Run every `dps150` command as the runner account, the same user as `dps150-watchdog`. The
latch and the lease live in directories that account owns, and the files are mode 0600. A
`clear-inhibit` or `kill` from another user fails on permissions, and a lease written by
another user is unreadable to the watchdog.
