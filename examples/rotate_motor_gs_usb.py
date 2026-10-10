"""Rotate a motor through a CandleLight USB adapter using gs_usb."""

from time import sleep

from packages.driver import CanBus, Gyems


def main() -> None:
    bus = CanBus(channel=0, bustype="gs_usb", bitrate=1_000_000)
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
