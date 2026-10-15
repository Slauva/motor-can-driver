import struct
import threading
import unittest
import uuid
from unittest.mock import Mock

import can

from can_motor.errors import LimitError, MotorFaultError, ProtocolError, SafetyError
from can_motor.models import CurrentScale, Health, MotorLimits, PidParameters
from can_motor.motor import Motor
from can_motor.protocol import Command
from can_motor.transport import CanTransport


def health_reply(*, temperature: int = 25, voltage: float = 24, flags: int = 0) -> bytes:
    return struct.pack("<BbxH2xB", Command.READ_HEALTH, temperature, round(voltage * 10), flags)


def feedback_reply(command: Command, *, temperature: int = 25, raw_current: int = 0, speed: int = 0) -> bytes:
    return struct.pack("<BbhhH", command, temperature, raw_current, speed, 1234)


class DeviceDouble:
    """A wire-level device responder; requests still pass through Motor's codec."""

    def __init__(self) -> None:
        self.transport = Mock(spec=CanTransport)
        self.transport.is_open = True
        self.transport.request.side_effect = self.reply
        self.overrides: dict[Command, bytes | BaseException] = {}

    def reply(self, identifier: int, payload: bytes, **options: object) -> bytes:
        command = Command(payload[0])
        if command in self.overrides:
            reply = self.overrides[command]
            if isinstance(reply, BaseException):
                raise reply
            return reply
        if command in (Command.READ_HEALTH, Command.CLEAR_ERRORS):
            return health_reply()
        if command in (Command.CURRENT, Command.SPEED, Command.POSITION, Command.READ_FEEDBACK):
            return feedback_reply(command)
        if command == Command.READ_ACCELERATION:
            return struct.pack("<B3xi", command, 100)
        return payload

    def commands(self) -> list[Command]:
        return [Command(call.args[1][0]) for call in self.transport.request.call_args_list]

    def reset_calls(self) -> None:
        self.transport.request.reset_mock()


