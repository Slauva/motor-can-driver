import gc
import sys
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import can
import usb.core
from gs_usb.constants import GS_CAN_MODE_HW_TIMESTAMP
from gs_usb.gs_usb_frame import GS_USB_NONE_ECHO_ID, GsUsbFrame

from packages.driver.gs_usb import GsUsbBus


class GsUsbFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        patches = ExitStack()
        self.addCleanup(patches.close)
        self.raw_device = MagicMock()
        self.adapter = Mock()
        self.adapter.gs_usb = self.raw_device
        self.adapter.device_capability = SimpleNamespace(fclk_can=48_000_000)
        self.adapter.device_flags = GS_CAN_MODE_HW_TIMESTAMP
        self.scan = patches.enter_context(
            patch("can.interfaces.gs_usb.GsUsb.scan", return_value=[self.adapter])
        )
        patches.enter_context(
            patch("packages.driver.gs_usb.usb.util.find_descriptor", return_value=SimpleNamespace(wMaxPacketSize=64))
        )
        self.dispose = patches.enter_context(patch("packages.driver.gs_usb.usb.util.dispose_resources"))

    def create_bus(self) -> GsUsbBus:
        bus = GsUsbBus(channel=0, bitrate=1_000_000, ignore_config=True)
        self.addCleanup(bus.shutdown)
        return bus

    def test_shutdown_stops_and_releases_only_original_device_without_rescanning(self) -> None:
        bus = self.create_bus()
        bus.shutdown()
        bus.shutdown()

        self.scan.assert_called_once_with()
        self.adapter.start.assert_called_once_with()
        self.adapter.stop.assert_called_once_with()
        self.dispose.assert_called_once_with(self.raw_device)
        self.assertTrue(bus._is_shutdown)

    def test_read_uses_endpoint_packet_size_and_preserves_received_frame(self) -> None:
        bus = self.create_bus()
        frame = GsUsbFrame(can_id=0x141, data=[0x9C, 42])
        frame.echo_id = GS_USB_NONE_ECHO_ID
        frame.timestamp_us = 12_345
        self.raw_device.read.return_value = frame.pack(True)

        message = bus.recv(0.1)

        self.assertIsNotNone(message)
        self.assertEqual(message.arbitration_id, 0x141)
        self.assertEqual(list(message.data), [0x9C, 42])
        self.assertEqual(message.dlc, 2)
        self.assertTrue(message.is_rx)
        self.assertAlmostEqual(message.timestamp, 0.012345)
        self.raw_device.read.assert_called_once_with(0x81, 64, timeout=100)

    def test_usb_timeout_keeps_connection_open(self) -> None:
        bus = self.create_bus()
        self.raw_device.read.side_effect = usb.core.USBTimeoutError("Read timed out", errno=110)

        self.assertEqual(bus._recv_internal(0.1), (None, False))

        self.assertFalse(bus._is_shutdown)
        self.adapter.stop.assert_not_called()
        self.dispose.assert_not_called()

    def test_read_overflow_or_disconnect_closes_connection_and_preserves_usb_cause(self) -> None:
        for errno in (75, 19):
            with self.subTest(errno=errno):
                self.adapter.reset_mock()
                self.dispose.reset_mock()
                bus = self.create_bus()
                original = usb.core.USBError("USB overflow or removed adapter", errno=errno)
                self.raw_device.read.side_effect = original

                with self.assertRaises(can.CanOperationError) as raised:
                    bus.recv(0.1)

                self.assertIs(raised.exception.__cause__, original)
                self.assertTrue(bus._is_shutdown)
                self.adapter.stop.assert_not_called()
                self.dispose.assert_called_once_with(self.raw_device)
                bus.shutdown()
                self.dispose.assert_called_once_with(self.raw_device)
                self.raw_device.read.reset_mock()
                with self.assertRaises(can.CanOperationError):
                    bus.recv(0.1)
                self.raw_device.read.assert_not_called()

        self.assertEqual(self.scan.call_count, 2, "Shutdown must not rescan newly plugged-in adapters")

    def test_send_failure_closes_connection_and_preserves_usb_cause(self) -> None:
        bus = self.create_bus()
        original = usb.core.USBError("USB adapter removed", errno=19)
        self.adapter.send.side_effect = original

        with self.assertRaises(can.CanOperationError) as raised:
            bus.send(can.Message(arbitration_id=0x141, data=[0x81]))

        self.assertIs(raised.exception.__cause__, original)
        self.assertTrue(bus._is_shutdown)
        self.dispose.assert_called_once_with(self.raw_device)
        self.adapter.stop.assert_not_called()
        self.scan.assert_called_once_with()
        self.adapter.send.reset_mock()
        with self.assertRaises(can.CanOperationError):
            bus.send(can.Message(arbitration_id=0x141, data=[0x81]))
        self.adapter.send.assert_not_called()

    def test_partial_initialization_error_releases_handle_without_destructor_retry(self) -> None:
        original = usb.core.USBError("USB overflow", errno=75)
        self.adapter.set_timing.side_effect = original
        with patch.object(sys, "unraisablehook") as unraisable:
            with self.assertRaises(can.CanInitializationError) as raised:
                GsUsbBus(channel=0, bitrate=1_000_000, ignore_config=True)
            self.assertIs(raised.exception.__cause__, original)
            raised.exception.__traceback__ = None
            original.__traceback__ = None
            gc.collect()
            unraisable.assert_not_called()

        self.dispose.assert_called_once_with(self.raw_device)
        self.adapter.start.assert_not_called()
        self.adapter.stop.assert_not_called()
        self.scan.assert_called_once_with()

    def test_endpoint_validation_failure_stops_and_releases_started_adapter(self) -> None:
        with patch("packages.driver.gs_usb.usb.util.find_descriptor", return_value=None):
            with self.assertRaises(can.CanInitializationError):
                GsUsbBus(channel=0, bitrate=1_000_000, ignore_config=True)

        self.adapter.start.assert_called_once_with()
        self.adapter.stop.assert_called_once_with()
        self.dispose.assert_called_once_with(self.raw_device)
        self.scan.assert_called_once_with()

    def test_cleanup_error_does_not_mask_usb_failure_during_initialization_or_receive(self) -> None:
        for operation in ("initialize", "receive"):
            with self.subTest(operation=operation):
                self.adapter.reset_mock(side_effect=True)
                self.dispose.reset_mock(side_effect=True)
                self.dispose.side_effect = RuntimeError("USB resource cleanup failed")
                original = usb.core.USBError("USB overflow", errno=75)
                if operation == "initialize":
                    self.adapter.set_timing.side_effect = original
                    with self.assertRaises(can.CanInitializationError) as raised:
                        GsUsbBus(channel=0, bitrate=1_000_000, ignore_config=True)
                else:
                    bus = self.create_bus()
                    self.raw_device.read.side_effect = original
                    with self.assertRaises(can.CanOperationError) as raised:
                        bus.recv(0.1)

                self.assertIs(raised.exception.__cause__, original)
                self.dispose.assert_called_once_with(self.raw_device)

    def test_healthy_connection_reports_explicit_shutdown_cleanup_error(self) -> None:
        bus = self.create_bus()
        self.dispose.side_effect = RuntimeError("USB resource cleanup failed")

        with self.assertRaisesRegex(RuntimeError, "USB resource cleanup failed"):
            bus.shutdown()

        self.assertTrue(bus._is_shutdown)
        bus.shutdown()
        self.dispose.assert_called_once_with(self.raw_device)
        self.scan.assert_called_once_with()

    def test_scan_failure_preserves_error_without_trying_to_dispose_missing_device(self) -> None:
        original = usb.core.USBError("USB enumeration failed", errno=75)
        self.scan.side_effect = original

        with self.assertRaises(can.CanInitializationError) as raised:
            GsUsbBus(channel=0, bitrate=1_000_000, ignore_config=True)

        self.assertIs(raised.exception.__cause__, original)
        self.dispose.assert_not_called()
        self.adapter.start.assert_not_called()
        self.adapter.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
