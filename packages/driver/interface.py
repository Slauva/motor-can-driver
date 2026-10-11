import logging
import os
import subprocess
from dataclasses import dataclass
from math import isfinite
from time import monotonic
from typing import Any

import can

logger = logging.getLogger(__name__)


@dataclass
class Response:
    id: int
    data: list[int]


class CanBus:
    def __init__(
        self,
        channel: str | int | None = None,
        bustype: str = "socketcan",
        baudrate: int = 115200,
        bitrate: int = 1000000,
        **backend_options: Any,
    ) -> None:
        """Open a legacy SLCAN adapter or a direct USB backend such as gs_usb.

        For gs_usb, channel is the zero-based adapter index. Extra options
        (such as index, bus, and address) are passed to python-can.
        """
        self._is_open = False
        self.bus: can.BusABC
        self.port: str | None = None
        self.channel = channel if channel is not None else (0 if bustype == "gs_usb" else "can0")
        self.bustype = bustype
        self.baudrate = baudrate
        self.bitrate = bitrate
        self._backend_options = backend_options
        self.open()

    @property
    def is_open(self) -> bool:
        return self._is_open

    def open(self) -> None:
        if self._is_open:
            return

        if self.bustype == "socketcan":
            self.port = subprocess.getoutput("ls /dev/ttyACM*")
            os.system(f"sudo slcand -o -c -s8 -S {self.baudrate} {self.port} {self.channel}")
            os.system(f"sudo ifconfig {self.channel} down")
            os.system(f"sudo ip link set {self.channel} type can bitrate {self.bitrate}")
            os.system(f"sudo ifconfig {self.channel} up")
            os.system(f"sudo ifconfig {self.channel} txqueuelen 65536")

        if self.bustype == "gs_usb":
            from .gs_usb import GsUsbBus

            self.bus = GsUsbBus(
                channel=self.channel,
                bitrate=self.bitrate,
                **self._backend_options,
            )
        else:
            self.bus = can.Bus(
                interface=self.bustype,
                channel=self.channel,
                bitrate=self.bitrate,
                **self._backend_options,
            )
        self._is_open = True

    def close(self) -> None:
        if not self._is_open:
            return

        self._is_open = False
        try:
            self.bus.shutdown()
        finally:
            if self.bustype == "socketcan":
                os.system(f"sudo ifconfig {self.channel} down")

    def _close_after_failure(self) -> None:
        try:
            self.close()
        except Exception:
            logger.debug("CAN cleanup failed after a transport error", exc_info=True)

    def send(self, message: can.Message, timeout: float | None = None) -> None:
        if not self.is_open:
            raise can.CanOperationError("CAN connection is closed; call open() to reconnect")
        try:
            if timeout is None:
                self.bus.send(message)
            else:
                self.bus.send(message, timeout=timeout)
        except can.CanOperationError:
            self._close_after_failure()
            raise

    def recv(self, timeout: float | None = None) -> can.Message | None:
        if not self.is_open:
            raise can.CanOperationError("CAN connection is closed; call open() to reconnect")
        try:
            return self.bus.recv(timeout)
        except can.CanOperationError:
            self._close_after_failure()
            raise

    def request(self, arbitration_id: int, data: list[int], timeout: float = 1, n_packets: int = 1) -> list[Response]:
        """Send once and collect responses within a total timeout in seconds."""
        if not isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and greater than zero")
        if not isinstance(n_packets, int) or n_packets < 1:
            raise ValueError("n_packets must be a positive integer")
        msg = can.Message(arbitration_id=arbitration_id, dlc=len(data), data=data, is_extended_id=False)
        deadline = monotonic() + timeout
        self.send(msg)
        responses: list[Response] = []

        while len(responses) < n_packets:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise can.CanTimeoutError(f"Received {len(responses)}/{n_packets} CAN responses within {timeout:g} s")
            response = self.recv(min(0.1, remaining))
            if response and response.is_rx:
                responses.append(Response(response.arbitration_id, list(response.data)))

        return responses

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            # Explicit close() reports shutdown errors; finalizers cannot.
            pass
