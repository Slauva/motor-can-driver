"""A deterministic protocol responder on python-can's in-process virtual bus.

Run ``python -m examples.virtual_motor`` without CAN hardware or privileges.
This demonstrates frame exchange only; it does not model motor dynamics.
"""

import struct
from threading import Event, Thread
from uuid import uuid4

import can

from can_motor import MG_CURRENT_SCALE, CanTransport, Motor, MotorLimits


def main() -> int:
    channel = f"motor-demo-{uuid4()}"
    stopped = Event()
    errors: list[Exception] = []
    with can.Bus(interface="virtual", channel=channel, ignore_config=True) as peer:
        def respond() -> None:
            current_raw = 0
            acceleration = 100
            try:
                while not stopped.is_set():
                    message = peer.recv(0.05)
                    if message is None:
                        continue
                    command = message.data[0]
                    payload = bytes(message.data)
                    if command == 0x9A:
                        payload = bytes.fromhex("9a 19 00 f0 00 00 00 00")
                    elif command in (0x9C, 0xA1, 0xA2, 0xA4):
                        if command == 0xA1:
                            current_raw = int.from_bytes(payload[4:6], "little", signed=True)
                        payload = struct.pack("<BbhhH", command, 25, current_raw, 0, 0)
                    elif command == 0x92:
                        payload = bytes([command]) + bytes(7)
                    elif command == 0x33:
                        payload = struct.pack("<B3xi", command, acceleration)
                    elif command == 0x34:
                        acceleration = int.from_bytes(payload[4:8], "little", signed=True)
                    elif command == 0x80:
                        current_raw = 0
                    peer.send(can.Message(arbitration_id=message.arbitration_id, is_extended_id=False, data=payload))
            except Exception as error:
                errors.append(error)

        worker = Thread(target=respond, daemon=True)
        worker.start()
        try:
            with CanTransport.virtual(channel) as transport:
                motor = Motor(transport, current_scale=MG_CURRENT_SCALE,
                              limits=MotorLimits(firmware_current_limit_a=1.0))
                print(motor.read_health())
                motor.write_acceleration_ram(30)
                with motor.control():
                    print(motor.set_current(0.2))
                    print(motor.set_speed(5))
                    print(motor.set_position(10, max_speed_dps=5))
        finally:
            stopped.set()
            worker.join(1)
        if worker.is_alive():
            raise RuntimeError("Virtual motor responder did not stop")
        if errors:
            raise errors[0]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
