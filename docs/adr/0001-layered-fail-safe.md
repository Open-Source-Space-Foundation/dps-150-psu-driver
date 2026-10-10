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
5. **Device behavior on disconnect** (L0): measured on the bench, not assumed. The supply keeps its output on through every host-side loss of control, so L0 is not a kill path. See [L0 measured](#l0-measured-2026-10-10).
6. **Hardware dead-man relay** (L2): the only layer that covers a host kernel hang or a power loss. Deferred to a future issue.

**Note (2026-10-10): a device protection trip escalates to the inhibit latch.** On the
bench ([#13](https://github.com/Open-Source-Space-Foundation/dps-150-psu-driver/issues/13#issuecomment-6094429267),
firmware V1.2) an OVP trip turned the output off and left `DC=1` latched through
output-off and setpoint changes; the next `SET DB=1` both cleared it and turned the
output on. Layer 2 therefore does not hold on its own across CI cycles: the next
`--output 1` would re-energize the faulted board. Output-on now reads `DC` before `DB=1`
and, on a trip (OVP, OCP, OPP, OTP, REP, or an unknown code; LVP is excluded, a healthy
unit on a 5 V USB input reports it), sets the layer 3 latch with the trip named as the
reason and exits 3. Clearing that latch leaves a one-shot acknowledgement so the next
output-on may proceed while `DC` still reports the same trip. No new mechanism; a trip is
a kill the supply raised itself.

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

## L0 measured (2026-10-10)

Bench record: [#13](https://github.com/Open-Source-Space-Foundation/dps-150-psu-driver/issues/13#issuecomment-6094429267) (software gates) and [#13](https://github.com/Open-Source-Space-Foundation/dps-150-psu-driver/issues/13#issuecomment-6094554250) (operator steps).

Device: DPS-150, firmware V1.2, USB serial `14717A8D6866`, on host `proves-pi500`. Conditions: nothing on the output, output on at 1.0 V / 0.05 A, limit 5 V / 0.1 A.

| Loss of control | Output afterwards |
| --- | --- |
| Driver process killed with SIGKILL, session open | Stays on. |
| Session stop | Stays on. |
| USB unbind and rebind of the data link, 5 s | Stays on. |
| Data cable unplugged | Stays on with the last setpoints (operator observation). |
| PD input unplugged, data cable connected | The supply stays powered from the host's USB port, and the output stays on at a reduced level, about 3.3 V (operator observation). |
| Total power loss (PD input and host USB both gone) | The supply powers up with the output off. |

Consequences:

- L0 is not a kill path. Layers 3, 4, and 6 carry every host-side loss of control.
- A CI host power loss takes the supply down only when the two share a power source. The supply then comes back with the output off. A supply on its own power keeps the output on with no host alive, and the watchdog dies with the host, so only layer 6 covers that case (inferred from the data-cable row, not measured).
- When the PD input drops with the data cable connected, the supply runs from the host's USB port with the output on. With a load on the output, that current comes out of the host (inferred, not measured with a load).

SET echo (gate 12), 300 ms watch window after each SET:

- `C1`, `C2`, `D1`, and `D2` never echo: 0 frames in 120 SETs, 30 each.
- `DB` echoes every SET (120 of 120), including a SET that does not change the output. The worst latency observed was 29.2 ms, inside the 50 ms flush the driver runs after each SET and before the read-back GET. `COMMAND_SPACING_S` stays as it is.
- A `D1` write takes effect on the protection engine. With `D1=2.0` and the output on at 1.0 V, writing `C1=3.0` tripped OVP (`DB=0`, `DC=1`) about 1 s later. A `D2` write reads back, but an OCP trip needs a load, so its effect is not yet measured ([#16](https://github.com/Open-Source-Space-Foundation/dps-150-psu-driver/issues/16)).

The USB-PD contract varies between power-ups. The bench saw a 20 V contract (`E2` 19.876 V) and, after a replug, a 12 V contract (`E2` 11.90 V). `E2` is the input minus about 0.2 V, and the driver refuses any setpoint above it. The CI setpoint of 8.2 V fits under both; a 9 V contract would leave about 0.6 V of headroom.

The Pi500 and the supply shared one 60 W USB-PD brick. When either port renegotiates, the brick briefly interrupts all of its outputs. During the PD-unplug test this reset the Pi500 (`Undervoltage detected!` at boot); the operator confirmed the cause ([#16](https://github.com/Open-Source-Space-Foundation/dps-150-psu-driver/issues/16#issuecomment-6094594213)). Power the CI host and the supply from separate sources. Never share a PD brick between them.
