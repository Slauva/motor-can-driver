"""A single owner and request lock for one classic CAN connection.

All motors on a connection must share one :class:`CanTransport`. The wire
protocol has no transaction counter, so callers must also avoid competing CAN
readers or writers for the same motors. A bounded drain removes queued stale
frames before each transmission. A delayed old response arriving after a new
request cannot be distinguished from a new response by this protocol.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Self

import can

from .errors import ProtocolError

logger = logging.getLogger(__name__)


class CanTransport:
    """Own an open ``python-can`` bus and serialize complete transactions.

    Creating this object transfers responsibility for closing ``bus`` to it.
    No background receiver, retries, reconnection, or operating-system network
    configuration are performed. Adapter ``send``/``recv`` methods must honor
    their timeout arguments; the supplied gs_usb backend does so to USB's
    millisecond granularity.
    """

    _MAX_DRAIN_FRAMES = 256
    _MAX_DRAIN_SECONDS = 0.020

    def __init__(self, bus: can.BusABC) -> None:
        self._bus = bus
        self._lock = threading.Lock()
        self._open = True

    @classmethod
    def socketcan(cls, channel: str = "can0", **options: Any) -> Self:
        """Open an already configured Linux CAN interface.

        The caller configures bitrate and brings up the interface separately.
        Ambient ``python-can`` configuration is deliberately ignored.
        """
        options["ignore_config"] = True
        return cls(can.Bus(interface="socketcan", channel=channel, **options))

    @classmethod
    def gs_usb(cls, channel: int = 0, bitrate: int = 1_000_000, **options: Any) -> Self:
        """Open one directly attached USB adapter implementing gs_usb.

        Select by nonnegative ``channel``/``index``, or an explicit USB
        ``bus`` and ``address`` pair. Invalid selections fail before USB I/O.
        """
        cls._integer_option("channel", channel, minimum=0)
        cls._integer_option("bitrate", bitrate, minimum=1)
        if "index" in options:
            cls._integer_option("index", options["index"], minimum=0)
        has_bus = "bus" in options
        has_address = "address" in options
        if has_bus != has_address:
            raise ValueError("USB bus and address must be supplied together")
        if has_bus:
            cls._integer_option("bus", options["bus"], minimum=0)
            cls._integer_option("address", options["address"], minimum=0)
            if "index" in options:
                raise ValueError("USB index cannot be combined with bus/address")
        from .backends.gs_usb import GsUsbBus

        options["ignore_config"] = True
        return cls(GsUsbBus(channel=channel, bitrate=bitrate, **options))

    @staticmethod
    def _integer_option(name: str, value: object, *, minimum: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} must be an integer greater than or equal to {minimum}")

    @classmethod
    def virtual(cls, channel: str, **options: Any) -> Self:
        """Open an in-process virtual CAN bus for examples and tests."""
        options["ignore_config"] = True
        return cls(can.Bus(interface="virtual", channel=channel, **options))

    @property
    def is_open(self) -> bool:
        """Whether this transport has not been closed or failed."""
        return self._open

    def close(self) -> None:
        """Wait for an active request and close the owned bus once."""
        with self._lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        if self._open:
            self._open = False
            self._bus.shutdown()

    def __enter__(self) -> Self:
        if not self._open:
            raise can.CanOperationError("CAN transport is closed")
        return self

    def __exit__(self, *exc_info: object) -> None:
        if exc_info and exc_info[0] is not None:
            try:
                self.close()
            except Exception:
                logger.debug("Could not close CAN bus after an error", exc_info=True)
        else:
            self.close()

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise can.CanTimeoutError("CAN request deadline expired")
        return remaining

    def _drain(self, deadline: float) -> None:
        drain_deadline = min(deadline, time.monotonic() + self._MAX_DRAIN_SECONDS)
        for _ in range(self._MAX_DRAIN_FRAMES):
            if time.monotonic() >= drain_deadline:
                break
            if self._bus.recv(timeout=0.0) is None:
                return
        raise can.CanTimeoutError("CAN receive queue did not become quiet before transmission")

    def request(
        self,
        arbitration_id: int,
        payload: bytes,
        *,
        timeout: float = 0.5,
        response_commands: tuple[int, ...] | None = None,
    ) -> bytes:
        """Send exactly once and return a matching eight-byte RX payload.

        ``timeout`` covers waiting for the shared lock, stale-frame draining,
        sending, and receiving. RX frames must be classic standard CAN data
        frames with the requested ID and command byte. USB TX echoes and
        unrelated traffic are discarded. A truncated matching reply raises
        :class:`ProtocolError`. ``CanTimeoutError`` leaves the bus open; an
        adapter ``CanOperationError`` closes it without retrying.

        Before sending, drain at most 256 queued frames for at most 20 ms. If
        the queue does not empty within those limits, fail without sending.
        This consumes traffic for other CAN IDs as well: share this transport
        only with motors using this request/response protocol.
        """
        if isinstance(arbitration_id, bool) or not isinstance(arbitration_id, int) or not 0 <= arbitration_id <= 0x7FF:
            raise ValueError("arbitration_id must be a standard CAN identifier (0..0x7FF)")
        if not isinstance(payload, bytes) or len(payload) != 8:
            raise ValueError("payload must contain exactly eight bytes")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be finite and greater than zero")
        try:
            timeout_seconds = float(timeout)
        except OverflowError as exc:
            raise ValueError("timeout must be finite and greater than zero") from exc
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout must be finite and greater than zero")
        commands = (payload[0],) if response_commands is None else response_commands
        if (
            not isinstance(commands, tuple)
            or not commands
            or any(
                isinstance(command, bool) or not isinstance(command, int) or not 0 <= command <= 255
                for command in commands
            )
        ):
            raise ValueError("response_commands must be a nonempty tuple of command bytes")

        deadline = time.monotonic() + timeout_seconds
        if not self._lock.acquire(timeout=min(threading.TIMEOUT_MAX, self._remaining(deadline))):
            raise can.CanTimeoutError("CAN request timed out waiting for another request")
        try:
            if not self._open:
                raise can.CanOperationError("CAN transport is closed")
            self._drain(deadline)
            self._bus.send(
                can.Message(arbitration_id=arbitration_id, is_extended_id=False, data=payload, check=True),
                timeout=self._remaining(deadline),
            )
            while True:
                message = self._bus.recv(timeout=self._remaining(deadline))
                self._remaining(deadline)
                if message is None:
                    raise can.CanTimeoutError("No matching motor response before the CAN request deadline")
                if (
                    message.arbitration_id != arbitration_id
                    or message.is_extended_id
                    or message.is_fd
                    or message.is_remote_frame
                    or message.is_error_frame
                    or not message.is_rx
                ):
                    continue
                if message.data and message.data[0] not in commands:
                    continue
                if message.dlc != 8 or len(message.data) != 8:
                    raise ProtocolError("Motor reply must contain exactly eight bytes")
                return bytes(message.data)
        except can.CanOperationError:
            try:
                self._close_unlocked()
            except Exception:
                logger.debug("Could not close a failed CAN bus", exc_info=True)
            raise
        finally:
            self._lock.release()
