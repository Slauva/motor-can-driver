"""Rotate a motor through an SLCAN serial adapter configured as SocketCAN.

CanBus finds /dev/ttyACM* and creates can0 using slcand.
"""

import sys
from time import sleep

sys.dont_write_bytecode = True

from can import CanError

from packages.driver import CanBus, Gyems


def main() -> int:
    bus: CanBus | None = None
    motor: Gyems | None = None
    exit_code = 0
    try:
        bus = CanBus(
            channel="can0",
            bustype="socketcan",
            baudrate=115200,
            bitrate=1_000_000,
        )
        motor = Gyems(bus)
        motor.enable()
        motor.set_zero()
        motor.set_speed(-360)
        while True:
            print(motor.info())
            sleep(0.1)
    except KeyboardInterrupt:
        pass
    except CanError as error:
        print(f"Ошибка CAN: {error}", file=sys.stderr)
        exit_code = 1
    finally:
        if bus is not None:
            try:
                if motor is not None and bus.is_open:
                    motor.disable(True)
            except CanError as error:
                print(f"Не удалось выключить двигатель: {error}", file=sys.stderr)
                exit_code = 1
            finally:
                try:
                    bus.close()
                except CanError as error:
                    print(f"Не удалось закрыть CAN: {error}", file=sys.stderr)
                    exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
