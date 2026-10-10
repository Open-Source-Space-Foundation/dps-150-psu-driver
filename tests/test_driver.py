import math
from itertools import pairwise

import pytest
from conftest import FACTORY_REGISTERS, assert_only_allowlisted_commands

from dps150.driver import (
    CEILING,
    USB_PID,
    USB_VID,
    ClampError,
    Dps150,
    Dps150Error,
    Limit,
    LimitError,
    Measurement,
    NoReply,
    ReadbackMismatch,
    WrongDevice,
    open_serial,
)
from dps150.protocol import CMD_GET, CMD_SESSION, CMD_SET, encode, pack_f32, unpack_f32
from dps150.testing import FakeClock, MockSerial

PORT = "/dev/serial/by-id/usb-FNIRSI_DPS-150_ABC123-if00"
SESSION_START = encode(CMD_SESSION, 0x00, b"\x01")
SESSION_STOP = encode(CMD_SESSION, 0x00, b"\x00")
OUTPUT_ON = encode(CMD_SET, 0xDB, b"\x01")
OUTPUT_OFF = encode(CMD_SET, 0xDB, b"\x00")


def dps150_usb_id(path):
    return (USB_VID, USB_PID)


def make_supply(max_voltage=5.0, max_current=1.0, usb_id=dps150_usb_id, **port_kwargs):
    clock = FakeClock()
    port = MockSerial(clock=clock, **port_kwargs)
    port.registers.update(FACTORY_REGISTERS)
    supply = Dps150(
        PORT,
        max_voltage=max_voltage,
        max_current=max_current,
        serial_factory=lambda path: port,
        usb_id=usb_id,
        clock=clock,
    )
    return supply, port, clock


def get_frame(register):
    return encode(CMD_GET, register, b"\x00")


def writes_of(port, command, register):
    return [f for f in port.written if f[1] == command and f[2] == register]


def index_of(port, frame):
    return port.written.index(frame)


# Limits and ceiling


def test_ceiling_is_18_v_3_a():
    assert CEILING == Limit(18.0, 3.0)


def test_ceiling_survives_the_float32_wire_format_exactly():
    assert unpack_f32(pack_f32(CEILING.voltage)) == CEILING.voltage
    assert unpack_f32(pack_f32(CEILING.current)) == CEILING.current


@pytest.mark.parametrize("kwargs", [{}, {"max_voltage": 5.0}, {"max_current": 1.0}])
def test_refuses_to_construct_without_a_limit(kwargs):
    with pytest.raises(LimitError):
        Dps150(PORT, **kwargs)


@pytest.mark.parametrize(
    "max_voltage, max_current",
    [(18.01, 1.0), (5.0, 3.01), (0.0, 1.0), (5.0, -1.0), (math.nan, 1.0), (5.0, math.inf)],
)
def test_refuses_a_limit_above_the_ceiling_or_not_positive(max_voltage, max_current):
    with pytest.raises(LimitError):
        Dps150(PORT, max_voltage=max_voltage, max_current=max_current)


@pytest.mark.parametrize("max_voltage", [10.0, 10.01, 18.0])
def test_limit_at_or_below_the_ceiling_is_accepted(max_voltage):
    supply = Dps150(PORT, max_voltage=max_voltage, max_current=3.0)
    assert supply.limit == Limit(max_voltage, 3.0)


# Open


def test_open_refuses_a_device_with_the_wrong_vid_pid():
    opened = []
    supply, port, _ = make_supply(usb_id=lambda path: (0x2341, 0x0043))
    supply._serial_factory = lambda path: opened.append(path)
    with pytest.raises(WrongDevice):
        supply.open()
    assert opened == []


def test_open_refuses_a_path_with_no_usb_identity():
    supply, port, _ = make_supply(usb_id=lambda path: None)
    with pytest.raises(WrongDevice):
        supply.open()
    assert port.written == []


def test_open_checks_the_resolved_port_path():
    seen = []

    def usb_id(path):
        seen.append(path)
        return (USB_VID, USB_PID)

    supply, _, _ = make_supply(usb_id=usb_id)
    supply.open()
    assert seen == [PORT]


