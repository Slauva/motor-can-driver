"""RPM controls share the degree/second safety checks and wire format."""

import contextlib
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Inexact, localcontext
import io
import struct
import unittest
from unittest.mock import MagicMock, Mock, patch

from can_motor import dps_to_rpm, rpm_to_dps
from can_motor.errors import LimitError, MotorFaultError, SafetyError
from can_motor.models import CurrentScale, Feedback, MotorLimits
from can_motor.motor import Motor
from can_motor import protocol
from can_motor.protocol import Command
from can_motor.transport import CanTransport
from examples import motor_test


class RpmDevice:
    def __init__(self) -> None:
        self.transport = Mock(spec=CanTransport)
        self.transport.is_open = True
        self.transport.request.side_effect = self.reply
        self.feedback_speed_dps = 0
        self.acceleration = 100

    def reply(self, identifier: int, payload: bytes, **options: object) -> bytes:
        command = Command(payload[0])
        if command == Command.READ_HEALTH:
            return struct.pack("<BbxH2xB", command, 25, 240, 0)
        if command == Command.READ_ACCELERATION:
            return struct.pack("<B3xi", command, self.acceleration)
        if command in (Command.SPEED, Command.POSITION, Command.READ_FEEDBACK):
            return struct.pack("<BbhhH", command, 25, 0, self.feedback_speed_dps, 1234)
        return payload

    def commands(self) -> list[Command]:
        return [Command(call.args[1][0]) for call in self.transport.request.call_args_list]


class RpmUnitTests(unittest.TestCase):
    def test_conversions_preserve_sign_and_fractional_speed(self) -> None:
        for rpm, dps in ((0, 0), (1, 6), (-2, -12), (0.125, 0.75), (5, 30)):
            with self.subTest(rpm=rpm):
                self.assertEqual(rpm_to_dps(rpm), dps)
                self.assertEqual(dps_to_rpm(dps), rpm)

    def test_conversions_are_exported_from_units_and_package(self) -> None:
        from can_motor.units import dps_to_rpm as units_dps_to_rpm
        from can_motor.units import rpm_to_dps as units_rpm_to_dps

        self.assertIs(rpm_to_dps, units_rpm_to_dps)
        self.assertIs(dps_to_rpm, units_dps_to_rpm)

    def test_conversions_reject_invalid_numbers(self) -> None:
        for converter in (rpm_to_dps, dps_to_rpm):
            for value in (True, False, "1", None, float("nan"), float("inf"), -float("inf"), 10**1000):
                with self.subTest(converter=converter.__name__, value=value), self.assertRaises(ValueError):
                    converter(value)

    def test_conversion_cannot_produce_infinite_speed(self) -> None:
        with self.assertRaises(ValueError):
            rpm_to_dps(1e308)

    def test_conversion_does_not_depend_on_callers_decimal_context(self) -> None:
        with localcontext() as context:
            context.prec = 1
            context.traps[Inexact] = True
            self.assertEqual(rpm_to_dps(1.9), 11.4)

    def test_feedback_rpm_preserves_signed_wire_speed(self) -> None:
        for speed_dps in (-32768, -12, 0, 7, 32767):
            with self.subTest(speed_dps=speed_dps):
                feedback = Feedback(25, 0.0, speed_dps, 1234)
                self.assertEqual(feedback.speed_rpm, speed_dps / 6)


