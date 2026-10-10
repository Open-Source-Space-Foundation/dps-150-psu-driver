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