def test_open_starts_the_session_first_and_reads_model_and_firmware():
    supply, port, _ = make_supply()
    supply.open()
    assert port.written[0] == SESSION_START
    assert supply.model == "DPS-150"
    assert supply.firmware == "V1.2"


def test_open_flushes_stale_bytes_holding_a_false_header():
    supply, port, clock = make_supply()
    clock.sleep(2.0)
    port.push(b"\xf0\xa1\xc3\xff\x01\x02")
    supply.open()
    assert supply.model == "DPS-150"
    assert len(writes_of(port, CMD_GET, 0xDE)) == 1


def test_open_programs_ovp_ocp_from_the_limit_and_reads_them_back():
    supply, port, _ = make_supply(max_voltage=5.0, max_current=1.0)
    supply.open()
    assert port.registers[0xD1] == pack_f32(5.0)
    assert port.registers[0xD2] == pack_f32(1.0)
    set_ovp = index_of(port, encode(CMD_SET, 0xD1, pack_f32(5.0)))
    set_ocp = index_of(port, encode(CMD_SET, 0xD2, pack_f32(1.0)))
    assert index_of(port, get_frame(0xD1)) > set_ovp
    assert index_of(port, get_frame(0xD2)) > set_ocp


def test_open_fails_when_the_device_refuses_the_ovp_write():
    supply, port, _ = make_supply()
    port.drop_sets.add(0xD1)
    with pytest.raises(ReadbackMismatch):
        supply.open()
    assert port.written[-1] == SESSION_STOP
    assert OUTPUT_ON not in port.written
    assert port.closed
    assert not supply.is_open


def test_open_fails_when_the_ocp_readback_gets_no_reply():
    supply, port, _ = make_supply()
    port.silent.add(0xD2)
    with pytest.raises(NoReply):
        supply.open()
    assert port.written[-1] == SESSION_STOP
    assert OUTPUT_ON not in port.written


def test_open_is_idempotent():
    supply, port, _ = make_supply()
    supply.open()
    count = len(port.written)
    supply.open()
    assert len(port.written) == count


def test_commands_are_spaced_at_least_50_ms_apart():
    supply, port, clock = make_supply()
    stamps = []
    original_write = port.write

    def stamped_write(data):
        stamps.append(clock.monotonic())
        return original_write(data)

    port.write = stamped_write
    supply.open()
    supply.set_voltage(3.3)
    gaps = [b - a for a, b in pairwise(stamps)]
    assert len(gaps) >= 6
    assert min(gaps) >= 0.05 - 1e-9


def test_get_retries_past_a_false_header_that_arrives_after_the_flush():
    supply, port, clock = make_supply()
    original_write = port.write

    def corrupt_before_first_reply(data):
        if data == get_frame(0xDE) and not writes_of(port, CMD_GET, 0xDE):
            port.push(b"\xf0\xa1\xc3\xff")
        return original_write(data)

    port.write = corrupt_before_first_reply
    supply.open()
    assert supply.model == "DPS-150"
    assert len(writes_of(port, CMD_GET, 0xDE)) == 2


def test_methods_refuse_before_open():
    supply, port, _ = make_supply()
    for call in (
        lambda: supply.set_voltage(1.0),
        lambda: supply.set_current(0.1),
        lambda: supply.output(True),
        lambda: supply.output(False),
        supply.measure,
        supply.status,
    ):
        with pytest.raises(Dps150Error):
            call()
    assert port.written == []


# Close


def test_close_stops_the_session_and_leaves_the_output_alone():
    supply, port, _ = make_supply()
    supply.open()
    supply.set_voltage(3.3)
    supply.set_current(0.5)
    supply.output(True)
    supply.close()
    assert port.written[-1] == SESSION_STOP
    assert port.registers[0xDB] == b"\x01"
    assert port.closed
    assert not supply.is_open


def test_context_manager_opens_and_closes():
    supply, port, _ = make_supply()
    with supply as s:
        assert s is supply
        assert supply.is_open
        assert port.written[0] == SESSION_START
    assert port.written[-1] == SESSION_STOP
    assert not supply.is_open