class MotorTests(unittest.TestCase):
    def make_motor(self, *, limits: MotorLimits | None = None, node_id: int = 1) -> tuple[Motor, DeviceDouble]:
        device = DeviceDouble()
        motor = Motor(device.transport, node_id=node_id, current_scale=CurrentScale(16.5), limits=limits)
        return motor, device

    def make_enabled(self, *, limits: MotorLimits | None = None) -> tuple[Motor, DeviceDouble]:
        motor, device = self.make_motor(limits=limits)
        motor.enable()
        device.reset_calls()
        return motor, device

    def test_construction_has_no_io_and_sets_conservative_limits(self) -> None:
        motor, device = self.make_motor(node_id=3)
        device.transport.request.assert_not_called()
        self.assertFalse(motor.enabled)
        self.assertEqual(motor.node_id, 3)
        self.assertEqual(motor.limits.max_current_a, 1)
        self.assertEqual(motor.limits.max_speed_dps, 30)
        self.assertEqual(motor.limits.max_acceleration_dps2, 100)

    def test_enable_cancels_old_commands_checks_health_then_runs(self) -> None:
        motor, device = self.make_motor(node_id=3)
        motor.enable()
        self.assertTrue(motor.enabled)
        self.assertEqual(device.commands(), [Command.OFF, Command.READ_HEALTH, Command.RUN])
        for call in device.transport.request.call_args_list:
            self.assertEqual(call.args[0], 0x143)
            self.assertEqual(len(call.args[1]), 8)
            self.assertEqual(call.kwargs["timeout"], 0.5)
            self.assertEqual(call.kwargs["response_commands"], (call.args[1][0],))
        self.assertNotIn(Command.CLEAR_ERRORS, device.commands())
        self.assertTrue(all(int(command) not in (0x32, 0x19, 0x91) for command in device.commands()))

    def test_enable_refuses_fault_without_run_or_error_clear(self) -> None:
        motor, device = self.make_motor()
        device.overrides[Command.READ_HEALTH] = health_reply(flags=0x08)
        with self.assertRaises(MotorFaultError):
            motor.enable()
        self.assertFalse(motor.enabled)
        self.assertEqual(device.commands(), [Command.OFF, Command.READ_HEALTH, Command.OFF])

    def test_all_motion_modes_require_enable_without_io(self) -> None:
        motor, device = self.make_motor(limits=MotorLimits(firmware_current_limit_a=0.5))
        for action in (lambda: motor.set_current(0.1), lambda: motor.set_speed(1), lambda: motor.set_position(10)):
            with self.subTest(action=action), self.assertRaises(SafetyError):
                action()
        device.transport.request.assert_not_called()

    def test_every_motion_has_fresh_health_and_closed_loop_acceleration(self) -> None:
        motor, device = self.make_enabled(limits=MotorLimits(firmware_current_limit_a=0.5))
        for action, expected in (
            (lambda: motor.set_current(0.1), [Command.READ_HEALTH, Command.CURRENT]),
            (lambda: motor.set_current(-0.1), [Command.READ_HEALTH, Command.CURRENT]),
            (lambda: motor.set_speed(1), [Command.READ_HEALTH, Command.READ_ACCELERATION, Command.SPEED]),
            (lambda: motor.set_position(10), [Command.READ_HEALTH, Command.READ_ACCELERATION, Command.POSITION]),
        ):
            with self.subTest(commands=expected):
                device.reset_calls()
                action()
                self.assertEqual(device.commands(), expected)
                self.assertTrue(motor.enabled)

    def test_current_speed_and_position_payloads_have_exact_units_and_sign(self) -> None:
        motor, device = self.make_enabled(limits=MotorLimits(firmware_current_limit_a=0.5))
        motor.set_current(-0.5)
        self.assertEqual(device.transport.request.call_args.args[1], b"\xa1\x00\x00\x00\xc2\xff\x00\x00")
        motor.set_speed(-12.345)
        self.assertEqual(device.transport.request.call_args.args[1], b"\xa2\x00\x00\x00\x2e\xfb\xff\xff")
        motor.set_position(-45.679, max_speed_dps=12.9)
        self.assertEqual(device.transport.request.call_args.args[1], b"\xa4\x00\x0c\x00\x29\xee\xff\xff")

    def test_limit_rejections_send_no_frames_and_leave_enabled(self) -> None:
        motor, device = self.make_enabled(limits=MotorLimits(firmware_current_limit_a=0.5))
        actions = (
            lambda: motor.set_current(1.001), lambda: motor.set_current(-1.001),
            lambda: motor.set_speed(30.001), lambda: motor.set_speed(-30.001),
            lambda: motor.set_position(180.001), lambda: motor.set_position(-180.001),
            lambda: motor.set_position(0, max_speed_dps=30.001),
            lambda: motor.set_position(0, max_speed_dps=0.99),
        )
        for action in actions:
            with self.subTest(action=action), self.assertRaises(LimitError):
                action()
        device.transport.request.assert_not_called()
        self.assertTrue(motor.enabled)

    def test_nonnumeric_and_nonfinite_motion_never_reaches_bus(self) -> None:
        motor, device = self.make_enabled(limits=MotorLimits(firmware_current_limit_a=0.5))
        for method in (motor.set_current, motor.set_speed, motor.set_position):
            for value in (float("nan"), float("inf"), -float("inf"), True, "1"):
                with self.subTest(method=method.__name__, value=value), self.assertRaises(ValueError):
                    method(value)
        device.transport.request.assert_not_called()

    def test_quantized_position_must_stay_inside_configured_target_limits(self) -> None:
        motor, device = self.make_enabled(limits=MotorLimits(
            min_position_deg=0.001, max_position_deg=0.009, firmware_current_limit_a=0.5,
        ))
        with self.assertRaises(LimitError):
            motor.set_position(0.005)
        device.transport.request.assert_not_called()

    def test_closed_loop_requires_declared_firmware_current_limit_before_io(self) -> None:
        motor, device = self.make_enabled()
        for action in (lambda: motor.set_speed(1), lambda: motor.set_position(10)):
            with self.subTest(action=action), self.assertRaises(SafetyError):
                action()
        device.transport.request.assert_not_called()
        motor.set_current(0.1)
        self.assertEqual(device.commands(), [Command.READ_HEALTH, Command.CURRENT])

    def test_declared_firmware_cap_must_not_exceed_host_current_limit(self) -> None:
        with self.assertRaises(ValueError):
            MotorLimits(max_current_a=1, firmware_current_limit_a=1.01)
        motor, _ = self.make_motor(limits=MotorLimits(max_current_a=1, firmware_current_limit_a=1))
        self.assertEqual(motor.limits.firmware_current_limit_a, 1)

    def test_current_host_limit_must_fit_selected_device_scale(self) -> None:
        device = DeviceDouble()
        with self.assertRaises(ValueError):
            Motor(device.transport, current_scale=CurrentScale(0.5), limits=MotorLimits(max_current_a=1))
        device.transport.request.assert_not_called()

    def test_bad_acceleration_suppresses_closed_loop_motion_and_disables(self) -> None:
        for acceleration in (-1, 0, 101):
            for mode in ("speed", "position"):
                with self.subTest(acceleration=acceleration, mode=mode):
                    motor, device = self.make_enabled(limits=MotorLimits(firmware_current_limit_a=0.5))
                    device.overrides[Command.READ_ACCELERATION] = struct.pack("<B3xi", Command.READ_ACCELERATION, acceleration)
                    with self.assertRaises(MotorFaultError):
                        (motor.set_speed if mode == "speed" else motor.set_position)(1)
                    self.assertEqual(device.commands(), [Command.READ_HEALTH, Command.READ_ACCELERATION, Command.OFF])
                    self.assertFalse(motor.enabled)

    def test_health_fault_masks_include_unknown_bits(self) -> None:
        undervoltage = Health(25, 24, 0x01)
        overtemperature = Health(25, 24, 0x08)
        self.assertTrue(undervoltage.undervoltage)
        self.assertFalse(undervoltage.overtemperature)
        self.assertTrue(overtemperature.overtemperature)
        self.assertFalse(overtemperature.undervoltage)
        for flags in (0x01, 0x08, 0x09, 0x02, 0x80):
            with self.subTest(flags=flags):
                motor, device = self.make_enabled()
                device.overrides[Command.READ_HEALTH] = health_reply(flags=flags)
                with self.assertRaises(MotorFaultError):
                    motor.set_current(0.1)
                self.assertEqual(device.commands(), [Command.READ_HEALTH, Command.OFF])
                self.assertFalse(motor.enabled)

    def test_host_temperature_and_voltage_preflight_limits_suppress_motion(self) -> None:
        cases = (
            (MotorLimits(), health_reply(temperature=60)),
            (MotorLimits(min_voltage_v=20), health_reply(voltage=19.9)),
            (MotorLimits(max_voltage_v=30), health_reply(voltage=30.1)),
        )
        for limits, reply in cases:
            with self.subTest(limits=limits):
                motor, device = self.make_enabled(limits=limits)
                device.overrides[Command.READ_HEALTH] = reply
                with self.assertRaises(MotorFaultError):
                    motor.set_current(0.1)
                self.assertEqual(device.commands(), [Command.READ_HEALTH, Command.OFF])
                self.assertFalse(motor.enabled)

    def test_motion_feedback_limits_disable_after_the_response(self) -> None:
        for fields in ({"temperature": 60}, {"raw_current": 125}, {"speed": 31}, {"speed": -31}):
            with self.subTest(fields=fields):
                motor, device = self.make_enabled()
                device.overrides[Command.CURRENT] = feedback_reply(Command.CURRENT, **fields)
                with self.assertRaises(MotorFaultError):
                    motor.set_current(0.1)
                self.assertEqual(device.commands(), [Command.READ_HEALTH, Command.CURRENT, Command.OFF])
                self.assertFalse(motor.enabled)

    def test_motion_timeout_attempts_off_without_retry_and_revokes_enable(self) -> None:
        motor, device = self.make_enabled()
        original = can.CanTimeoutError("reply missing")
        device.overrides[Command.CURRENT] = original
        with self.assertRaises(can.CanTimeoutError) as raised:
            motor.set_current(0.1)
        self.assertIs(raised.exception, original)
        self.assertEqual(device.commands(), [Command.READ_HEALTH, Command.CURRENT, Command.OFF])
        self.assertFalse(motor.enabled)
        device.reset_calls()
        with self.assertRaises(SafetyError):
            motor.set_current(0)
        device.transport.request.assert_not_called()

    def test_motion_error_is_preserved_if_best_effort_off_also_fails(self) -> None:
        motor, device = self.make_enabled()
        original = can.CanTimeoutError("original motion timeout")
        device.overrides.update({Command.CURRENT: original, Command.OFF: can.CanTimeoutError("off timeout")})
        with self.assertLogs("can_motor.motor", level="WARNING"):
            with self.assertRaises(can.CanTimeoutError) as raised:
                motor.set_current(0.1)
        self.assertIs(raised.exception, original)
        self.assertFalse(motor.enabled)

    def test_disconnected_transport_prevents_futile_off_request(self) -> None:
        motor, device = self.make_enabled()
        device.overrides[Command.CURRENT] = can.CanOperationError("USB removed")

        def disconnect_during_motion(identifier: int, payload: bytes, **options: object) -> bytes:
            if payload[0] == Command.CURRENT:
                device.transport.is_open = False
            return device.reply(identifier, payload, **options)

        device.transport.request.side_effect = disconnect_during_motion
        with self.assertRaises(can.CanOperationError):
            motor.set_current(0.1)
        self.assertEqual(device.commands(), [Command.READ_HEALTH, Command.CURRENT])
        self.assertFalse(motor.enabled)

    def test_stop_disarms_and_requires_explicit_reenable(self) -> None:
        motor, device = self.make_enabled()
        motor.stop()
        self.assertEqual(device.commands(), [Command.STOP])
        self.assertFalse(motor.enabled)
        device.reset_calls()
        with self.assertRaises(SafetyError):
            motor.set_current(0)
        device.transport.request.assert_not_called()
        motor.enable()
        self.assertEqual(device.commands(), [Command.OFF, Command.READ_HEALTH, Command.RUN])

    def test_disable_transmits_off_even_when_already_logically_disabled(self) -> None:
        motor, device = self.make_motor()
        motor.disable()
        motor.disable()
        self.assertEqual(device.commands(), [Command.OFF, Command.OFF])
        self.assertFalse(motor.enabled)

    def test_control_always_disables_after_success_and_keyboard_interrupt(self) -> None:
        for error in (None, KeyboardInterrupt()):
            with self.subTest(error=error):
                motor, device = self.make_motor()
                if error is None:
                    with motor.control() as controlled:
                        self.assertIs(controlled, motor)
                        self.assertTrue(motor.enabled)
                else:
                    with self.assertRaises(KeyboardInterrupt) as raised:
                        with motor.control():
                            raise error
                    self.assertIs(raised.exception, error)
                self.assertEqual(device.commands(), [Command.OFF, Command.READ_HEALTH, Command.RUN, Command.OFF])
                self.assertFalse(motor.enabled)

    def test_control_attempts_off_even_when_initial_enable_fails(self) -> None:
        motor, device = self.make_motor()
        original = can.CanTimeoutError("run did not acknowledge")
        device.overrides[Command.RUN] = original
        with self.assertRaises(can.CanTimeoutError) as raised:
            with motor.control():
                self.fail("Body must not execute after failed enable")
        self.assertIs(raised.exception, original)
        self.assertEqual(device.commands()[:3], [Command.OFF, Command.READ_HEALTH, Command.RUN])
        self.assertEqual(device.commands()[-1], Command.OFF)
        self.assertGreaterEqual(device.commands().count(Command.OFF), 2)
        self.assertFalse(motor.enabled)

    def test_control_preserves_body_exception_when_off_fails(self) -> None:
        motor, device = self.make_motor()
        original = ValueError("test body failed")
        with self.assertLogs("can_motor.motor", level="WARNING"):
            with self.assertRaises(ValueError) as raised:
                with motor.control():
                    device.overrides[Command.OFF] = can.CanTimeoutError("off timeout")
                    raise original
        self.assertIs(raised.exception, original)
        self.assertFalse(motor.enabled)

    def test_parameter_changes_and_explicit_error_clear_require_disarmed_motor(self) -> None:
        motor, device = self.make_enabled()
        pid = PidParameters(1, 2, 3, 4, 5, 6)
        for action in (lambda: motor.write_pid_ram(pid), lambda: motor.write_acceleration_ram(50), motor.clear_errors):
            with self.subTest(action=action), self.assertRaises(SafetyError):
                action()
        device.transport.request.assert_not_called()
        motor.disable()
        device.reset_calls()
        self.assertEqual(motor.write_pid_ram(pid), pid)
        self.assertEqual(device.transport.request.call_args.args[1], b"\x31\x00\x01\x02\x03\x04\x05\x06")
        self.assertEqual(motor.write_acceleration_ram(50), 50)
        self.assertEqual(device.transport.request.call_args.args[1], b"\x34\x00\x00\x00\x32\x00\x00\x00")
        self.assertEqual(motor.clear_errors().error_flags, 0)
        self.assertEqual(device.transport.request.call_args.kwargs["response_commands"], (0x9A, 0x9B))
        self.assertEqual(device.commands(), [
            Command.OFF, Command.WRITE_PID_RAM,
            Command.OFF, Command.WRITE_ACCELERATION_RAM,
            Command.OFF, Command.CLEAR_ERRORS,
        ])
        self.assertFalse(motor.enabled)

    def test_parameter_write_or_clear_is_blocked_when_physical_off_is_unconfirmed(self) -> None:
        for operation in ("pid", "acceleration", "clear_errors"):
            with self.subTest(operation=operation):
                motor, device = self.make_motor()
                original = can.CanTimeoutError("OFF not acknowledged")
                device.overrides[Command.OFF] = original
                with self.assertRaises(can.CanTimeoutError) as raised:
                    if operation == "pid":
                        motor.write_pid_ram(PidParameters(1, 2, 3, 4, 5, 6))
                    elif operation == "acceleration":
                        motor.write_acceleration_ram(50)
                    else:
                        motor.clear_errors()
                self.assertIs(raised.exception, original)
                self.assertEqual(device.commands(), [Command.OFF])
                self.assertFalse(motor.enabled)

    def test_phase_current_read_trips_temperature_only_while_enabled(self) -> None:
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                motor, device = self.make_enabled() if enabled else self.make_motor()
                device.overrides[Command.READ_PHASE_CURRENTS] = struct.pack(
                    "<Bbhhh", Command.READ_PHASE_CURRENTS, 60, 0, 0, 0,
                )
                if enabled:
                    with self.assertRaises(MotorFaultError):
                        motor.read_phase_currents()
                    self.assertEqual(device.commands(), [Command.READ_PHASE_CURRENTS, Command.OFF])
                else:
                    self.assertEqual(motor.read_phase_currents().temperature_c, 60)
                    self.assertEqual(device.commands(), [Command.READ_PHASE_CURRENTS])
                self.assertFalse(motor.enabled)

    def test_failed_stop_attempts_off_and_preserves_original_error(self) -> None:
        motor, device = self.make_enabled()
        original = can.CanTimeoutError("STOP not acknowledged")
        device.overrides[Command.STOP] = original
        with self.assertRaises(can.CanTimeoutError) as raised:
            motor.stop()
        self.assertIs(raised.exception, original)
        self.assertEqual(device.commands(), [Command.STOP, Command.OFF])
        self.assertFalse(motor.enabled)

    def test_acceleration_write_rejects_host_limit_before_io(self) -> None:
        motor, device = self.make_motor()
        with self.assertRaises(LimitError):
            motor.write_acceleration_ram(101)
        device.transport.request.assert_not_called()

    def test_invalid_pid_object_is_rejected_before_any_io(self) -> None:
        motor, device = self.make_motor()
        with self.assertRaises(TypeError):
            motor.write_pid_ram(None)
        device.transport.request.assert_not_called()

    def test_parameter_acknowledgement_must_echo_the_exact_request(self) -> None:
        motor, device = self.make_motor()
        device.overrides[Command.WRITE_ACCELERATION_RAM] = b"\x34" + bytes(7)
        with self.assertRaises(ProtocolError):
            motor.write_acceleration_ram(50)
        self.assertFalse(motor.enabled)

    def test_active_read_decoder_failure_attempts_off_once(self) -> None:
        for reader, command, malformed in (
            ("read_feedback", Command.READ_FEEDBACK, feedback_reply(Command.READ_FEEDBACK, raw_current=2049)),
            ("read_health", Command.READ_HEALTH, b"\x9a"),
            ("read_encoder", Command.READ_ENCODER, b"\x90"),
            ("read_pid", Command.READ_PID, b"\x30"),
            ("read_acceleration", Command.READ_ACCELERATION, b"\x33"),
            ("read_phase_currents", Command.READ_PHASE_CURRENTS, b"\x9d"),
            ("read_angle", Command.READ_ANGLE, b"\x92"),
        ):
            with self.subTest(reader=reader):
                motor, device = self.make_enabled()
                device.overrides[command] = malformed
                with self.assertRaises(ProtocolError):
                    getattr(motor, reader)()
                self.assertEqual(device.commands(), [command, Command.OFF])
                self.assertFalse(motor.enabled)

    def test_disarmed_read_failures_do_not_send_state_changing_commands(self) -> None:
        for reader, command in (
            ("read_health", Command.READ_HEALTH), ("read_feedback", Command.READ_FEEDBACK),
            ("read_angle", Command.READ_ANGLE), ("read_encoder", Command.READ_ENCODER),
            ("read_pid", Command.READ_PID), ("read_acceleration", Command.READ_ACCELERATION),
            ("read_phase_currents", Command.READ_PHASE_CURRENTS),
        ):
            for failure in (ProtocolError("malformed reply"), can.CanTimeoutError("reply missing")):
                with self.subTest(reader=reader, failure=type(failure).__name__):
                    motor, device = self.make_motor()
                    device.overrides[command] = failure
                    with self.assertRaises(type(failure)) as raised:
                        getattr(motor, reader)()
                    self.assertIs(raised.exception, failure)
                    self.assertEqual(device.commands(), [command])
                    self.assertFalse(motor.enabled)


