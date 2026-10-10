# 0001: Layered fail-safe; the output defaults to off when control is lost

## Status

Accepted, 2026-10-09.

## Context

CI calls the driver one shot at a time (`--output 1`, run tests, `--output 0`). Between calls, no host process controls the supply, and the output stays on. Control can be lost in several ways: the driver process crashes, the CI job dies, the USB link drops, or the host hangs. A future bench thermal sensor must also be able to force the output off and keep it off.

## Decision

The safety guarantees sit in layers. Each layer covers the failures the layers above it miss.

1. **Ceiling and clamp** (driver): reject setpoints above the configured limit, and reject limits above the compiled ceiling. Refuse to run with no limit configured.
2. **Device protection** (supply): program OVP/OCP from the limit and read them back on every connect, so the supply trips with no host alive.
3. **Inhibit latch** (driver, persistent): a kill turns the output off and writes a latch file that survives reboots. Output-on refuses while the latch exists. Only a human clears it.
4. **Lease** (watchdog, L1): output-on requires a lease. The `dps150-watchdog` systemd unit turns the output off when the lease expires. Expiry does *not* latch: a lease expiry is a supervision failure, not a hazard reading, and off is already the safe state.
5. **Device behavior on disconnect** (L0): measured on the bench, not assumed. If the supply turns its output off on USB loss, that becomes a free kill path.
6. **Hardware dead-man relay** (L2): the only layer that covers a host kernel hang or a power loss. Deferred to a future issue.

External kill is a one-shot CLI (`dps150 kill`) that takes the port lock. We considered a long-running daemon that owns the port, and deferred it until a monitoring need justifies one.

## Consequences

- Software alone cannot guarantee off if the host dies. Only L2 can.
- CI must pass a lease (`PSU_LEASE_S`) and cap job time (`timeout-minutes`), so a hung job cannot hold power longer than one lease.
- A crashed CI job does not block the runner; a thermal kill does, until a human looks.

## Amendment (2026-10-09)

The compiled ceiling is raised from 10 V / 3 A to 18 V / 3 A. Decision record: [#16](https://github.com/Open-Source-Space-Foundation/dps-150-psu-driver/issues/16).

Rationale: future benches will simulate a solar array, which needs setpoints above 10 V. A 10 V ceiling would force a driver release for each such bench. The current ceiling stays at 3 A.

Consequences:

- The ceiling is now a backstop against typos and misconfiguration, not the protection sized to the board. The per-runner limit (`PSU_MAX_V`, `PSU_MAX_I`) is that protection: the clamp enforces it, and the driver programs it as the supply's OVP/OCP.
- Every runner must declare a limit sized to its connected hardware. The CI flight controller runs at 10 V / 1.5 A.
- The supply outputs at most about 0.2 V below its USB-PD input (register `E2`). Reaching 18 V needs a USB-PD source that negotiates at least 18.2 V.