class RpmLimitTests(unittest.TestCase):
    def test_default_and_rpm_configuration_have_one_canonical_limit(self) -> None:
        default = MotorLimits()
        rpm = MotorLimits(max_speed_rpm=5)
        dps = MotorLimits(max_speed_dps=30)
        self.assertEqual(default.max_speed_rpm, 5)
        self.assertEqual(rpm.max_speed_dps, 30)
        self.assertEqual(default, rpm)
        self.assertEqual(rpm, dps)
        self.assertNotIn("max_speed_rpm", {field.name for field in fields(rpm)})

    def test_rpm_limit_is_immutable(self) -> None:
        limits = MotorLimits(max_speed_rpm=2)
        with self.assertRaises((AttributeError, FrozenInstanceError, TypeError)):
            limits.max_speed_rpm = 10
        self.assertEqual(limits.max_speed_dps, 12)

    def test_existing_positional_constructor_is_preserved(self) -> None:
        limits = MotorLimits(2, 12, -90, 90, 200, 55, 20, 30, 1)
        self.assertEqual(limits.max_current_a, 2)
        self.assertEqual(limits.max_speed_rpm, 2)
        self.assertEqual(limits.min_position_deg, -90)
        self.assertEqual(limits.max_position_deg, 90)
        self.assertEqual(limits.max_acceleration_dps2, 200)
        self.assertEqual(limits.max_temperature_c, 55)
        self.assertEqual(limits.min_voltage_v, 20)
        self.assertEqual(limits.max_voltage_v, 30)
        self.assertEqual(limits.firmware_current_limit_a, 1)

    def test_dataclass_replace_keeps_rpm_limit(self) -> None:
        original = MotorLimits(max_speed_rpm=2)
        changed = replace(original, max_current_a=0.5)
        self.assertEqual(changed.max_speed_dps, 12)
        self.assertEqual(changed.max_speed_rpm, 2)
        self.assertEqual(changed.max_current_a, 0.5)
        self.assertEqual(original.max_current_a, 1)

    def test_specifying_both_units_is_rejected_even_when_equal(self) -> None:
        for dps, rpm in ((30, 5), (12, 2), (30, 2)):
            with self.subTest(dps=dps, rpm=rpm), self.assertRaises(ValueError):
                MotorLimits(max_speed_dps=dps, max_speed_rpm=rpm)

    def test_rpm_limits_must_be_positive_finite_numbers(self) -> None:
        for value in (0, -1, True, False, "1", float("nan"), float("inf"), -float("inf"), 10**1000):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MotorLimits(max_speed_rpm=value)

    def test_rpm_limits_fit_the_position_commands_uint16_dps_field(self) -> None:
        largest = MotorLimits(max_speed_rpm=65535 / 6)
        self.assertEqual(largest.max_speed_dps, 65535)
        with self.assertRaises(ValueError):
            MotorLimits(max_speed_rpm=65536 / 6)

    def test_rpm_limit_is_not_rounded_by_callers_decimal_context(self) -> None:
        with localcontext() as context:
            context.prec = 1
            context.traps[Inexact] = True
            self.assertEqual(MotorLimits(max_speed_rpm=5.9).max_speed_dps, 35.4)


class RpmMotorTests(unittest.TestCase):
    def make_motor(self, *, enabled: bool = True, declared_current: bool = True) -> tuple[Motor, RpmDevice]:
        device = RpmDevice()
        limits = MotorLimits(max_speed_rpm=5, firmware_current_limit_a=0.5 if declared_current else None)
        motor = Motor(device.transport, current_scale=CurrentScale(16.5), limits=limits)
        if enabled:
            motor.enable()
        device.transport.request.reset_mock()
        return motor, device

    def test_speed_rpm_sends_exact_signed_dps_command_and_returns_feedback(self) -> None:
        motor, device = self.make_motor()
        device.feedback_speed_dps = -12
        feedback = motor.set_speed_rpm(-2)
        self.assertEqual(device.commands(), [Command.READ_HEALTH, Command.READ_ACCELERATION, Command.SPEED])
        self.assertEqual(device.transport.request.call_args.args[1], struct.pack("<B3xi", Command.SPEED, -1200))
        self.assertEqual(feedback.speed_rpm, -2)

    def test_wire_commands_ignore_and_preserve_callers_decimal_context(self) -> None:
        motor, device = self.make_motor()
        with localcontext() as context:
            context.prec = 1
            context.Emax = 1
            context.Emin = -1
            context.traps[Inexact] = True
            original_flags = context.flags.copy()
            motor.set_speed_rpm(1.9)
            self.assertEqual(device.transport.request.call_args.args[1], struct.pack("<B3xi", Command.SPEED, 1140))
            motor.set_position(-45.679, max_speed_rpm=2.15)
            self.assertEqual(device.transport.request.call_args.args[1], struct.pack("<BBHi", Command.POSITION, 0, 12, -4567))
            self.assertEqual(protocol.current_command(-0.5, CurrentScale(16.5)), b"\xa1\x00\x00\x00\xc2\xff\x00\x00")
            self.assertEqual(context.prec, 1)
            self.assertEqual(context.Emax, 1)
            self.assertEqual(context.Emin, -1)
            self.assertTrue(context.traps[Inexact])
            self.assertEqual(context.flags, original_flags)

    def test_limit_is_enforced_for_both_command_units(self) -> None:
        motor, device = self.make_motor()
        for action in (
            lambda: motor.set_speed_rpm(5.001),
            lambda: motor.set_speed_rpm(-5.001),
            lambda: motor.set_speed(30.001),
            lambda: motor.set_speed(-30.001),
            lambda: motor.set_position(0, max_speed_rpm=5.001),
            lambda: motor.set_position(0, max_speed_dps=30.001),
        ):
            with self.subTest(action=action), self.assertRaises(LimitError):
                action()
        device.transport.request.assert_not_called()
        self.assertTrue(motor.enabled)

    def test_invalid_rpm_commands_never_reach_can(self) -> None:
        motor, device = self.make_motor()
        for value in (True, False, "1", float("nan"), float("inf"), -float("inf"), 10**1000):
            for action in (
                lambda: motor.set_speed_rpm(value),
                lambda: motor.set_position(0, max_speed_rpm=value),
            ):
                with self.subTest(value=value, action=action), self.assertRaises(ValueError):
                    action()
        device.transport.request.assert_not_called()

    def test_speed_rpm_requires_enable_and_declared_firmware_current_limit(self) -> None:
        for options in ({"enabled": False}, {"declared_current": False}):
            with self.subTest(options=options):
                motor, device = self.make_motor(**options)
                with self.assertRaises(SafetyError):
                    motor.set_speed_rpm(1)
                device.transport.request.assert_not_called()

    def test_speed_rpm_checks_acceleration_and_disables_on_fault(self) -> None:
        motor, device = self.make_motor()
        device.acceleration = 101
        with self.assertRaises(MotorFaultError):
            motor.set_speed_rpm(1)
        self.assertEqual(device.commands(), [Command.READ_HEALTH, Command.READ_ACCELERATION, Command.OFF])
        self.assertFalse(motor.enabled)

    def test_rpm_limit_also_applies_to_measured_dps_feedback(self) -> None:
        motor, device = self.make_motor()
        device.feedback_speed_dps = -31
        with self.assertRaises(MotorFaultError):
            motor.set_speed_rpm(1)
        self.assertEqual(device.commands(), [Command.READ_HEALTH, Command.READ_ACCELERATION, Command.SPEED, Command.OFF])
        self.assertFalse(motor.enabled)

    def test_position_rpm_encodes_whole_degrees_per_second(self) -> None:
        motor, device = self.make_motor()
        motor.set_position(-45, max_speed_rpm=2)
        self.assertEqual(device.transport.request.call_args.args[1], struct.pack("<BBHi", Command.POSITION, 0, 12, -4500))
        motor.set_position(45, max_speed_rpm=2.15)
        self.assertEqual(device.transport.request.call_args.args[1], struct.pack("<BBHi", Command.POSITION, 0, 12, 4500))

    def test_position_default_uses_configured_rpm_limit(self) -> None:
        motor, device = self.make_motor()
        motor.set_position(0)
        self.assertEqual(device.transport.request.call_args.args[1], struct.pack("<BBHi", Command.POSITION, 0, 30, 0))

    def test_position_rejects_both_speed_units_without_can_io(self) -> None:
        motor, device = self.make_motor()
        with self.assertRaises(ValueError):
            motor.set_position(0, max_speed_dps=12, max_speed_rpm=2)
        device.transport.request.assert_not_called()

    def test_position_rpm_cannot_quantize_to_zero_speed(self) -> None:
        motor, device = self.make_motor()
        for rpm in (0, -1, 0.1):
            with self.subTest(rpm=rpm), self.assertRaises(LimitError):
                motor.set_position(0, max_speed_rpm=rpm)
        device.transport.request.assert_not_called()
        motor.set_position(0, max_speed_rpm=1 / 6)
        self.assertEqual(device.transport.request.call_args.args[1], struct.pack("<BBHi", Command.POSITION, 0, 1, 0))


