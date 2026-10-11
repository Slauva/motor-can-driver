import gc
import itertools
import sys
import threading
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch
from uuid import uuid4

import can

from packages.driver.interface import CanBus, Response


class CanBusLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        patches = ExitStack()
        self.addCleanup(patches.close)
        self.getoutput = patches.enter_context(
            patch("packages.driver.interface.subprocess.getoutput", return_value="/dev/ttyACM0")
        )
        self.system = patches.enter_context(patch("packages.driver.interface.os.system", return_value=0))
        self.underlying_bus = Mock(spec=can.BusABC)
        self.bus_factory = patches.enter_context(
            patch("packages.driver.interface.can.Bus", return_value=self.underlying_bus)
        )
        self.gs_usb_factory = patches.enter_context(
            patch("packages.driver.gs_usb.GsUsbBus", return_value=self.underlying_bus)
        )

    def create_bus(self, **kwargs) -> CanBus:
        bus = CanBus(**kwargs)
        self.addCleanup(bus.close)
        return bus

    def test_gs_usb_uses_device_zero_without_linux_interface_commands(self) -> None:
        bus = self.create_bus(bustype="gs_usb")

        self.assertEqual(bus.channel, 0)
        self.gs_usb_factory.assert_called_once_with(channel=0, bitrate=1_000_000)
        self.bus_factory.assert_not_called()
        bus.close()

        self.underlying_bus.shutdown.assert_called_once_with()
        self.getoutput.assert_not_called()
        self.system.assert_not_called()

    def test_gs_usb_passes_device_selection_and_bitrate_to_python_can(self) -> None:
        selections = (
            {"channel": 2},
            {"channel": "candleLight", "index": 1},
            {"channel": "candleLight", "bus": 1, "address": 3},
        )
        for options in selections:
            with self.subTest(options=options):
                self.gs_usb_factory.reset_mock()
                bus = self.create_bus(bustype="gs_usb", bitrate=500_000, **options)

                self.gs_usb_factory.assert_called_once_with(bitrate=500_000, **options)
                bus.close()

        self.getoutput.assert_not_called()
        self.system.assert_not_called()

    def test_backend_options_reach_python_can_unchanged(self) -> None:
        filters = [{"can_id": 0x141, "can_mask": 0x7FF, "extended": False}]
        bus = self.create_bus(bustype="gs_usb", can_filters=filters, ignore_config=True)

        self.gs_usb_factory.assert_called_once_with(
            channel=0, bitrate=1_000_000, can_filters=filters, ignore_config=True
        )
        bus.close()

    def test_close_is_safe_to_repeat(self) -> None:
        bus = self.create_bus(bustype="gs_usb")

        bus.close()
        bus.close()

        self.underlying_bus.shutdown.assert_called_once_with()
        self.system.assert_not_called()

    def test_open_on_open_bus_does_not_allocate_another_connection(self) -> None:
        bus = self.create_bus(bustype="gs_usb")

        bus.open()
        bus.open()

        self.gs_usb_factory.assert_called_once_with(channel=0, bitrate=1_000_000)
        self.underlying_bus.shutdown.assert_not_called()

    def test_closed_bus_can_be_reopened_and_new_connection_is_closed(self) -> None:
        replacement_bus = Mock(spec=can.BusABC)
        self.gs_usb_factory.side_effect = [self.underlying_bus, replacement_bus]
        bus = self.create_bus(bustype="gs_usb", channel=1, bitrate=250_000, ignore_config=True)

        bus.close()
        bus.open()
        self.assertIs(bus.bus, replacement_bus)
        bus.close()

        self.assertEqual(self.gs_usb_factory.call_count, 2)
        for call in self.gs_usb_factory.call_args_list:
            self.assertEqual(
                call.kwargs,
                {"channel": 1, "bitrate": 250_000, "ignore_config": True},
            )
        self.underlying_bus.shutdown.assert_called_once_with()
        replacement_bus.shutdown.assert_called_once_with()
        self.getoutput.assert_not_called()
        self.system.assert_not_called()

    def test_failed_usb_initialization_keeps_original_error_and_has_no_destructor_error(self) -> None:
        error = can.CanInitializationError("USB device not found")
        self.gs_usb_factory.side_effect = error

        with patch.object(sys, "unraisablehook") as unraisable:
            with self.assertRaises(can.CanInitializationError) as raised:
                CanBus(bustype="gs_usb")
            self.assertIs(raised.exception, error)
            # Release the failed constructor's traceback so its destructor runs
            # while the hooks and the shell commands are still patched.
            error.__traceback__ = None
            gc.collect()
            unraisable.assert_not_called()

        self.getoutput.assert_not_called()
        self.system.assert_not_called()
        self.underlying_bus.shutdown.assert_not_called()

    def test_operational_error_closes_connection_and_later_requests_do_not_use_it(self) -> None:
        for operation in ("send", "recv"):
            with self.subTest(operation=operation):
                self.underlying_bus.reset_mock(side_effect=True)
                bus = self.create_bus(bustype="gs_usb")
                error = can.CanOperationError("USB adapter disconnected")
                getattr(self.underlying_bus, operation).side_effect = error

                with self.assertRaises(can.CanOperationError) as raised:
                    if operation == "send":
                        bus.send(can.Message(arbitration_id=0x141, data=[0xA2]))
                    else:
                        bus.recv(0.1)

                self.assertIs(raised.exception, error)
                self.assertFalse(bus.is_open)
                self.underlying_bus.shutdown.assert_called_once_with()
                self.underlying_bus.send.reset_mock()
                self.underlying_bus.recv.reset_mock()
                with self.assertRaises(can.CanOperationError):
                    bus.request(0x141, [0x81])
                self.underlying_bus.send.assert_not_called()
                self.underlying_bus.recv.assert_not_called()
                bus.close()
                self.underlying_bus.shutdown.assert_called_once_with()

    def test_cleanup_failure_preserves_original_usb_operation_error(self) -> None:
        bus = self.create_bus(bustype="gs_usb")
        original = can.CanOperationError("USB overflow")
        self.underlying_bus.recv.side_effect = original
        self.underlying_bus.shutdown.side_effect = can.CanOperationError("USB device is gone")

        with self.assertRaises(can.CanOperationError) as raised:
            bus.recv(0.1)

        self.assertIs(raised.exception, original)
        self.assertFalse(bus.is_open)
        bus.close()
        self.underlying_bus.shutdown.assert_called_once_with()

    def test_request_has_a_total_timeout_and_does_not_resend_motor_command(self) -> None:
        bus = self.create_bus(bustype="gs_usb")
        self.underlying_bus.recv.return_value = None
        ticks = itertools.count(step=0.4)
        with patch("packages.driver.interface.monotonic", side_effect=lambda: next(ticks)):
            with self.assertRaises(can.CanTimeoutError):
                bus.request(0x141, [0xA2], timeout=1.0)

        self.underlying_bus.send.assert_called_once()
        self.assertLessEqual(self.underlying_bus.recv.call_count, 3)
        self.assertTrue(bus.is_open, "A response timeout should still allow a motor-stop command")

    def test_request_rejects_invalid_limits_before_sending(self) -> None:
        bus = self.create_bus(bustype="gs_usb")
        for limits in ({"timeout": 0}, {"timeout": -1}, {"n_packets": 0}, {"n_packets": -1}):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                bus.request(0x141, [0xA2], **limits)
        self.underlying_bus.send.assert_not_called()

    def test_request_skips_usb_transmit_echoes_and_waits_for_received_frames(self) -> None:
        bus = self.create_bus(bustype="gs_usb")
        echo = can.Message(arbitration_id=0x141, data=[0xA2, 0], is_rx=False)
        first_response = can.Message(arbitration_id=0x141, data=[0xA2, 0x10], is_rx=True)
        second_response = can.Message(arbitration_id=0x142, data=[0xA2, 0x20], is_rx=True)
        self.underlying_bus.recv.side_effect = [echo, None, first_response, second_response]

        responses = bus.request(0x141, [0xA2, 0], n_packets=2)

        self.assertEqual(responses, [Response(0x141, [0xA2, 0x10]), Response(0x142, [0xA2, 0x20])])
        self.assertEqual(self.underlying_bus.recv.call_count, 4)
        self.underlying_bus.send.assert_called_once()
        request = self.underlying_bus.send.call_args.args[0]
        self.assertEqual(request.arbitration_id, 0x141)
        self.assertFalse(request.is_extended_id)
        self.assertEqual(list(request.data), [0xA2, 0])

    def test_socketcan_keeps_serial_adapter_setup_and_shuts_down_connection(self) -> None:
        bus = self.create_bus()

        self.assertEqual(bus.channel, "can0")
        self.getoutput.assert_called_once_with("ls /dev/ttyACM*")
        self.bus_factory.assert_called_once_with(interface="socketcan", channel="can0", bitrate=1_000_000)
        commands = [call.args[0] for call in self.system.call_args_list]
        self.assertIn("sudo slcand -o -c -s8 -S 115200 /dev/ttyACM0 can0", commands)
        self.assertIn("sudo ip link set can0 type can bitrate 1000000", commands)
        self.assertIn("sudo ifconfig can0 up", commands)

        bus.close()
        self.underlying_bus.shutdown.assert_called_once_with()
        self.assertEqual(self.system.call_args.args, ("sudo ifconfig can0 down",))
        command_count = self.system.call_count
        bus.close()
        self.assertEqual(self.system.call_count, command_count)

    def test_socketcan_respects_custom_channel_and_serial_speed(self) -> None:
        bus = self.create_bus(channel="can1", baudrate=230_400, bitrate=500_000)

        self.bus_factory.assert_called_once_with(interface="socketcan", channel="can1", bitrate=500_000)
        commands = [call.args[0] for call in self.system.call_args_list]
        self.assertIn("sudo slcand -o -c -s8 -S 230400 /dev/ttyACM0 can1", commands)
        self.assertIn("sudo ip link set can1 type can bitrate 500000", commands)
        bus.close()