def test_close_is_idempotent():
    supply, port, _ = make_supply()
    supply.open()
    supply.close()
    count = len(port.written)
    supply.close()
    assert len(port.written) == count


# Setpoints


@pytest.mark.parametrize("volts", [5.01, 12.0, -0.1, math.nan, math.inf])
def test_set_voltage_rejects_setpoints_outside_the_limit(volts):
    supply, port, _ = make_supply(max_voltage=5.0)
    supply.open()
    with pytest.raises(ClampError):
        supply.set_voltage(volts)
    assert writes_of(port, CMD_SET, 0xC1) == []


@pytest.mark.parametrize("amps", [1.001, 3.0, -0.1, math.nan])
def test_set_current_rejects_setpoints_outside_the_limit(amps):
    supply, port, _ = make_supply(max_current=1.0)
    supply.open()
    with pytest.raises(ClampError):
        supply.set_current(amps)
    assert writes_of(port, CMD_SET, 0xC2) == []


def test_set_voltage_writes_then_reads_back_with_float32_rounding():
    supply, port, _ = make_supply()
    supply.open()
    supply.set_voltage(3.3)
    assert port.registers[0xC1] == pack_f32(3.3)
    assert index_of(port, get_frame(0xC1)) > index_of(port, encode(CMD_SET, 0xC1, pack_f32(3.3)))
    assert supply.status().voltage_setpoint == pytest.approx(3.3, abs=0.01)


def test_set_current_writes_then_reads_back_with_float32_rounding():
    supply, port, _ = make_supply()
    supply.open()
    supply.set_current(0.5)
    assert port.registers[0xC2] == pack_f32(0.5)
    assert index_of(port, get_frame(0xC2)) > index_of(port, encode(CMD_SET, 0xC2, pack_f32(0.5)))


def test_setpoint_at_the_limit_is_accepted():
    supply, port, _ = make_supply(max_voltage=5.0, max_current=1.0)
    supply.open()
    supply.set_voltage(5.0)
    supply.set_current(1.0)
    assert port.registers[0xC1] == pack_f32(5.0)
    assert port.registers[0xC2] == pack_f32(1.0)


def test_set_voltage_refuses_a_setpoint_above_the_supply_max_voltage_e2():
    supply, port, _ = make_supply(max_voltage=10.0)
    port.registers[0xE2] = pack_f32(4.79)
    supply.open()
    with pytest.raises(ClampError):
        supply.set_voltage(5.0)
    assert writes_of(port, CMD_SET, 0xC1) == []


def test_set_voltage_fails_when_the_readback_differs():
    supply, port, _ = make_supply()
    supply.open()
    port.drop_sets.add(0xC1)
    with pytest.raises(ReadbackMismatch):
        supply.set_voltage(3.3)


def test_set_current_fails_when_the_readback_gets_no_reply():
    supply, port, _ = make_supply()
    supply.open()
    port.silent.add(0xC2)
    with pytest.raises(NoReply):
        supply.set_current(0.5)


# Output


def test_output_on_verifies_protection_and_setpoints_before_writing_db_and_confirms_by_get():
    supply, port, _ = make_supply()
    supply.open()
    supply.set_voltage(3.3)
    supply.set_current(0.5)
    start = len(port.written)
    supply.output(True)
    assert port.registers[0xDB] == b"\x01"
    after = port.written[start:]
    on = after.index(OUTPUT_ON)
    for register in (0xD1, 0xD2, 0xC1, 0xC2, 0xE2):
        assert get_frame(register) in after[:on], f"0x{register:02X} not verified before DB=1"
    assert get_frame(0xDB) in after[on + 1 :]


@pytest.mark.parametrize("register, value", [(0xC1, 6.0), (0xC2, 1.5)])
def test_output_on_refuses_a_front_panel_setpoint_above_the_limit(register, value):
    supply, port, _ = make_supply(max_voltage=5.0, max_current=1.0)
    supply.open()
    port.registers[register] = pack_f32(value)
    with pytest.raises(ClampError):
        supply.output(True)
    assert OUTPUT_ON not in port.written
    assert port.registers[0xDB] == b"\x00"


