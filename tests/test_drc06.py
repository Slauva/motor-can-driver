import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import can

from packages.driver.DRC06 import DRC06


class DRC06RequestTests(unittest.TestCase):
    def test_request_ignores_usb_transmit_echo_and_other_motor_frames(self) -> None:
        underlying_bus = Mock(spec=can.BusABC)
        driver = DRC06(SimpleNamespace(bus=underlying_bus), motor_id=0x142)
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


if __name__ == "__main__":
    unittest.main()
