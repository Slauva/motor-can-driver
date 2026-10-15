"""Read telemetry by default; request a short current test explicitly."""

import argparse
import math
import sys
from time import monotonic, sleep

import can

from can_motor import (
    MF_CURRENT_SCALE,
    MG_CURRENT_SCALE,
    CanTransport,
    Motor,
    MotorLimits,
    ProtocolError,
    SafetyError,
)


def positive_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("expected a finite positive number")
    return number


def finite_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("expected a finite number")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--interface", choices=("socketcan", "gs_usb"), required=True)
    result.add_argument("--channel", help="SocketCAN interface name or USB adapter index")
    result.add_argument("--node-id", type=int, choices=range(1, 33), default=1)
    result.add_argument(
        "--current-scale",
        choices=("mf", "mg"),
        required=True,
        help="verified device family: MF=16.5 A, MG=33 A at raw 2048",
    )
    result.add_argument(
        "--bitrate",
        type=int,
        choices=(100000, 125000, 250000, 500000, 1000000),
        default=1000000,
        help="CAN bitrate for direct gs_usb; SocketCAN uses system configuration",
    )
    result.add_argument("--max-current-a", type=positive_number, default=1.0)
    speed_limit = result.add_mutually_exclusive_group()
    speed_limit.add_argument("--max-speed-dps", type=positive_number,
                             help="maximum speed in degrees/second (default: 30 °/s = 5 RPM)")
    speed_limit.add_argument("--max-speed-rpm", type=positive_number,
                             help="maximum speed in revolutions/minute (alternative to --max-speed-dps)")
    result.add_argument(
        "--current-a", type=finite_number, help="explicitly enable a current test; default is telemetry only"
    )
    result.add_argument("--duration", type=positive_number, default=2.0, help="test duration in seconds")
    return result


def main(argv: list[str] | None = None) -> int:
    arguments_parser = parser()
    args = arguments_parser.parse_args(argv)
    try:
        limits = MotorLimits(
            max_current_a=args.max_current_a,
            max_speed_dps=args.max_speed_dps,
            max_speed_rpm=args.max_speed_rpm,
        )
        scale = MF_CURRENT_SCALE if args.current_scale == "mf" else MG_CURRENT_SCALE
        if limits.max_current_a > scale.full_scale_a:
            raise ValueError("max-current-a exceeds the selected protocol scale")
        # Validate before opening any hardware or enabling the motor.
        if args.current_a is not None and abs(args.current_a) > limits.max_current_a:
            raise ValueError("requested current exceeds max-current-a")
        if args.interface == "gs_usb":
            channel = int(args.channel) if args.channel is not None else 0
            if channel < 0:
                raise ValueError("USB adapter index must be nonnegative")
        else:
            channel = args.channel or "can0"
    except ValueError as error:
        arguments_parser.error(str(error))

    try:
        connection = (
            CanTransport.gs_usb(channel=channel, bitrate=args.bitrate)
            if args.interface == "gs_usb"
            else CanTransport.socketcan(channel=channel)
        )
        with connection as transport:
            motor = Motor(transport, node_id=args.node_id, current_scale=scale, limits=limits)
            print(motor.read_health())
            print(motor.read_feedback())
            print(f"angle_deg={motor.read_angle():.2f}")
            if args.current_a is not None:
                with motor.control():
                    deadline = monotonic() + args.duration
                    while monotonic() < deadline:
                        # Each update checks fresh health and returned feedback.
                        print(motor.set_current(args.current_a))
                        sleep(min(0.1, max(0, deadline - monotonic())))
        return 0
    except KeyboardInterrupt:
        print("Прервано пользователем", file=sys.stderr)
        return 130
    except (can.CanError, ProtocolError, SafetyError, OSError) as error:
        print(f"Ошибка теста двигателя: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