def test_output_on_refuses_when_the_declared_setpoint_was_changed_behind_the_driver():
    supply, port, _ = make_supply()
    supply.open()
    supply.set_voltage(3.3)
    port.registers[0xC1] = pack_f32(4.0)
    with pytest.raises(ReadbackMismatch):
        supply.output(True)
    assert OUTPUT_ON not in port.written


def test_output_on_refuses_when_the_setpoint_exceeds_the_supply_max_voltage():
    supply, port, _ = make_supply()
    supply.open()
    supply.set_voltage(4.0)
    port.registers[0xE2] = pack_f32(3.5)
    with pytest.raises(ClampError):
        supply.output(True)
    assert OUTPUT_ON not in port.written


def test_output_on_refuses_when_ovp_drifted_from_the_limit():
    supply, port, _ = make_supply()
    supply.open()
    port.registers[0xD1] = pack_f32(30.0)
    with pytest.raises(ReadbackMismatch):
        supply.output(True)
    assert OUTPUT_ON not in port.written


def test_output_on_failure_after_the_write_forces_output_off_and_raises():
    supply, port, _ = make_supply()
    supply.open()
    port.silent.add(0xDB)
    with pytest.raises(NoReply):
        supply.output(True)
    db_writes = writes_of(port, CMD_SET, 0xDB)
    assert db_writes[0] == OUTPUT_ON
    assert db_writes[-1] == OUTPUT_OFF
    assert port.registers[0xDB] == b"\x00"


def test_output_on_fails_when_the_device_does_not_turn_on():
    supply, port, _ = make_supply()
    supply.open()
    port.drop_sets.add(0xDB)
    with pytest.raises(ReadbackMismatch):
        supply.output(True)
    assert writes_of(port, CMD_SET, 0xDB)[-1] == OUTPUT_OFF


def test_output_off_writes_db_0_and_confirms():
    supply, port, _ = make_supply()
    supply.open()
    port.registers[0xDB] = b"\x01"
    supply.output(False)
    assert port.registers[0xDB] == b"\x00"
    assert index_of(port, get_frame(0xDB)) > index_of(port, OUTPUT_OFF)


def test_output_off_fails_when_the_device_stays_on():
    supply, port, _ = make_supply()
    supply.open()
    port.registers[0xDB] = b"\x01"
    port.drop_sets.add(0xDB)
    with pytest.raises(ReadbackMismatch):
        supply.output(False)


# Measure and status


def test_measure_returns_voltage_current_power():
    supply, port, _ = make_supply()
    supply.open()
    port.telemetry = (3.3, 0.25, 0.825)
    assert supply.measure() == Measurement(
        pytest.approx(3.3), pytest.approx(0.25), pytest.approx(0.825)
    )


def test_status_reports_setpoints_output_protection_and_mode():
    supply, port, _ = make_supply()
    supply.open()
    supply.set_voltage(3.3)
    supply.set_current(0.5)
    port.registers[0xDB] = b"\x01"
    port.registers[0xDC] = b"\x05"
    port.registers[0xDD] = b"\x00"
    status = supply.status()
    assert status.voltage_setpoint == pytest.approx(3.3)
    assert status.current_setpoint == pytest.approx(0.5)
    assert status.output is True
    assert status.protection == 5
    assert status.protection_name == "LVP"
    assert status.mode == "CC"


def test_status_does_not_treat_nonzero_protection_as_output_off():
    supply, port, _ = make_supply()
    supply.open()
    port.registers[0xDB] = b"\x01"
    port.registers[0xDC] = b"\x05"
    assert supply.status().output is True


def test_status_names_an_unknown_protection_code():
    supply, port, _ = make_supply()
    supply.open()
    port.registers[0xDC] = b"\x09"
    assert supply.status().protection_name == "UNKNOWN(9)"


# Safety net


def test_allowlist_scan_catches_a_forbidden_frame_the_driver_might_swallow():
    port = MockSerial(clock=FakeClock())
    port.written.append(bytes([0xF1, 0xC0, 0x00, 0x00, 0x00]))
    with pytest.raises(AssertionError):
        assert_only_allowlisted_commands([port])
    port.written.clear()


# Fix-forward round 1 (PR #24 review): fail-closed read-backs, limit bound, echo, emergency off