class RpmCliTests(unittest.TestCase):
    def test_cli_passes_normalized_speed_limit_to_motor(self) -> None:
        for interface in ("socketcan", "gs_usb"):
            for option, value, expected_dps in ((None, None, 30), ("--max-speed-rpm", "2.5", 15), ("--max-speed-dps", "12", 12)):
                with self.subTest(interface=interface, option=option):
                    transport = MagicMock(spec=CanTransport)
                    transport.__enter__.return_value = transport
                    motor = MagicMock(spec=Motor)
                    motor.read_angle.return_value = 0.0
                    arguments = ["--interface", interface, "--current-scale", "mg"]
                    if option is not None:
                        arguments.extend([option, value])
                    with patch.object(motor_test.CanTransport, interface, return_value=transport), patch.object(
                        motor_test, "Motor", return_value=motor
                    ) as constructor, patch("builtins.print"):
                        self.assertEqual(motor_test.main(arguments), 0)
                    self.assertEqual(constructor.call_args.kwargs["limits"].max_speed_dps, expected_dps)
                    motor.control.assert_not_called()

    def test_cli_rejects_ambiguous_or_invalid_limits_before_opening_hardware(self) -> None:
        invalid_options = [
            ["--max-speed-dps", "12", "--max-speed-rpm", "2"],
            *[["--max-speed-rpm", value] for value in ("0", "-1", "nan", "inf", "1e308", "10923")],
            ["--max-speed-dps", "65536"],
        ]
        for interface in ("socketcan", "gs_usb"):
            for options in invalid_options:
                with self.subTest(interface=interface, options=options), patch.object(
                    motor_test.CanTransport, interface
                ) as factory, contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        motor_test.main(["--interface", interface, "--current-scale", "mg", *options])
                    self.assertEqual(error.exception.code, 2)
                    factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
