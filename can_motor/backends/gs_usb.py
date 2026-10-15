"""gs_usb backend with explicit cleanup when a USB adapter disappears."""

import logging
import math
from typing import Any, NoReturn

import can
import usb.core
import usb.util
from can.interfaces.gs_usb import GsUsbBus as PythonCanGsUsbBus
from gs_usb.constants import CAN_EFF_FLAG, CAN_ERR_FLAG, CAN_MAX_DLC, CAN_RTR_FLAG, GS_CAN_MODE_HW_TIMESTAMP
from gs_usb.gs_usb_frame import GS_USB_NONE_ECHO_ID, GsUsbFrame

logger = logging.getLogger(__name__)


class GsUsbBus(PythonCanGsUsbBus):
    """Keep a failed connection closed and never rescan devices at shutdown."""

    def __init__(self, channel: str | int, **kwargs: Any) -> None:
        self._periodic_tasks = []
        self._usb_failed = False
        self._usb_started = False
        self._usb_cleanup_done = False

        try:
            super().__init__(channel=channel, **kwargs)
            self._usb_started = True
            raw_device = self.gs_usb.gs_usb
            interface = raw_device.get_active_configuration()[(0, 0)]
            endpoint = usb.util.find_descriptor(interface, bEndpointAddress=0x81)
            if endpoint is None:
                raise can.CanInitializationError("gs_usb receive endpoint 0x81 is missing")

            packet_size = endpoint.wMaxPacketSize & 0x7FF
            if packet_size == 0:
                raise can.CanInitializationError("gs_usb receive endpoint has an invalid packet size")

            self._hw_timestamps = bool(self.gs_usb.device_flags & GS_CAN_MODE_HW_TIMESTAMP)
            self._frame_size = GsUsbFrame().__sizeof__(self._hw_timestamps)
            # A bulk receive buffer must hold a whole USB endpoint packet.
            self._read_size = ((self._frame_size + packet_size - 1) // packet_size) * packet_size
        except usb.core.USBError as exc:
            self._usb_failed = True
            self._close_after_failure()
            raise can.CanInitializationError(f"Cannot open gs_usb adapter: {exc}") from exc
        except BaseException:
            self._close_after_failure()
            raise

    def shutdown(self) -> None:
        if self._usb_cleanup_done:
            return

        self._usb_cleanup_done = True
        try:
            # The upstream shutdown rescans and starts another device by index.
            can.BusABC.shutdown(self)
            device = getattr(self, "gs_usb", None)
            if device is not None and self._usb_started and not self._usb_failed:
                try:
                    device.stop()
                except usb.core.USBError:
                    logger.debug("gs_usb adapter disappeared while stopping", exc_info=True)
        finally:
            self._is_shutdown = True
            self._usb_started = False
            device = getattr(self, "gs_usb", None)
            if device is not None:
                try:
                    usb.util.dispose_resources(device.gs_usb)
                except usb.core.USBError:
                    logger.debug("gs_usb adapter disappeared while releasing USB resources", exc_info=True)

    def _close_after_failure(self) -> None:
        try:
            self.shutdown()
        except Exception:
            # Preserve the original failure if best-effort cleanup also fails.
            logger.debug("Could not release a failed gs_usb connection", exc_info=True)

    def _require_open(self) -> None:
        if self._is_shutdown:
            raise can.CanOperationError("gs_usb connection is closed; open a new connection to the adapter")

    def _raise_usb_error(self, operation: str, exc: usb.core.USBError) -> NoReturn:
        self._usb_failed = True
        self._close_after_failure()
        raise can.CanOperationError(f"gs_usb {operation} failed: {exc}") from exc

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        """Write one classic frame with a bounded USB bulk transfer.

        The upstream backend ignores ``timeout``. PyUSB uses milliseconds;
        positive timeouts are rounded up to the next millisecond. ``None``
        retains the usual unbounded Bus API meaning. A USB timeout leaves the
        connection open and does not retry: the frame may already be on CAN.
        """
        self._require_open()
        if msg.is_fd or not 0 <= msg.dlc <= CAN_MAX_DLC or len(msg.data) > CAN_MAX_DLC:
            raise can.CanOperationError("gs_usb supports classic CAN frames with at most eight data bytes")
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("timeout must be finite and nonnegative, or None")
        timeout_ms = 0 if timeout is None else max(1, math.ceil(min(timeout, 0xFFFFFFFF / 1000) * 1000))
        can_id = msg.arbitration_id
        if msg.is_extended_id:
            can_id |= CAN_EFF_FLAG
        if msg.is_remote_frame:
            can_id |= CAN_RTR_FLAG
        if msg.is_error_frame:
            can_id |= CAN_ERR_FLAG
        frame = GsUsbFrame(can_id=can_id, data=list(msg.data))
        frame.can_dlc = msg.dlc
        data = frame.pack(self._hw_timestamps)
        try:
            written = self.gs_usb.gs_usb.write(0x02, data, timeout=timeout_ms)
        except usb.core.USBTimeoutError as exc:
            raise can.CanTimeoutError("gs_usb send timed out; delivery is uncertain") from exc
        except usb.core.USBError as exc:
            self._raise_usb_error("send", exc)
        if written != len(data):
            self._usb_failed = True
            self._close_after_failure()
            raise can.CanOperationError(f"Incomplete gs_usb write: expected {len(data)} bytes, wrote {written}")

    def _recv_internal(self, timeout: float | None) -> tuple[can.Message | None, bool]:
        self._require_open()
        timeout_ms = max(1, math.ceil(min(timeout, 0xFFFFFFFF / 1000) * 1000)) if timeout else 1
        try:
            data = self.gs_usb.gs_usb.read(0x81, self._read_size, timeout=timeout_ms)
        except usb.core.USBTimeoutError:
            return None, False
        except usb.core.USBError as exc:
            self._raise_usb_error("receive", exc)

        if len(data) != self._frame_size:
            self._usb_failed = True
            self._close_after_failure()
            raise can.CanOperationError(
                f"Invalid gs_usb frame length: expected {self._frame_size} bytes, received {len(data)}"
            )

        frame = GsUsbFrame()
        GsUsbFrame.unpack_into(frame, data, self._hw_timestamps)
        if frame.can_dlc > CAN_MAX_DLC:
            self._usb_failed = True
            self._close_after_failure()
            raise can.CanOperationError(f"Invalid gs_usb classic CAN frame DLC: {frame.can_dlc}")

        return can.Message(
            timestamp=frame.timestamp,
            arbitration_id=frame.arbitration_id,
            is_extended_id=frame.is_extended_id,
            is_remote_frame=frame.is_remote_frame,
            is_error_frame=frame.is_error_frame,
            channel=self.channel_info,
            dlc=frame.can_dlc,
            data=bytearray(frame.data[: frame.can_dlc]),
            is_rx=frame.echo_id == GS_USB_NONE_ECHO_ID,
        ), False