class CanBusVirtualIntegrationTests(unittest.TestCase):
    def test_request_preserves_can_id_payload_and_multiple_responses(self) -> None:
        channel = f"can-motor-test-{uuid4()}"
        with ExitStack() as resources:
            getoutput = resources.enter_context(patch("packages.driver.interface.subprocess.getoutput"))
            system = resources.enter_context(patch("packages.driver.interface.os.system"))
            bus = CanBus(channel=channel, bustype="virtual", ignore_config=True)
            resources.callback(bus.close)
            peer = can.Bus(interface="virtual", channel=channel, ignore_config=True)
            resources.callback(peer.shutdown)
            results = []
            errors = []

            def request() -> None:
                try:
                    results.extend(bus.request(0x141, [0xA2, 0, 0, 0], n_packets=2))
                except Exception as error:
                    errors.append(error)

            worker = threading.Thread(target=request, daemon=True)
            worker.start()
            command = peer.recv(1.0)
            self.assertIsNotNone(command, "Request did not reach the virtual CAN peer")
            self.assertEqual(command.arbitration_id, 0x141)
            self.assertFalse(command.is_extended_id)
            self.assertEqual(list(command.data), [0xA2, 0, 0, 0])
            peer.send(can.Message(arbitration_id=0x141, data=[0xA2, 0x10]))
            peer.send(can.Message(arbitration_id=0x142, data=[0xA2, 0x20]))

            worker.join(2.0)
            self.assertFalse(worker.is_alive(), "Request did not finish after receiving both responses")
            self.assertEqual(errors, [])
            self.assertEqual(results, [Response(0x141, [0xA2, 0x10]), Response(0x142, [0xA2, 0x20])])
            getoutput.assert_not_called()
            system.assert_not_called()


if __name__ == "__main__":
    unittest.main()