NAN = pack_f32(math.nan)


def make_nan_supply(nan_registers):
    """A supply whose GET of any register in `nan_registers` (a live set) replies NaN."""
    supply, port, clock = make_supply(max_voltage=10.0, max_current=3.0)
    original_reply = port._reply

    def reply(register):
        if register in nan_registers:
            port.push(bytes([0xF0, 0xA1, register, 4, *NAN, (register + 4 + sum(NAN)) & 0xFF]))
        else:
            original_reply(register)

    port._reply = reply
    return supply, port


@pytest.mark.parametrize("register", [0xD1, 0xD2])
def test_open_rejects_a_nan_protection_readback(register):
    supply, port = make_nan_supply({register})
    with pytest.raises(ReadbackMismatch):
        supply.open()
    assert OUTPUT_ON not in port.written


@pytest.mark.parametrize("register", [0xC1, 0xC2, 0xD1, 0xD2, 0xE2])
def test_output_on_rejects_a_nan_readback(register):
    nan_registers = set()
    supply, port = make_nan_supply(nan_registers)
    supply.open()
    nan_registers.add(register)
    with pytest.raises(ReadbackMismatch):
        supply.output(True)
    assert OUTPUT_ON not in port.written
    assert port.registers[0xDB] == b"\x00"


def test_set_voltage_rejects_a_nan_e2_readback():
    supply, port = make_nan_supply({0xE2})
    supply.open()
    with pytest.raises(ReadbackMismatch):
        supply.set_voltage(5.0)
    assert writes_of(port, CMD_SET, 0xC1) == []


@pytest.mark.parametrize("ovp", [10.005, 10.01])
def test_open_rejects_ovp_readback_above_the_limit_within_tolerance(ovp):
    supply, port, _ = make_supply(max_voltage=10.0, max_current=3.0)
    port.drop_sets.add(0xD1)
    port.registers[0xD1] = pack_f32(ovp)
    with pytest.raises(ReadbackMismatch):
        supply.open()


@pytest.mark.parametrize("register, value", [(0xD1, 10.005), (0xD2, 3.0005)])
def test_output_on_rejects_protection_above_the_limit_within_tolerance(register, value):
    supply, port, _ = make_supply(max_voltage=10.0, max_current=3.0)
    supply.open()
    port.registers[register] = pack_f32(value)
    with pytest.raises(ReadbackMismatch):
        supply.output(True)
    assert OUTPUT_ON not in port.written


@pytest.mark.parametrize("register, value", [(0xC1, 10.005), (0xC2, 3.0005)])
def test_output_on_rejects_a_front_panel_setpoint_above_the_limit_within_tolerance(register, value):
    supply, port, _ = make_supply(max_voltage=10.0, max_current=3.0)
    supply.open()
    port.registers[register] = pack_f32(value)
    with pytest.raises(ClampError):
        supply.output(True)
    assert OUTPUT_ON not in port.written


def test_limit_that_float32_rounds_down_still_accepts_a_setpoint_at_the_limit():
    supply, port, _ = make_supply(max_voltage=3.3, max_current=0.7)
    supply.open()
    supply.set_voltage(3.3)
    supply.set_current(0.7)
    supply.output(True)
    assert port.registers[0xDB] == b"\x01"


def test_limit_that_float32_rounds_up_still_accepts_the_programmed_protection():
    supply, port, _ = make_supply(max_voltage=0.1, max_current=0.1)
    supply.open()
    supply.output(True)
    assert port.registers[0xDB] == b"\x01"


@pytest.mark.parametrize("register", [0xD1, 0xD2])
def test_open_fails_when_the_device_echoes_but_ignores_the_protection_write(register):
    supply, port, _ = make_supply(echo_sets=True)
    port.drop_sets.add(register)
    with pytest.raises(ReadbackMismatch):
        supply.open()
    assert OUTPUT_ON not in port.written


def test_set_voltage_fails_when_the_device_echoes_but_ignores_the_write():
    supply, port, _ = make_supply(echo_sets=True)
    supply.open()
    port.drop_sets.add(0xC1)
    with pytest.raises(ReadbackMismatch):
        supply.set_voltage(3.3)


