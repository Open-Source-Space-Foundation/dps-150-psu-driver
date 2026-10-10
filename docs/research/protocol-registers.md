# DPS-150 protocol: registers, read-back paths, and source disagreements

Research for [#6](https://github.com/Open-Source-Space-Foundation/dps-150-psu-driver/issues/6). Input to #8 (protocol layer) and #9 (`Dps150` class).
Gathered 2026-10-09. Every source is reverse-engineered; FNIRSI publishes no protocol.

## Sources (pinned)

| Key | Source | Commit | License | Evidence basis |
| --- | --- | --- | --- | --- |
| JS | [cho45/fnirsi-dps-150 `dps-150.js`][js] | `6107bd3` | MIT (repo LICENSE) | Working WebSerial app |
| DOC | [cho45/fnirsi-dps-150 `docs/FNIRSI_DPS-150_Protocol.md`][doc] | `6107bd3` | MIT | USB sniff of official software |
| HML | [hmldns/dps150ctl `PROTOCOL.md`][hml], [`protocol.py`][hmlp], [`driver.py`][hmld], [fixture][hmlf] | `9edd13a` | MIT (repo LICENSE) | **Hardware captures on HW V1.0 / FW V1.2**, committed as fixtures |
| NAS | [nasheed-x/dps150_api `PROTOCOL.md`][nas], [`dps150.py`][nasp] | `2fa873a` | MIT (repo LICENSE) | Decompiled official Windows app (.NET) |
| CAJ | [cajunpanda/dps150 `dps150.py`][caj] | `6a088ae` | MIT (repo LICENSE) | Working CLI |
| KOC | [KochC/DPS-150-python-library `dps150/`][koc] | `ae1df44` | **No LICENSE file.** `setup.py` L23 classifier and README L576 claim MIT; GitHub reports no license | Async library, port of JS |

Citations below use `KEY Lnn` for line numbers in the pinned file. HML is the only source with on-device captures in the repo, so it outranks the others where they conflict.

## Wire format (confirms #8)

- Host frame `F1 <cmd> <reg> <len> <data…> <chk>`; device frame `F0 <cmd> <reg> <len> <data…> <chk>`; total size `5 + len`. JS L7-8, L174-182; DOC L50-52; NAS L52-68; HML L14-19.
- Checksum `(reg + len + sum(data)) & 0xFF`; header and command bytes excluded. **All six sources agree** (JS L170-172 and L119-123; DOC L67; NAS L66; HML L19, `protocol.py` L55; CAJ L96-97; KOC `protocol.py` L50-53).
- Commands: `A1` get / every device reply and push, `B1` set, `B0` baud, `C1` session, `C0` **enter firmware-update (DFU) mode, never send it** (USB port vanishes, needs replug). JS L10-13; NAS L84-90, L408-415, L481; HML L25-36.
- Every device frame in every source and capture uses command `A1`, so scanning for `F0 A1` (JS L108) is sound. HML's parser also tolerates other command bytes (`protocol.py` L112) defensively.
- float32 little-endian (JS L188; DOC L40-42; NAS L316). Strings are ASCII, length from `len`, may hold a NUL (HML L21, `protocol.py` L157).
- The byte stream fragments arbitrarily: the V1.2 capture has a lone `f0` read followed by the rest of the frame, and two frames in one read ([fixture][hmlf] lines 3-7; HML L22-23).
- Resync advice for #8: on a bad checksum, drop **one byte** and rescan (HML `protocol.py` L121-125, CAJ L142). JS instead skips the declared payload length (JS L124-127), which loses a good frame if the length byte was the corrupted one.

## Read-back paths

GET frame: `F1 A1 <reg> 01 00 <chk>` (one zero data byte), as the official app sends (NAS L478) and as HML captured on hardware (HML L66-72). A zero-length GET (`F1 A1 <reg> 00 <reg>`) is also accepted per NAS L478, and CAJ L269/KOC `device.py` L390 use it; prefer the one-byte form.

HML read **every** register `C0`-`E3` individually by GET on a V1.2 unit and got valid replies (HML L84-87). So every register below can be read on request.

| Reg | Meaning | Type | GET on request | Pushed by device | `FF` offset | Sources |
| --- | --- | --- | --- | --- | --- | --- |
| `C1` | Voltage setpoint V | f32 | Yes | No | 4 | HML L84, L92, L126; NAS L178, L260; JS L263 |
| `C2` | Current setpoint A | f32 | Yes | No | 8 | HML L93, L126; NAS L179, L261; JS L264 |
| `DB` | Output 0=off 1=on | u8 | Yes | On change | 107 | HML L103, L133; DOC L271; NAS L152, L187, L288; JS L291 |
| `D1` | OVP threshold V | f32 | Yes | No | 76 | HML L97, L130; NAS L216, L278; JS L281 |
| `D2` | OCP threshold A | f32 | Yes | No | 80 | HML L97, L130; NAS L217, L279; JS L282 |
| `DC` | Protection 0 OK,1 OVP,2 OCP,3 OPP,4 OTP,5 LVP,6 REP | u8 | Yes | On change | 108 | HML L104, L114, L133; DOC L272; NAS L153, L237-247, L289; JS L50-58, L292 |
| `DD` | 0=CC 1=CV | u8 | Yes | On change | 109 | HML L105, L133; DOC L273; NAS L154, L290; JS L293 |
| `DE` | Model, e.g. `DPS-150` | str | Yes | No | not in `FF` | HML L66-67; NAS L190 |
| `DF` | Hardware version, e.g. `V1.0` | str | Yes | No | not in `FF` | HML L68-69 |
| `E0` | Firmware version, e.g. `V1.2` | str | Yes | No | not in `FF` | HML L70-71 |
| `C3` | Measured V, A, W | 3×f32 | Yes | Every ~500 ms (even output off) | 12, 16, 20 | DOC L225-233; NAS L143; HML L151-153 |
| `E2`/`E3` | Max V (≈ input − 0.2 V) / max A (5.1) | f32 | Yes | Every ~500 ms | 111, 115 | DOC L230-231; HML L135, L155-159 |
| `FF` | Full state | 139 bytes | Yes | No | — | DOC L279-281; HML L72-73, L120-121 |

Request `FF` with `F1 A1 FF 01 00 00` (HML L72, NAS L253). Its reply is `F0 A1 FF 8B <139 bytes> <chk>`.

### `FF` payload layout (offsets from first payload byte)

Packed, no padding; floats at 99, 103, 111 onward are unaligned (HML L138). Agreed by JS L262-302, NAS L257-298, HML L123-136.

| Offset | Type | Field | Value in V1.2 capture |
| --- | --- | --- | --- |
| 0 | f32 | Input V | 4.99 |
| **4** | f32 | **Voltage setpoint `C1`** | 3.3994 |
| **8** | f32 | **Current setpoint `C2`** | 1.0 |
| 12, 16, 20 | f32 | Measured V, A, W | |
| 24 | f32 | Temperature °C | |
| 28-72 | 12×f32 | Presets M1-M6 V/A pairs | |
| **76** | f32 | **OVP `D1`** | 30.0 |
| **80** | f32 | **OCP `D2`** | 5.1 |
| 84, 88, 92 | f32 | OPP W, OTP °C, LVP V | 150, 80, 5.0 |
| 96, 97 | u8 | Brightness, volume | 10, 2 |
| 98 | u8 | Metering flag (polarity disputed, see below) | 1 |
| 99, 103 | f32 | Capacity Ah, energy Wh | |
| **107** | u8 | **Output `DB`** | 0 |
| **108** | u8 | **Protection `DC`** | 5 (LVP) |
| **109** | u8 | **CC/CV `DD`** | 1 (CV) |
| 110 | u8 | Unknown, reserved | 1 |
| 111, 115 | f32 | Max V, max A (`E2`, `E3`) | 4.7899, 5.1 |
| 119-135 | 5×f32 | OVP/OCP/OPP/OTP/LVP **maximum allowed thresholds** (NAS L294-302 from decompile; HML L136, L143-146) | 30, 5.1, 150, 99, 30 |

The capture column is my decode of the V1.2 `FF` reply in [HML fixture][hmlf] (checksum valid, length 139), run with a local script, not taken from a write-up.

## Session, baud, and serial settings

- Session start `F1 C1 00 01 01 02`; session stop `F1 C1 00 01 00 01`. All sources agree: DOC L81-82, NAS L99/L131, HML L65/L74, JS L84/L145 (JS sends `[0]`/`[1]` as data).
- Telemetry streams only after session start (CAJ L16).
- Baud select `F1 B0 00 01 <idx> <chk>`, index 1-based: 1=9600 … 5=115200, so 115200 is `F1 B0 00 01 05 06` (DOC L90-99, NAS L25-34/L118, JS L147, CAJ L236, KOC `device.py` L159-160). The official app sends it after session start (NAS L116-118). HML never sends it and works on V1.2 (HML L12). It only re-asserts 115200; sending it is harmless and matches the official app.
- Init order in the official app: session start, (optional `E1` address check), baud, `DE`, `E0`, `DF`, `FF` (NAS L104-126). It retries `DE`/`E0` on a 1 s timer, so the first GET can be lost.
- Serial: 115200 8N1. **Flow control: do not enable RTS/CTS.** The official app sets `RtsEnable = true` with no handshake (NAS L22, L36-39). HML runs with `rtscts=False` and pyserial's default asserted RTS/DTR on hardware (HML L9-11, `driver.py` L77-83). NAS opens `rtscts=False` and sets `rts=True` (`dps150.py` L244-249). JS (`flowControl: 'hardware'`, L75), KOC (`transport.py` L56) and CAJ (L231, with a `--no-rtscts` escape hatch "try if reads hang", L545-546) enable RTS/CTS and also work. DOC says "None" (L39). Recommendation: `rtscts=False`, RTS asserted (pyserial default).
- Space commands ~50 ms apart: JS L197, HML L11 and `driver.py` L116-118, NAS L165-167.

## Do SET frames echo?

Disputed. **Do not use the echo as the read-back.**

- DOC shows the device answering a set with an `F0 A1` frame of the same value (DOC L113-114 for `C1`, L144-151 for `DB`).
- NAS says the device does not echo individual register writes and the app re-reads `FF` after each write (NAS L473). DOC L291 also says the official software issues `FF` after every write.
- HML verifies writes by reading `FF` after the write (`driver.py` L197-203, L221-224), never by echo. That path is the one proven on hardware: SET `C1`, `C2`, `DB=1` read back correctly (HML L179-183).
- `DB`, `DC`, `DD` are pushed on change (DOC L267-273, NAS L148-154). No hardware capture in any repo shows that push, so treat it as a bonus, not the confirmation.

For #9: after a write, issue a GET for the register (or `FF`) and wait for the matching `F0 A1 <reg>` reply, discarding interleaved telemetry. HML's `read_register` (L143-158) is the pattern: drain queued frames, send GET, scan replies until the register matches or the timeout expires. HML accepts a setpoint read-back within one display step (0.01 V / 0.001 A) plus float margin (HML L193-194, `driver.py` L184-185, L201), because the device stores float32 and can round (DOC L124 sends 0.5 A as `FD FF FF 3E` = 0.49999991).

## Session stop and USB disconnect behavior

- **Session stop does not change the output.** CAJ L18-19: "The output on/off state and stored setpoints are NOT affected by connect/disconnect." HML `driver.py` L106: "Session close releases remote control, not the output relay." HML L166-169 reconnected after output-on and found the output still on.
- Session start locks the front panel; session stop releases it (HML L49-60, citing the FNIRSI manual). HML found the lock persists after the host closes the port without session stop (HML L56-58). No register reports the lock state (HML L52).
- NAS's own `disconnect()` writes `DB=0` before session stop precisely because session stop leaves the output on (NAS `dps150.py` L281-293, README L103, L140).
- **USB unplug: no source documents it.** HML L59: "No persistence across power/USB resets is promised." This matches ADR 0001 item 5: measure it on the bench.
- Undocumented claim in `CONTEXT.md`: "The device ignores every other command outside a session." No source states this. CAJ L16 only says telemetry starts after session start.

## Firmware versions

- The only hardware-verified unit is HW V1.0 / FW V1.2 (HML L4-5). No source names another firmware version or a behavior that differs by version.
- `FF` was 139 bytes on V1.2 (HML L121). HML's decoder accepts ≥119 bytes defensively but notes "only the 139-byte layout has been verified on hardware" (HML L139-141). KOC requires ≥139 (`device.py` L320).
- The `E1` device-address register (default checked by the official app, NAS L104-112) read as 1 on V1.2 (HML L85).

## Where sources disagree or are wrong

Example checksums were recomputed locally with `(reg + len + sum(data)) & 0xFF`.

| # | Claim | Source | Finding |
| --- | --- | --- | --- |
| 1 | Metering request `F1 A1 D8 01 01 02` | DOC L254 | **Wrong twice.** Checksum should be `DA`; command should be `B1` (set), as JS L385 and NAS L393 (`F1 B1 D8 01 01 DA`) send it. |
| 2 | Capacity push `F0 A1 D9 04 9B D6 34 00 81` | DOC L257 | Checksum should be `82`. |
| 3 | Baud template `F1 B0 00 01 XX 06` | DOC L90 | `06` is right only for `XX=05`; checksum is `XX + 1`. |
| 4 | Set OVP 25 V `F1 B1 D1 04 00 00 C8 41 4E` | NAS L388 | Checksum should be `DE`. Relevant: #9 writes `D1`. |
| 5 | `FF` request | DOC L198 vs L280 | Two forms: `F1 A1 FF 01 00 00` and `F1 A1 FF 00 FF`. Both checksum correctly; use the one-byte form (official app, HML hardware). |
| 6 | Flow control | DOC L39 "None"; JS L75, KOC, CAJ RTS/CTS; NAS, HML no RTS/CTS with RTS asserted | Use no RTS/CTS, RTS asserted (official app, HML hardware). |
| 7 | Set frames echo | DOC L113-114, L144-151 yes; NAS L473 no | Unresolved; read back explicitly. |
| 8 | Metering flag polarity (`FF`[98]) | JS L288 comment "open=0 close=1" but L346 maps 0 to closed; NAS L285 0=running; CAJ L197 1=on | Contradictory, even within JS. Not needed by the driver. |
| 9 | Device validates checksums | NAS L479: official app sends the baud frame with a stale checksum and the device accepts it | Unverified claim. Always send correct checksums; do not rely on rejection either way. |
| 10 | `FF`[119-135] meaning | JS L298-302 "???"; NAS L294-302 protection maxima | NAS (decompile) and HML capture values (30 V, 5.1 A, …) agree on maxima. |
| 11 | `DC`=5 (LVP) means output inhibited | HML L171-174 | LVP stayed set while output on and regulating at ~5 V input. `DC` semantics (latched vs live) are unresolved. |

## What #9 needs to know

1. Read-back of `C1`, `C2`, `D1`, `D2`, `DB`, `DC`, `DD`, `DE`, `E0`: GET each register, or one `FF` (offsets 4, 8, 76, 80, 107, 108, 109). Strings `DE`/`DF`/`E0` are GET-only.
2. Writes of `D1`/`D2` are documented from the official software (DOC L181-200, NAS L216-217) but **no source verified them on hardware** (HML L196 excludes protection writes). The connect-time OVP/OCP program-and-verify is the first test of that on our bench.
3. Device maxima: OVP ≤ `FF`[119], OCP ≤ `FF`[123] (30 V, 5.1 A on V1.2). Our 10 V / 3 A ceiling is well inside them.
4. Max output voltage `E2` tracks input minus ~0.2 V (DOC L230; HML L155-158). A 10 V setpoint needs a USB-PD input above ~10.2 V. No source says what the device does with a setpoint above `E2`.
5. `DC` may read non-zero (LVP) at low input with the output working (HML L171-174). Do not treat `DC != 0` alone as "output off" or as a connect failure without bench data.
6. Close must not assume session stop turns output off. Only `DB=0` does.

[js]: https://github.com/cho45/fnirsi-dps-150/blob/6107bd34531aa52692d757e3b82d9573750311ac/dps-150.js
[doc]: https://github.com/cho45/fnirsi-dps-150/blob/6107bd34531aa52692d757e3b82d9573750311ac/docs/FNIRSI_DPS-150_Protocol.md
[hml]: https://github.com/hmldns/dps150ctl/blob/9edd13ae51adb35dede5a7a975a65b07c0883cb2/PROTOCOL.md
[hmlp]: https://github.com/hmldns/dps150ctl/blob/9edd13ae51adb35dede5a7a975a65b07c0883cb2/dps150ctl/protocol.py
[hmld]: https://github.com/hmldns/dps150ctl/blob/9edd13ae51adb35dede5a7a975a65b07c0883cb2/dps150ctl/driver.py
[hmlf]: https://github.com/hmldns/dps150ctl/blob/9edd13ae51adb35dede5a7a975a65b07c0883cb2/tests/fixtures/v1.2-state.jsonl
[nas]: https://github.com/nasheed-x/dps150_api/blob/2fa873ae3e75a11b80def34e10209c4df944034e/PROTOCOL.md
[nasp]: https://github.com/nasheed-x/dps150_api/blob/2fa873ae3e75a11b80def34e10209c4df944034e/dps150.py
[caj]: https://github.com/cajunpanda/dps150/blob/6a088ae9f57cf5b72377123e03b47783258680e1/dps150.py
[koc]: https://github.com/KochC/DPS-150-python-library/tree/ae1df445d5993bdc0839d863ac1ba25d9d1bbfae/dps150
