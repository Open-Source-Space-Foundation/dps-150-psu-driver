# Context

Vocabulary for the DPS-150 driver. Use these terms in code, issues, and commits.

## Glossary

**Supply**: the FNIRSI DPS-150 unit. One supply has one USB serial number, which keys all per-supply state.

**Session**: the period between the session-start frame (`F1 C1 00 01 01 02`) and the session-stop frame. The device ignores every other command outside a session.

**Output**: the supply's switched DC output (register `DB`). "On" and "off" always mean the output, never the session or the USB link.

**Setpoint**: the target voltage (`C1`) or current limit (`C2`) written to the supply. Setpoints are *declarative*: the driver writes them and reads them back before every output-on, and never trusts the front panel.

**Ceiling**: the absolute maximum voltage and current compiled into the driver, 18 V / 3 A. No config or flag can raise it. The ceiling is a backstop against typos and misconfiguration; the limit is the protection sized to the connected hardware.

**Limit**: the per-runner maximum voltage and current (`PSU_MAX_V`, `PSU_MAX_I`), at or below the ceiling and sized to the board the runner powers (the CI flight controller uses 10 V / 1.5 A). CLI flags may lower a limit, never raise it. With no limit configured, the driver refuses to run.

**Clamp**: the software check that rejects any setpoint above the limit. A clamp rejects; it never silently reduces a value.

**Device protection**: the supply's own OVP/OCP thresholds (`D1`, `D2`), programmed from the limit and read back on every connect. They trip even if no host process is alive.

**Kill**: an external request to force the output off and set the inhibit latch. Any process may kill: a human, a thermal monitor, CI.

**Inhibit latch**: a persistent file (`/var/lib/dps150/<serial>.inhibit`) that blocks every output-on until a human clears it with `clear-inhibit`. It survives reboots. Only a kill sets it.

**Lease**: a deadline written at output-on (`/run/dps150/<serial>.lease`). Output-on requires a lease. When the lease expires, the watchdog turns the output off *without* setting the inhibit latch.

**Watchdog**: the `dps150-watchdog` systemd service that enforces leases. systemd's own `WatchdogSec` restarts it if it hangs.

**Port lock**: an `flock` on the serial device so that only one process talks to a supply at a time.

## Avoid

- "Disable" for output-off. It is ambiguous with "disable the driver". Say "output off".
- "Limit" for the ceiling. The ceiling is fixed; a limit is configured.
