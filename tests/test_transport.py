import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch

import can

from can_motor.errors import ProtocolError
from can_motor.transport import CanTransport


PAYLOAD = bytes([0x9C, 0, 0, 0, 0, 0, 0, 0])
REPLY = bytes([0x9C, 1, 2, 3, 4, 5, 6, 7])


def message(data: bytes = REPLY, **options: object) -> can.Message:
    return can.Message(arbitration_id=0x141, is_extended_id=False, data=data, **options)


class TransportTests(unittest.TestCase):
    def make_transport(self) -> tuple[CanTransport, Mock]:
        bus = Mock(spec=can.BusABC)
        transport = CanTransport(bus)
        self.addCleanup(transport.close)
        return transport, bus

    def test_owned_bus_is_closed_once_and_rejects_new_requests(self) -> None:
        transport, bus = self.make_transport()
        with transport as entered:
            self.assertIs(entered, transport)
            self.assertTrue(transport.is_open)
        transport.close()
        self.assertFalse(transport.is_open)
        bus.shutdown.assert_called_once()
        with self.assertRaises(can.CanOperationError):
            transport.request(0x141, PAYLOAD)
        bus.send.assert_not_called()

    def test_invalid_arguments_are_rejected_before_bus_access(self) -> None:
        transport, bus = self.make_transport()
        for identifier in (-1, 0x800, True, 1.0):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                transport.request(identifier, PAYLOAD)
        for payload in (b"", b"1234567", b"123456789", bytearray(PAYLOAD)):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                transport.request(0x141, payload)
        for timeout in (0, -1, float("nan"), float("inf"), 10**1000, True, "0.1"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                transport.request(0x141, PAYLOAD, timeout=timeout)
        for commands in ((), (256,), (-1,), (True,), [0x9C]):
            with self.subTest(commands=commands), self.assertRaises(ValueError):
                transport.request(0x141, PAYLOAD, response_commands=commands)
        bus.send.assert_not_called()
        bus.recv.assert_not_called()

    def test_filters_nonmatching_frames_and_transmit_echoes(self) -> None:
        transport, bus = self.make_transport()
        unrelated_id = can.Message(arbitration_id=0x142, is_extended_id=False, data=REPLY)
        extended = can.Message(arbitration_id=0x141, is_extended_id=True, data=REPLY)
        bus.recv.side_effect = [
            None,
            unrelated_id,
            extended,
            message(is_fd=True),
            message(is_remote_frame=True),
            message(is_error_frame=True),
            message(is_rx=False),
            message(bytes([0x92])),
            message(),
        ]

        self.assertEqual(transport.request(0x141, PAYLOAD), REPLY)
        bus.send.assert_called_once()
        sent = bus.send.call_args.args[0]
        self.assertEqual(sent.arbitration_id, 0x141)
        self.assertFalse(sent.is_extended_id)
        self.assertEqual(bytes(sent.data), PAYLOAD)
        self.assertGreater(bus.send.call_args.kwargs["timeout"], 0)

    def test_explicit_response_commands_allow_protocol_specific_reply(self) -> None:
        transport, bus = self.make_transport()
        response = bytes([0x93]) + REPLY[1:]
        bus.recv.side_effect = [None, message(response)]
        self.assertEqual(transport.request(0x141, PAYLOAD, response_commands=(0x92, 0x93)), response)

    def test_matching_truncated_or_invalid_dlc_is_a_protocol_error(self) -> None:
        for data, dlc in ((b"", 0), (b"\x9c", 1), (REPLY, 7), (REPLY[:7], 8)):
            with self.subTest(data=data, dlc=dlc):
                transport, bus = self.make_transport()
                bus.recv.side_effect = [None, message(data, dlc=dlc)]
                with self.assertRaises(ProtocolError):
                    transport.request(0x141, PAYLOAD)
                self.assertTrue(transport.is_open)

    def test_stale_queued_response_is_drained_before_transmission(self) -> None:
        transport, bus = self.make_transport()
        stale = message(bytes([0x9C, 99, 0, 0, 0, 0, 0, 0]))
        bus.recv.side_effect = [stale, None, message()]
        self.assertEqual(transport.request(0x141, PAYLOAD), REPLY)
        self.assertEqual(bus.method_calls[0][0], "recv")
        self.assertEqual(bus.method_calls[1][0], "recv")
        self.assertEqual(bus.method_calls[2][0], "send")

    def test_busy_receive_queue_fails_before_send(self) -> None:
        transport, bus = self.make_transport()
        bus.recv.return_value = message()
        with self.assertRaises(can.CanTimeoutError):
            transport.request(0x141, PAYLOAD)
        self.assertLessEqual(bus.recv.call_count, transport._MAX_DRAIN_FRAMES)
        bus.send.assert_not_called()
        self.assertTrue(transport.is_open)

    def test_reply_timeout_keeps_connection_available_for_following_stop(self) -> None:
        transport, bus = self.make_transport()
        bus.recv.side_effect = [None, None]
        with self.assertRaises(can.CanTimeoutError):
            transport.request(0x141, PAYLOAD, timeout=0.01)
        self.assertTrue(transport.is_open)
        bus.shutdown.assert_not_called()
        stop = bytes([0x81, 0, 0, 0, 0, 0, 0, 0])
        bus.recv.side_effect = [None, message(stop)]
        self.assertEqual(transport.request(0x141, stop), stop)
        self.assertEqual(bus.send.call_count, 2)

    def test_send_timeout_does_not_close_or_retry(self) -> None:
        transport, bus = self.make_transport()
        bus.recv.return_value = None
        original = can.CanTimeoutError("write timed out")
        bus.send.side_effect = original
        with self.assertRaises(can.CanTimeoutError) as raised:
            transport.request(0x141, PAYLOAD)
        self.assertIs(raised.exception, original)
        self.assertTrue(transport.is_open)
        bus.send.assert_called_once()
        bus.shutdown.assert_not_called()

    def test_send_consumes_same_deadline_as_receive(self) -> None:
        transport, bus = self.make_transport()
        bus.recv.side_effect = [None, message()]
        clock = [100.0]

        def slow_send(*args: object, **kwargs: object) -> None:
            clock[0] += 0.2

        bus.send.side_effect = slow_send
        with patch("can_motor.transport.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(can.CanTimeoutError):
                transport.request(0x141, PAYLOAD, timeout=0.1)
        bus.send.assert_called_once()
        self.assertEqual(bus.recv.call_count, 1, "Expired send budget must not restart a receive timeout")

    def test_lock_wait_is_included_in_deadline(self) -> None:
        transport, bus = self.make_transport()
        transport._lock.acquire()
        try:
            start = time.monotonic()
            with self.assertRaises(can.CanTimeoutError):
                transport.request(0x141, PAYLOAD, timeout=0.01)
            self.assertLess(time.monotonic() - start, 0.2)
        finally:
            transport._lock.release()
        bus.send.assert_not_called()
        self.assertTrue(transport.is_open)

    def test_adapter_failure_closes_connection_and_preserves_original_error(self) -> None:
        for operation in ("send", "recv", "drain"):
            with self.subTest(operation=operation):
                transport, bus = self.make_transport()
                original = can.CanOperationError("adapter disconnected")
                bus.shutdown.side_effect = RuntimeError("cleanup also failed")
                if operation == "send":
                    bus.recv.return_value = None
                    bus.send.side_effect = original
                else:
                    bus.recv.side_effect = [original] if operation == "drain" else [None, original]
                with self.assertRaises(can.CanOperationError) as raised:
                    transport.request(0x141, PAYLOAD)
                self.assertIs(raised.exception, original)
                self.assertFalse(transport.is_open)
                bus.shutdown.assert_called_once()
                self.assertLessEqual(bus.send.call_count, 1)

    def test_factories_force_ignoring_python_can_configuration(self) -> None:
        for backend in ("socketcan", "virtual"):
            with self.subTest(backend=backend), patch("can_motor.transport.can.Bus") as bus_factory:
                transport = getattr(CanTransport, backend)("test", ignore_config=False)
                transport.close()
                bus_factory.assert_called_once_with(interface=backend, channel="test", ignore_config=True)
        with patch("can_motor.backends.gs_usb.GsUsbBus") as bus_factory:
            transport = CanTransport.gs_usb(ignore_config=False)
            transport.close()
            bus_factory.assert_called_once_with(channel=0, bitrate=1_000_000, ignore_config=True)

    def test_gs_usb_invalid_selection_is_rejected_without_opening_hardware(self) -> None:
        invalid_options = (
            {"channel": -1}, {"channel": True}, {"channel": "0"}, {"channel": 0.0},
            {"index": -1}, {"index": True}, {"index": 0.0}, {"index": None},
            {"bitrate": 0}, {"bitrate": -1}, {"bitrate": True}, {"bitrate": 1_000_000.0},
            {"bus": 1}, {"address": 2},
            {"bus": -1, "address": 2}, {"bus": 1, "address": -1},
            {"bus": True, "address": 2}, {"bus": 1, "address": False},
            {"bus": 1.0, "address": 2}, {"bus": 1, "address": None},
            {"index": 0, "bus": 1, "address": 2},
        )
        with patch("can_motor.backends.gs_usb.GsUsbBus") as bus_factory:
            for options in invalid_options:
                with self.subTest(options=options), self.assertRaises(ValueError):
                    CanTransport.gs_usb(**options)
            bus_factory.assert_not_called()

    def test_gs_usb_valid_explicit_selection_preserves_advanced_options(self) -> None:
        for selection in ({"index": 2}, {"bus": 3, "address": 5}, {"bus": 0, "address": 0}):
            with self.subTest(selection=selection), patch("can_motor.backends.gs_usb.GsUsbBus") as bus_factory:
                filters = [{"can_id": 0x141, "can_mask": 0x7FF}]
                transport = CanTransport.gs_usb(
                    channel=0, bitrate=500_000, can_filters=filters, **selection,
                )
                transport.close()
                bus_factory.assert_called_once_with(
                    channel=0, bitrate=500_000, can_filters=filters,
                    ignore_config=True, **selection,
                )


class VirtualTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        channel = f"can-motor-test-{uuid.uuid4()}"
        self.transport = CanTransport.virtual(channel)
        self.peer = can.Bus(interface="virtual", channel=channel, ignore_config=True)
        self.addCleanup(self.transport.close)
        self.addCleanup(self.peer.shutdown)

    def test_request_round_trip_on_real_python_can_virtual_bus(self) -> None:
        errors: list[BaseException] = []

        def responder() -> None:
            try:
                request = self.peer.recv(0.5)
                self.assertIsNotNone(request)
                self.assertEqual(bytes(request.data), PAYLOAD)
                self.peer.send(message())
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=responder, daemon=True)
        worker.start()
        self.assertEqual(self.transport.request(0x141, PAYLOAD), REPLY)
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_real_virtual_queue_drops_stale_matching_response(self) -> None:
        stale = bytes([0x9C, 99, 0, 0, 0, 0, 0, 0])
        self.peer.send(message(stale))

        def responder() -> None:
            request = self.peer.recv(0.5)
            if request is not None:
                self.peer.send(message())

        worker = threading.Thread(target=responder, daemon=True)
        worker.start()
        self.assertEqual(self.transport.request(0x141, PAYLOAD), REPLY)
        worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_two_motors_share_transaction_lock_until_each_response(self) -> None:
        errors: list[BaseException] = []
        results: dict[int, bytes] = {}
        barrier = threading.Barrier(3)

        def requester(identifier: int) -> None:
            try:
                barrier.wait(timeout=1)
                results[identifier] = self.transport.request(identifier, PAYLOAD, timeout=0.5)
            except BaseException as exc:
                errors.append(exc)

        workers = [threading.Thread(target=requester, args=(identifier,), daemon=True) for identifier in (0x141, 0x142)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=1)
        for _ in range(2):
            request = self.peer.recv(0.5)
            self.assertIsNotNone(request)
            # A second request must not reach the wire before the first reply.
            self.assertIsNone(self.peer.recv(0.02))
            self.peer.send(can.Message(arbitration_id=request.arbitration_id, is_extended_id=False, data=REPLY))
        for worker in workers:
            worker.join(1)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, {0x141: REPLY, 0x142: REPLY})

    def test_real_virtual_bus_timeout_is_bounded(self) -> None:
        start = time.monotonic()
        with self.assertRaises(can.CanTimeoutError):
            self.transport.request(0x141, PAYLOAD, timeout=0.02)
        self.assertLess(time.monotonic() - start, 0.2)
        self.assertTrue(self.transport.is_open)
        self.assertIsNotNone(self.peer.recv(0))
        self.assertIsNone(self.peer.recv(0))


if __name__ == "__main__":
    unittest.main()