def test_set_current_fails_when_the_device_echoes_but_ignores_the_write():
    supply, port, _ = make_supply(echo_sets=True)
    supply.open()
    port.drop_sets.add(0xC2)
    with pytest.raises(ReadbackMismatch):
        supply.set_current(0.5)


def test_output_on_fails_when_the_device_echoes_but_ignores_the_write():
    supply, port, _ = make_supply(echo_sets=True)
    supply.open()
    port.drop_sets.add(0xDB)
    with pytest.raises(ReadbackMismatch):
        supply.output(True)
    assert port.registers[0xDB] == b"\x00"


def test_get_flushes_input_right_before_each_send():
    supply, port, _ = make_supply()
    supply.open()
    stale = bytes([0xF0, 0xA1, 0xC1, 4, *pack_f32(9.0), (0xC1 + 4 + sum(pack_f32(9.0))) & 0xFF])
    port.push(stale)
    assert supply.status().voltage_setpoint == 0.0


# open() failure after session start: best-effort output off before session stop


def test_open_failure_attempts_output_off_before_session_stop():
    supply, port, _ = make_supply()
    port.drop_sets.add(0xD1)
    port.registers[0xDB] = b"\x01"
    with pytest.raises(ReadbackMismatch):
        supply.open()
    assert port.registers[0xDB] == b"\x00"
    assert index_of(port, OUTPUT_OFF) < index_of(port, SESSION_STOP)
    assert index_of(port, get_frame(0xDB)) > index_of(port, OUTPUT_OFF)
    assert port.written[-1] == SESSION_STOP
    assert port.closed


def test_open_failure_output_off_failure_does_not_mask_the_original_error():
    supply, port, _ = make_supply()
    port.drop_sets.update({0xD1, 0xDB})
    port.registers[0xDB] = b"\x01"
    with pytest.raises(ReadbackMismatch, match="OVP") as info:
        supply.open()
    assert any("output" in note for note in info.value.__notes__)
    assert port.written[-1] == SESSION_STOP
    assert port.closed
    assert not supply.is_open


def test_open_failure_session_stop_failure_does_not_mask_the_original_error():
    supply, port, _ = make_supply()
    port.silent.add(0xD2)
    original_write = port.write

    def failing_stop(data):
        if data == SESSION_STOP:
            raise OSError("port vanished")
        return original_write(data)

    port.write = failing_stop
    with pytest.raises(NoReply) as info:
        supply.open()
    assert any("OSError" in note for note in info.value.__notes__)
    assert port.closed
    assert not supply.is_open


# output(True) recovery precedence


def test_output_on_recovery_failure_does_not_mask_the_original_error():
    supply, port, _ = make_supply()
    supply.open()
    port.registers[0xC1] = pack_f32(50.0)
    port.registers[0xDB] = b"\x01"
    port.drop_sets.add(0xDB)
    with pytest.raises(ClampError) as info:
        supply.output(True)
    assert any("output" in note for note in info.value.__notes__)


def test_output_on_interrupted_between_write_and_confirm_still_turns_off():
    supply, port, _ = make_supply()
    supply.open()
    original_write = port.write
    interrupted = []

    def interrupt_once(data):
        if data == get_frame(0xDB) and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt
        return original_write(data)

    port.write = interrupt_once
    with pytest.raises(KeyboardInterrupt):
        supply.output(True)
    assert writes_of(port, CMD_SET, 0xDB) == [OUTPUT_ON, OUTPUT_OFF]
    assert port.registers[0xDB] == b"\x00"


# Input domain of the clamp and the limit


@pytest.mark.parametrize("bad", ["5", None, b"\x00", True, False, 1j])
def test_set_voltage_rejects_non_real_or_bool_with_clamp_error(bad):
    supply, port, _ = make_supply()
    supply.open()
    with pytest.raises(ClampError):
        supply.set_voltage(bad)
    with pytest.raises(ClampError):
        supply.set_current(bad)
    assert writes_of(port, CMD_SET, 0xC1) == []
    assert writes_of(port, CMD_SET, 0xC2) == []