class MotorVirtualIntegrationTests(unittest.TestCase):
    def test_complete_lifecycle_through_real_virtual_can_bus(self) -> None:
        channel = f"motor-integration-{uuid.uuid4()}"
        peer = can.Bus(interface="virtual", channel=channel, ignore_config=True)
        self.addCleanup(peer.shutdown)
        transport = CanTransport.virtual(channel)
        self.addCleanup(transport.close)
        stop = threading.Event()
        commands: list[Command] = []
        failures: list[BaseException] = []
        device = DeviceDouble()

        def respond() -> None:
            try:
                while not stop.is_set():
                    request = peer.recv(0.05)
                    if request is None:
                        continue
                    self.assertEqual(request.arbitration_id, 0x142)
                    self.assertFalse(request.is_extended_id)
                    commands.append(Command(request.data[0]))
                    reply = device.reply(request.arbitration_id, bytes(request.data))
                    peer.send(can.Message(arbitration_id=request.arbitration_id, is_extended_id=False, data=reply))
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=respond, daemon=True)
        worker.start()
        try:
            motor = Motor(
                transport, node_id=2, current_scale=CurrentScale(16.5),
                limits=MotorLimits(firmware_current_limit_a=0.5),
            )
            with motor.control():
                self.assertEqual(motor.set_current(0.25).encoder_raw, 1234)
                motor.set_speed(-5)
                motor.set_position(10, max_speed_dps=3)
                self.assertEqual(motor.read_angle(), 0)
            self.assertFalse(motor.enabled)
        finally:
            stop.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(commands, [
            Command.OFF, Command.READ_HEALTH, Command.RUN,
            Command.READ_HEALTH, Command.CURRENT,
            Command.READ_HEALTH, Command.READ_ACCELERATION, Command.SPEED,
            Command.READ_HEALTH, Command.READ_ACCELERATION, Command.POSITION,
            Command.READ_ANGLE, Command.OFF,
        ])


if __name__ == "__main__":
    unittest.main()
