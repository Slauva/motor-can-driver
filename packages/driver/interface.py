import os
import subprocess
from dataclasses import dataclass
from time import time
from typing import Any

import can


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

    def request(self, arbitration_id: int, data: list[int], timeout: float = 1, n_packets: int = 1) -> list[Response]:
        msg = can.Message(arbitration_id=arbitration_id, dlc=len(data), data=data, is_extended_id=False)

        self.bus.send(msg)
        ans = []
        st_time = time()
        while True:
            if time() - st_time >= timeout:
                self.bus.send(msg)
                st_time = time()
            recv = self.bus.recv(0.1)

            if recv and recv.is_rx:
                ans.append(recv)

            if len(ans) == n_packets:
                break

        res = []
        for resp in ans:
            idx = resp.arbitration_id
            data = resp.data
            res.append(Response(idx, list(data)))
        return res

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            # Explicit close() reports shutdown errors; finalizers cannot.
            pass
