# dps-150-psu-driver

Python driver and CLI for the FNIRSI DPS-150 programmable power supply, built to power flight hardware on PROVES CI benches. It replaces the hand-installed Korad script (`korad_control.py`) that [proves-core-reference](https://github.com/Open-Source-Space-Foundation/proves-core-reference) CI calls today, and adds safety guardrails that live in the driver itself.

Read `CONTEXT.md` for vocabulary and `docs/adr/` before changing anything that touches limits, the inhibit latch, or the lease.

## Hard rules

- The supply powers flight hardware. Never weaken a guardrail (ceiling, clamp, device OVP/OCP, inhibit latch, lease) to make a test pass.
- Unit tests run against a mock serial backend and never need hardware.
- Bench tests run on the `proves-pi500` host over ssh. With a board connected, cap setpoints at the board rating the runner's `.env` declares.

## Tooling

Python >= 3.11, managed with `uv`. `pytest` for tests, `ruff` for lint and format.

## Agent skills

### Issue tracker

GitHub Issues on `Open-Source-Space-Foundation/dps-150-psu-driver` (always pass `-R`). See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