@pytest.mark.parametrize("bad", ["5", True, b"\x00", 1j])
def test_limit_rejects_non_real_or_bool_with_limit_error(bad):
    with pytest.raises(LimitError):
        Dps150(PORT, max_voltage=bad, max_current=1.0)
    with pytest.raises(LimitError):
        Dps150(PORT, max_voltage=5.0, max_current=bad)


def test_negative_zero_setpoint_is_accepted():
    supply, port, _ = make_supply()
    supply.open()
    supply.set_voltage(-0.0)
    assert unpack_f32(port.registers[0xC1]) == 0.0


# USB serial number


def test_serial_number_comes_from_the_injected_usb_serial_lookup():
    supply, _, _ = make_supply()
    supply._usb_serial = lambda path: "ABC123"
    assert supply.serial_number == "ABC123"


def test_usb_serial_is_none_for_a_path_that_is_not_a_listed_port():
    from dps150.driver import usb_serial

    assert usb_serial("/dev/definitely-not-a-port") is None


# force_output_off


def force_output_off(*args, **kwargs):
    from dps150.driver import force_output_off

    return force_output_off(*args, **kwargs)


def make_force_off_port(**port_kwargs):
    clock = FakeClock()
    port = MockSerial(clock=clock, **port_kwargs)
    port.registers.update(FACTORY_REGISTERS)
    port.registers[0xDB] = b"\x01"
    return port, clock


def test_force_output_off_writes_db_0_confirms_and_stops_the_session():
    port, clock = make_force_off_port()
    force_output_off(PORT, serial_factory=lambda path: port, usb_id=dps150_usb_id, clock=clock)
    assert port.registers[0xDB] == b"\x00"
    assert port.written[0] == SESSION_START
    assert index_of(port, get_frame(0xDB)) > index_of(port, OUTPUT_OFF)
    assert port.written[-1] == SESSION_STOP
    assert port.closed
    registers_touched = {f[2] for f in port.written if f[1] in (CMD_SET, CMD_GET)}
    assert registers_touched == {0xDB}


def test_force_output_off_refuses_the_wrong_device_before_opening_the_port():
    opened = []
    with pytest.raises(WrongDevice):
        force_output_off(
            PORT, serial_factory=lambda path: opened.append(path), usb_id=lambda path: None
        )
    assert opened == []


def test_force_output_off_raises_when_the_device_stays_on():
    port, clock = make_force_off_port()
    port.drop_sets.add(0xDB)
    with pytest.raises(ReadbackMismatch):
        force_output_off(PORT, serial_factory=lambda path: port, usb_id=dps150_usb_id, clock=clock)
    assert port.written[-1] == SESSION_STOP
    assert port.closed


def test_force_output_off_raises_when_the_confirm_gets_no_reply():
    port, clock = make_force_off_port()
    port.silent.add(0xDB)
    with pytest.raises(NoReply):
        force_output_off(PORT, serial_factory=lambda path: port, usb_id=dps150_usb_id, clock=clock)
    assert port.written[-1] == SESSION_STOP


def test_force_output_off_ignores_a_set_echo_and_confirms_by_get():
    port, clock = make_force_off_port(echo_sets=True)
    port.drop_sets.add(0xDB)
    with pytest.raises(ReadbackMismatch):
        force_output_off(PORT, serial_factory=lambda path: port, usb_id=dps150_usb_id, clock=clock)


def test_force_output_off_is_exported_from_the_package():
    import dps150
    from dps150 import driver

    assert dps150.force_output_off is driver.force_output_off
    assert dps150.usb_serial is driver.usb_serial


def test_open_serial_bounds_a_wedged_write(monkeypatch):
    """A full CDC TX buffer on a hung device blocks write() forever without a write
    timeout, which would hang the watchdog mid-tick. pyserial then raises
    SerialTimeoutException, an OSError the session recovery already handles."""
    import serial

    opened = {}

    def fake_serial(path, **kwargs):
        opened.update(kwargs)
        return MockSerial()

    monkeypatch.setattr(serial, "Serial", fake_serial)
    open_serial("/dev/ttyACM0")
    assert opened["write_timeout"] == 1.0
    assert issubclass(serial.SerialTimeoutException, OSError)
