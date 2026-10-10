"""Rotate a motor through an SLCAN serial adapter configured as SocketCAN.

CanBus finds /dev/ttyACM* and creates can0 using slcand.
"""

from time import sleep

from packages.driver import CanBus, Gyems


def main() -> None:
    bus = CanBus(
        channel="can0",
        bustype="socketcan",
        baudrate=115200,
        bitrate=1_000_000,
    )
    motor = Gyems(bus)

    try:
        motor.enable()
        motor.set_zero()
        motor.set_speed(-360)
        while True:
            print(motor.info())
            sleep(0.1)
    except KeyboardInterrupt:
        pass

    finally:
        try:
            motor.disable(True)
        finally:
            bus.close()


if __name__ == "__main__":
    main()
