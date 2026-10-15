"""Speed conversions for the same shaft; no gearbox ratio is applied."""

from fractions import Fraction
from math import isfinite


def finite_number(name: str, value: float) -> float:
    """Reject booleans, NaN and infinity as well as nonnumeric inputs."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        valid = isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be a finite number")
    return value


def rpm_to_dps(speed_rpm: float) -> float:
    """Convert signed revolutions/minute to degrees/second: 1 RPM = 6 °/s."""
    finite_number("speed_rpm", speed_rpm)
    try:
        speed_dps = float(Fraction(str(speed_rpm)) * 6)
    except OverflowError as error:
        raise ValueError("converted speed_dps must be a finite number") from error
    return finite_number("converted speed_dps", speed_dps)


def dps_to_rpm(speed_dps: float) -> float:
    """Convert signed degrees/second to revolutions/minute."""
    return finite_number("speed_dps", speed_dps) / 6
