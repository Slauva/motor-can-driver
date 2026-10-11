import importlib
import unittest
from unittest.mock import Mock, patch

import can


class ExampleLifecycleTests(unittest.TestCase):
    example_names = ("examples.rotate_motor_gs_usb", "examples.rotate_motor_socketcan")

    def test_disconnect_skips_motor_commands_and_closes_connection(self) -> None:
        for name in self.example_names:
            with self.subTest(example=name):
                example = importlib.import_module(name)
                bus = Mock(is_open=True)
                motor = Mock()

                def disconnect():
                    bus.is_open = False
                    raise can.CanOperationError("USB adapter disconnected")

                motor.info.side_effect = disconnect
                with patch.object(example, "CanBus", return_value=bus), patch.object(
                    example, "Gyems", return_value=motor
                ), patch("builtins.print"):
                    self.assertEqual(example.main(), 1)

                motor.disable.assert_not_called()
                bus.close.assert_called_once_with()

    def test_keyboard_interrupt_stops_motor_and_closes_connection(self) -> None:
        for name in self.example_names:
            with self.subTest(example=name):
                example = importlib.import_module(name)
                bus = Mock(is_open=True)
                motor = Mock()
                motor.info.side_effect = KeyboardInterrupt
                with patch.object(example, "CanBus", return_value=bus), patch.object(
                    example, "Gyems", return_value=motor
                ), patch("builtins.print"):
                    self.assertEqual(example.main(), 0)

                motor.disable.assert_called_once_with(True)
                bus.close.assert_called_once_with()

    def test_failed_connection_does_not_construct_motor(self) -> None:
        for name in self.example_names:
            with self.subTest(example=name):
                example = importlib.import_module(name)
                with patch.object(
                    example, "CanBus", side_effect=can.CanInitializationError("USB overflow")
                ), patch.object(example, "Gyems") as motor_factory, patch("builtins.print"):
                    self.assertEqual(example.main(), 1)

                motor_factory.assert_not_called()

    def test_error_while_stopping_motor_still_closes_connection(self) -> None:
        for name in self.example_names:
            with self.subTest(example=name):
                example = importlib.import_module(name)
                bus = Mock(is_open=True)
                motor = Mock()
                motor.info.side_effect = KeyboardInterrupt
                motor.disable.side_effect = can.CanTimeoutError("No motor reply")
                with patch.object(example, "CanBus", return_value=bus), patch.object(
                    example, "Gyems", return_value=motor
                ), patch("builtins.print"):
                    self.assertEqual(example.main(), 1)

                bus.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
