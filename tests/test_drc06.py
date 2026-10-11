import itertools
import unittest
from unittest.mock import Mock, patch

import can

from packages.driver.DRC06 import DRC06
from packages.driver.interface import CanBus


class DRC06RequestTests(unittest.TestCase):
    def test_request_ignores_usb_transmit_echo_and_other_motor_frames(self) -> None:
        underlying_bus = Mock(spec=CanBus)
        driver = DRC06(underlying_bus, motor_id=0x142)
        payload = [0x9C, 0, 0, 0, 0, 0, 0, 0]
        echo = can.Message(arbitration_id=0x142, data=payload, is_rx=False)
        other_motor = can.Message(arbitration_id=0x141, data=[0x9C, 42, 0, 0, 0, 0, 0, 0], is_rx=True)
        response = can.Message(arbitration_id=0x142, data=[0x9C, 43, 0, 0, 0, 0, 0, 0], is_rx=True)
        underlying_bus.recv.side_effect = [echo, None, other_motor, response]

        self.assertIs(driver.request(payload), response)

        self.assertEqual(underlying_bus.recv.call_count, 4)
        underlying_bus.send.assert_called_once()
        request = underlying_bus.send.call_args.args[0]
        self.assertEqual(request.arbitration_id, 0x142)
        self.assertFalse(request.is_extended_id)
        self.assertEqual(request.dlc, 8)
        self.assertEqual(list(request.data), payload)

    def test_request_uses_wrapper_so_usb_errors_reach_connection_cleanup(self) -> None:
        bus = Mock(spec=CanBus)
        driver = DRC06(bus)
        error = can.CanOperationError("USB adapter disconnected")
        bus.recv.side_effect = error

        with self.assertRaises(can.CanOperationError) as raised:
            driver.request([0x9C, 0, 0, 0, 0, 0, 0, 0])

        self.assertIs(driver.bus, bus)
        self.assertIs(raised.exception, error)
        bus.send.assert_called_once()
        bus.recv.assert_called_once()

    def test_missing_motor_response_has_bounded_wait_without_resending(self) -> None:
        bus = Mock(spec=CanBus)
        bus.recv.return_value = can.Message(arbitration_id=0x142, data=[0x9C], is_rx=True)
        driver = DRC06(bus, motor_id=0x141)
        ticks = itertools.count(step=0.4)
        with patch("packages.driver.DRC06.monotonic", side_effect=lambda: next(ticks)):
            with self.assertRaises(can.CanTimeoutError):
                driver.request([0x9C, 0, 0, 0, 0, 0, 0, 0], timeout=1.0)

        bus.send.assert_called_once()
        self.assertLessEqual(bus.recv.call_count, 3)

    def test_invalid_timeout_does_not_send_motor_command(self) -> None:
        bus = Mock(spec=CanBus)
        driver = DRC06(bus)
        for timeout in (0, -1):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                driver.request([0x9C, 0, 0, 0, 0, 0, 0, 0], timeout=timeout)
        bus.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
