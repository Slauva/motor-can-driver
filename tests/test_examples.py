import contextlib
import io
import unittest
from unittest.mock import MagicMock, patch

import can

from can_motor import CanTransport, Motor
from examples import motor_test, virtual_motor


class ExampleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = MagicMock(spec=CanTransport)
        self.transport.__enter__.return_value = self.transport
        self.motor = MagicMock(spec=Motor)
        self.motor.read_angle.return_value = 0.0

    def test_default_run_reads_only_for_both_interfaces(self) -> None:
        for interface, channel in (("socketcan", "can0"), ("gs_usb", "0")):
            with self.subTest(interface=interface), patch.object(
                motor_test.CanTransport, interface, return_value=self.transport
            ) as factory, patch.object(motor_test, "Motor", return_value=self.motor), patch("builtins.print"):
                self.assertEqual(motor_test.main([
                    "--interface", interface, "--channel", channel, "--current-scale", "mg",
                ]), 0)
                if interface == "socketcan":
                    factory.assert_called_once_with(channel="can0")
                else:
                    factory.assert_called_once_with(channel=0, bitrate=1_000_000)
                self.motor.read_health.assert_called_once()
                self.motor.read_feedback.assert_called_once()
                self.motor.read_angle.assert_called_once()
                self.motor.control.assert_not_called()
                self.motor.set_current.assert_not_called()
                self.transport.__exit__.assert_called_once()
                self.motor.reset_mock()
                self.transport.reset_mock()

    def test_invalid_current_is_rejected_before_opening_bus(self) -> None:
        with patch.object(motor_test.CanTransport, "socketcan") as factory, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                motor_test.main(["--interface", "socketcan", "--current-scale", "mg", "--current-a", "2"])
            self.assertEqual(error.exception.code, 2)
            factory.assert_not_called()

    def test_current_loop_uses_motor_context_and_closes_on_interrupt(self) -> None:
        self.motor.set_current.side_effect = KeyboardInterrupt
        with patch.object(motor_test.CanTransport, "gs_usb", return_value=self.transport), patch.object(
            motor_test, "Motor", return_value=self.motor
        ), patch("builtins.print"):
            self.assertEqual(motor_test.main([
                "--interface", "gs_usb", "--current-scale", "mg", "--current-a", "0.2",
            ]), 130)
        self.motor.control.assert_called_once()
        context = self.motor.control.return_value
        context.__enter__.assert_called_once()
        self.assertIs(context.__exit__.call_args.args[0], KeyboardInterrupt)
        self.transport.__exit__.assert_called_once()

    def test_missing_reply_returns_failure_and_closes_transport(self) -> None:
        self.motor.read_health.side_effect = can.CanTimeoutError("no reply")
        with patch.object(motor_test.CanTransport, "socketcan", return_value=self.transport), patch.object(
            motor_test, "Motor", return_value=self.motor
        ), patch("builtins.print"):
            self.assertEqual(motor_test.main(["--interface", "socketcan", "--current-scale", "mg"]), 1)
        self.motor.control.assert_not_called()
        self.transport.__exit__.assert_called_once()

    def test_virtual_demo_exercises_real_transport_and_motor_without_hardware(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(virtual_motor.main(), 0)
        self.assertIn("voltage_v=24.0", output.getvalue())
        self.assertEqual(output.getvalue().count("Feedback("), 3)


if __name__ == "__main__":
    unittest.main()
