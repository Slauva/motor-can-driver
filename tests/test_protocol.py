"""Hardware-free checks against the LK-TECH CAN V2.35 byte tables.

Golden frames below are written explicitly rather than encoded by the codec
under test. They exercise units, signs and field offsets that differ between
commands and are particularly important when commissioning a motor.
"""

import unittest
from dataclasses import FrozenInstanceError

from can_motor.errors import ProtocolError
from can_motor.models import (
    CurrentScale,
    Health,
    MF_CURRENT_SCALE,
    MG_CURRENT_SCALE,
    MotorLimits,
    PidParameters,
    finite_number,
    integer_range,
)
from can_motor.protocol import (
    Command,
    acceleration_command,
    arbitration_id,
    current_command,
    decode_acceleration,
    decode_angle,
    decode_encoder,
    decode_feedback,
    decode_health,
    decode_phase_currents,
    decode_pid,
    pid_command,
    position_command,
    query,
    speed_command,
    validate_reply,
)


class CommandEncodingTests(unittest.TestCase):
    def test_standard_single_motor_identifier_range(self) -> None:
        self.assertEqual(arbitration_id(1), 0x141)
        self.assertEqual(arbitration_id(32), 0x160)
        for node_id in (0, 33, -1, True, 1.0, "1"):
            with self.subTest(node_id=node_id), self.assertRaises(ValueError):
                arbitration_id(node_id)

    def test_read_and_lifecycle_commands_have_eight_bytes(self) -> None:
        for command, expected in (
            (Command.READ_PID, "30 00 00 00 00 00 00 00"),
            (Command.READ_ACCELERATION, "33 00 00 00 00 00 00 00"),
            (Command.OFF, "80 00 00 00 00 00 00 00"),
            (Command.STOP, "81 00 00 00 00 00 00 00"),
            (Command.RUN, "88 00 00 00 00 00 00 00"),
            (Command.READ_ENCODER, "90 00 00 00 00 00 00 00"),
            (Command.READ_ANGLE, "92 00 00 00 00 00 00 00"),
            (Command.READ_SINGLE_TURN_ANGLE, "94 00 00 00 00 00 00 00"),
            (Command.READ_HEALTH, "9a 00 00 00 00 00 00 00"),
            (Command.CLEAR_ERRORS, "9b 00 00 00 00 00 00 00"),
            (Command.READ_FEEDBACK, "9c 00 00 00 00 00 00 00"),
            (Command.READ_PHASE_CURRENTS, "9d 00 00 00 00 00 00 00"),
        ):
            with self.subTest(command=command):
                self.assertEqual(query(command), bytes.fromhex(expected))

    def test_current_uses_series_scale_and_signed_little_endian_int16(self) -> None:
        for scale, amperes, expected in (
            (MF_CURRENT_SCALE, 16.5, "a1 00 00 00 00 08 00 00"),
            (MF_CURRENT_SCALE, -16.5, "a1 00 00 00 00 f8 00 00"),
            (MF_CURRENT_SCALE, 8.25, "a1 00 00 00 00 04 00 00"),
            (MF_CURRENT_SCALE, -8.25, "a1 00 00 00 00 fc 00 00"),
            (MG_CURRENT_SCALE, 33, "a1 00 00 00 00 08 00 00"),
            (MG_CURRENT_SCALE, -33, "a1 00 00 00 00 f8 00 00"),
            (MG_CURRENT_SCALE, 16.5, "a1 00 00 00 00 04 00 00"),
            (MG_CURRENT_SCALE, -16.5, "a1 00 00 00 00 fc 00 00"),
            (MF_CURRENT_SCALE, 1, "a1 00 00 00 7c 00 00 00"),
            (MG_CURRENT_SCALE, -1, "a1 00 00 00 c2 ff 00 00"),
        ):
            with self.subTest(scale=scale, amperes=amperes):
                self.assertEqual(current_command(amperes, scale), bytes.fromhex(expected))

    def test_current_quantizes_toward_zero_and_never_accepts_excess_scale(self) -> None:
        for value in (0, 0.001, -0.001):
            with self.subTest(value=value):
                self.assertEqual(
                    current_command(value, MF_CURRENT_SCALE),
                    bytes.fromhex("a1 00 00 00 00 00 00 00"),
                )
        for scale in (MF_CURRENT_SCALE, MG_CURRENT_SCALE):
            for value in (scale.full_scale_a + 0.00001, -scale.full_scale_a - 0.00001):
                with self.subTest(scale=scale, value=value), self.assertRaises(ValueError):
                    current_command(value, scale)

    def test_speed_has_hundredth_degree_per_second_units_and_signed_int32(self) -> None:
        for speed, expected in (
            (12.34, "a2 00 00 00 d2 04 00 00"),
            (-12.34, "a2 00 00 00 2e fb ff ff"),
            (0.29, "a2 00 00 00 1d 00 00 00"),
            (-0.29, "a2 00 00 00 e3 ff ff ff"),
            (0.009, "a2 00 00 00 00 00 00 00"),
            (-0.009, "a2 00 00 00 00 00 00 00"),
            (21474836.47, "a2 00 00 00 ff ff ff 7f"),
            (-21474836.48, "a2 00 00 00 00 00 00 80"),
        ):
            with self.subTest(speed=speed):
                self.assertEqual(speed_command(speed), bytes.fromhex(expected))
        for speed in (21474836.48, -21474836.49):
            with self.subTest(speed=speed), self.assertRaises(ValueError):
                speed_command(speed)

    def test_position_has_uint16_whole_dps_and_signed_hundredth_degree_angle(self) -> None:
        for position, speed, expected in (
            (123.45, 360, "a4 00 68 01 39 30 00 00"),
            (-90.25, 30, "a4 00 1e 00 bf dc ff ff"),
            (0.29, 30.9, "a4 00 1e 00 1d 00 00 00"),
            (21474836.47, 65535, "a4 00 ff ff ff ff ff 7f"),
            (-21474836.48, 1, "a4 00 01 00 00 00 00 80"),
        ):
            with self.subTest(position=position, speed=speed):
                self.assertEqual(position_command(position, speed), bytes.fromhex(expected))
        for position, speed in (
            (0, 0), (0, -1), (0, 0.999), (0, 65536),
            (21474836.48, 1), (-21474836.49, 1),
        ):
            with self.subTest(position=position, speed=speed), self.assertRaises(ValueError):
                position_command(position, speed)

    def test_acceleration_writes_positive_int32_to_ram(self) -> None:
        self.assertEqual(acceleration_command(100), bytes.fromhex("34 00 00 00 64 00 00 00"))
        self.assertEqual(
            acceleration_command(2**31 - 1), bytes.fromhex("34 00 00 00 ff ff ff 7f")
        )
        for value in (0, -1, 2**31, 1.5, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                acceleration_command(value)

    def test_pid_ram_write_preserves_six_raw_uint8_coefficients(self) -> None:
        coefficients = PidParameters(0, 1, 127, 128, 254, 255)
        self.assertEqual(pid_command(coefficients), bytes.fromhex("31 00 00 01 7f 80 fe ff"))

    def test_numeric_control_inputs_reject_boolean_nonfinite_and_nonnumeric_values(self) -> None:
        for value in (True, False, float("nan"), float("inf"), -float("inf"), "1", None):
            for name, operation in (
                ("current", lambda: current_command(value, MF_CURRENT_SCALE)),
                ("speed", lambda: speed_command(value)),
                ("position", lambda: position_command(value, 30)),
                ("position speed", lambda: position_command(0, value)),
            ):
                with self.subTest(value=value, field=name), self.assertRaises(ValueError):
                    operation()


class ReplyDecodingTests(unittest.TestCase):
    def test_feedback_signs_encoder_and_control_current_scale(self) -> None:
        for command in (0xA1, 0xA2, 0xA4):
            payload = bytes([command]) + bytes.fromhex("fe 00 fc 2e fb cd ab")
            for scale, expected_current in ((MF_CURRENT_SCALE, -8.25), (MG_CURRENT_SCALE, -16.5)):
                with self.subTest(command=command, scale=scale):
                    feedback = decode_feedback(payload, scale)
                    self.assertEqual(feedback.temperature_c, -2)
                    self.assertEqual(feedback.current_a, expected_current)
                    self.assertEqual(feedback.speed_dps, -1234)
                    self.assertEqual(feedback.encoder_raw, 0xABCD)

    def test_read_feedback_has_separately_configurable_current_scale(self) -> None:
        payload = bytes.fromhex("9c 19 00 04 ff 7f ff ff")
        self.assertEqual(decode_feedback(payload, MF_CURRENT_SCALE).current_a, 16.5)
        custom = CurrentScale(full_scale_a=16.5, status_full_scale_a=20)
        self.assertEqual(decode_feedback(payload, custom).current_a, 10)
        control_reply = bytes.fromhex("a1 19 00 04 ff 7f ff ff")
        self.assertEqual(decode_feedback(control_reply, custom).current_a, 8.25)

    def test_feedback_current_range_and_signed_speed_boundaries(self) -> None:
        positive = decode_feedback(bytes.fromhex("a1 7f 00 08 ff 7f 00 00"), MF_CURRENT_SCALE)
        negative = decode_feedback(bytes.fromhex("9c 80 00 f8 00 80 ff ff"), MF_CURRENT_SCALE)
        self.assertEqual((positive.temperature_c, positive.current_a, positive.speed_dps), (127, 16.5, 32767))
        self.assertEqual((negative.temperature_c, negative.current_a, negative.speed_dps), (-128, -33, -32768))
        for raw_current in ("01 08", "ff f7", "ff 7f", "00 80"):
            payload = bytes.fromhex(f"a1 00 {raw_current} 00 00 00 00")
            with self.subTest(raw_current=raw_current), self.assertRaises(ProtocolError):
                decode_feedback(payload, MF_CURRENT_SCALE)

    def test_health_voltage_temperature_and_documented_fault_bits(self) -> None:
        for command in (0x9A, 0x9B):
            health = decode_health(bytes([command]) + bytes.fromhex("fb 00 f1 00 00 00 09"))
            self.assertEqual(health.temperature_c, -5)
            self.assertEqual(health.voltage_v, 24.1)
            self.assertTrue(health.undervoltage)
            self.assertTrue(health.overtemperature)
            self.assertTrue(health.has_fault)
        for flags, undervoltage, overtemperature in ((0, False, False), (1, True, False), (8, False, True), (2, False, False)):
            with self.subTest(flags=flags):
                health = Health(25, 24, flags)
                self.assertEqual(health.undervoltage, undervoltage)
                self.assertEqual(health.overtemperature, overtemperature)
                self.assertEqual(health.has_fault, flags != 0)

    def test_phase_currents_use_sixty_four_lsb_per_ampere_and_signed_fields(self) -> None:
        phases = decode_phase_currents(bytes.fromhex("9d f6 40 00 80 ff 20 00"))
        self.assertEqual(phases.temperature_c, -10)
        self.assertEqual((phases.phase_a_a, phases.phase_b_a, phases.phase_c_a), (1, -2, 0.5))

    def test_encoder_fields_are_unsigned_and_begin_at_byte_two(self) -> None:
        encoder = decode_encoder(bytes.fromhex("90 ff 34 12 ff ff 00 80"))
        self.assertEqual((encoder.position_raw, encoder.original_raw, encoder.offset_raw), (0x1234, 65535, 32768))

    def test_multi_turn_angle_is_signed_seven_byte_hundredth_degree_value(self) -> None:
        for payload, expected in (
            ("92 a1 8c 00 00 00 00 00", 360.01),
            ("92 5f 73 ff ff ff ff ff", -360.01),
            ("92 ff ff ff ff ff ff ff", -0.01),
            ("92 80 00 00 00 00 00 00", 1.28),
            ("92 00 00 00 00 01 00 00", 42949672.96),
            ("92 00 00 00 00 00 00 80", -(2**55) / 100),
            ("92 ff ff ff ff ff ff 7f", (2**55 - 1) / 100),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(decode_angle(bytes.fromhex(payload)), expected)

    def test_single_turn_angle_is_uint32_at_bytes_four_through_seven(self) -> None:
        for payload, expected in (
            ("94 aa bb cc 00 00 00 00", 0),
            ("94 aa bb cc 28 23 00 00", 90),
            ("94 aa bb cc 9f 8c 00 00", 359.99),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(decode_angle(bytes.fromhex(payload), single_turn=True), expected)
        for raw in ("a0 8c 00 00", "00 00 01 00", "ff ff ff ff"):
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                decode_angle(bytes.fromhex(f"94 00 00 00 {raw}"), single_turn=True)

    def test_pid_read_and_ram_echo_return_raw_uint8_values(self) -> None:
        for command in (0x30, 0x31):
            payload = bytes([command, 0, 0, 1, 127, 128, 254, 255])
            self.assertEqual(decode_pid(payload), PidParameters(0, 1, 127, 128, 254, 255))

    def test_acceleration_read_is_signed_int32_in_whole_dps_squared(self) -> None:
        for payload, expected in (
            ("33 00 00 00 64 00 00 00", 100),
            ("34 00 00 00 64 00 00 00", 100),
            ("33 00 00 00 ff ff ff ff", -1),
            ("33 00 00 00 00 00 00 80", -(2**31)),
            ("33 00 00 00 ff ff ff 7f", 2**31 - 1),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(decode_acceleration(bytes.fromhex(payload)), expected)

    def test_every_decoder_rejects_wrong_length_and_command(self) -> None:
        for name, operation in (
            ("health", decode_health),
            ("feedback", lambda payload: decode_feedback(payload, MF_CURRENT_SCALE)),
            ("angle", decode_angle),
            ("single angle", lambda payload: decode_angle(payload, single_turn=True)),
            ("encoder", decode_encoder),
            ("phases", decode_phase_currents),
            ("PID", decode_pid),
            ("acceleration", decode_acceleration),
        ):
            for payload in (b"", bytes(7), bytes(9), bytes(8)):
                with self.subTest(decoder=name, payload=payload), self.assertRaises(ProtocolError):
                    operation(payload)
        payload = bytes.fromhex("9a 00 00 00 00 00 00 00")
        self.assertIs(validate_reply(payload, Command.READ_HEALTH), payload)


class ConfigurationValidationTests(unittest.TestCase):
    def test_numeric_helpers_reject_bool_without_rejecting_zero(self) -> None:
        self.assertEqual(finite_number("value", 0), 0)
        self.assertEqual(integer_range("value", 0, 0, 255), 0)
        for value in (True, False, None, "1", float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                finite_number("value", value)
        for value in (True, False, 1.0, -1, 256, "1", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                integer_range("value", value, 0, 255)

    def test_current_scale_requires_positive_finite_control_and_status_amperes(self) -> None:
        self.assertEqual(MF_CURRENT_SCALE, CurrentScale(16.5, 33))
        self.assertEqual(MG_CURRENT_SCALE, CurrentScale(33, 33))
        for name in ("full_scale_a", "status_full_scale_a"):
            for value in (0, -1, True, False, float("nan"), float("inf"), "33"):
                values = {"full_scale_a": 16.5, "status_full_scale_a": 33, name: value}
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    CurrentScale(**values)
        with self.assertRaises(FrozenInstanceError):
            MF_CURRENT_SCALE.full_scale_a = 100

    def test_default_limits_are_conservative_and_require_external_current_limit_for_motion(self) -> None:
        limits = MotorLimits()
        self.assertEqual(limits.max_current_a, 1)
        self.assertEqual(limits.max_speed_dps, 30)
        self.assertEqual((limits.min_position_deg, limits.max_position_deg), (-180, 180))
        self.assertIsNone(limits.firmware_current_limit_a)
        with self.assertRaises(FrozenInstanceError):
            limits.max_current_a = 100

    def test_limits_reject_nonfinite_and_boolean_configuration_fields(self) -> None:
        for field in (
            "max_current_a", "max_speed_dps", "min_position_deg", "max_position_deg",
            "max_temperature_c", "min_voltage_v", "max_voltage_v", "firmware_current_limit_a",
        ):
            for value in (True, False, float("nan"), float("inf"), -float("inf"), "1"):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    MotorLimits(**{field: value})

    def test_limits_reject_invalid_order_ranges_and_nonpositive_constraints(self) -> None:
        for values in (
            {"max_current_a": 0}, {"max_current_a": -1},
            {"max_speed_dps": 0}, {"max_speed_dps": -1}, {"max_speed_dps": 65536},
            {"max_acceleration_dps2": 0}, {"max_acceleration_dps2": -1},
            {"max_acceleration_dps2": 2**31}, {"max_acceleration_dps2": True},
            {"max_acceleration_dps2": 1.5},
            {"min_position_deg": 180}, {"max_position_deg": -180},
            {"min_position_deg": -21474836.49}, {"max_position_deg": 21474836.48},
            {"max_temperature_c": 128}, {"max_temperature_c": -129},
            {"min_voltage_v": 0}, {"max_voltage_v": -1},
            {"min_voltage_v": 24, "max_voltage_v": 24},
            {"min_voltage_v": 25, "max_voltage_v": 24},
            {"firmware_current_limit_a": 0}, {"firmware_current_limit_a": -1},
            {"firmware_current_limit_a": 1.01},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                MotorLimits(**values)

    def test_limit_boundaries_can_be_selected_explicitly(self) -> None:
        limits = MotorLimits(
            max_current_a=0.1,
            max_speed_dps=65535,
            min_position_deg=-21474836.48,
            max_position_deg=21474836.47,
            max_acceleration_dps2=2**31 - 1,
            max_temperature_c=127,
            min_voltage_v=20,
            max_voltage_v=30,
            firmware_current_limit_a=0.1,
        )
        self.assertEqual(limits.firmware_current_limit_a, limits.max_current_a)

    def test_pid_bounds_apply_to_each_raw_coefficient(self) -> None:
        fields = ("position_kp", "position_ki", "speed_kp", "speed_ki", "current_kp", "current_ki")
        self.assertEqual(PidParameters(0, 255, 0, 255, 0, 255).to_bytes(), bytes.fromhex("00 ff 00 ff 00 ff"))
        for field in fields:
            for value in (-1, 256, True, False, 1.0, "1", None, float("nan")):
                values = dict.fromkeys(fields, 0)
                values[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    PidParameters(**values)


if __name__ == "__main__":
    unittest.main()
