from time import sleep

from packages.driver import CanBus, Gyems

bus = CanBus()
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
    motor.disable(True)
    bus.close()
