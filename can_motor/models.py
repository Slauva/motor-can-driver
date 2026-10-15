"""Immutable configuration and telemetry, with units in field names."""

from dataclasses import dataclass

from .units import dps_to_rpm, finite_number, rpm_to_dps


def integer_range(name: str, value: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


@dataclass(frozen=True, slots=True)
class CurrentScale:
    """Amperes at raw ±2048; status 0x9C has a separately documented scale.

    V2.35 p.7: MF=16.5 A, MG=33 A for control replies. Page 19 describes
    status 0x9C as 33 A without distinguishing series. Verify both against
    the exact device firmware. MH has no specified scale in this document.
    """

    full_scale_a: float
    status_full_scale_a: float = 33.0

    def __post_init__(self) -> None:
        for name in ("full_scale_a", "status_full_scale_a"):
            if finite_number(name, getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")


MF_CURRENT_SCALE = CurrentScale(16.5)
MG_CURRENT_SCALE = CurrentScale(33.0)


@dataclass(frozen=True, slots=True, init=False)
class MotorLimits:
    """Host limits; defaults are starting values, never device ratings.

    firmware_current_limit_a is a declaration of a limit configured and
    checked externally. V2.35 cannot set/read it through these commands.
    Speed and position control stay unavailable until it is supplied.
    Specify the speed limit in either degrees/second or RPM. Both names
    refer to the same bound; the default is 30 °/s, equivalent to 5 RPM.
    """

    max_current_a: float
    max_speed_dps: float
    min_position_deg: float
    max_position_deg: float
    max_acceleration_dps2: int
    max_temperature_c: float
    min_voltage_v: float | None
    max_voltage_v: float | None
    firmware_current_limit_a: float | None

    def __init__(
        self,
        max_current_a: float = 1.0,
        max_speed_dps: float | None = None,
        min_position_deg: float = -180.0,
        max_position_deg: float = 180.0,
        max_acceleration_dps2: int = 100,
        max_temperature_c: float = 60.0,
        min_voltage_v: float | None = None,
        max_voltage_v: float | None = None,
        firmware_current_limit_a: float | None = None,
        *,
        max_speed_rpm: float | None = None,
    ) -> None:
        if max_speed_dps is not None and max_speed_rpm is not None:
            raise ValueError("Specify only one of max_speed_dps and max_speed_rpm")
        if max_speed_rpm is not None:
            max_speed_dps = rpm_to_dps(max_speed_rpm)
        elif max_speed_dps is None:
            max_speed_dps = 30.0
        values = {
            "max_current_a": max_current_a,
            "max_speed_dps": max_speed_dps,
            "min_position_deg": min_position_deg,
            "max_position_deg": max_position_deg,
            "max_acceleration_dps2": max_acceleration_dps2,
            "max_temperature_c": max_temperature_c,
            "min_voltage_v": min_voltage_v,
            "max_voltage_v": max_voltage_v,
            "firmware_current_limit_a": firmware_current_limit_a,
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        self.__post_init__()

    @property
    def max_speed_rpm(self) -> float:
        return dps_to_rpm(self.max_speed_dps)

    def __post_init__(self) -> None:
        for name in ("max_current_a", "max_speed_dps"):
            if finite_number(name, getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_speed_dps > 65535:
            raise ValueError("max_speed_dps exceeds the 0xA4 uint16 speed field")
        integer_range("max_acceleration_dps2", self.max_acceleration_dps2, 1, 2**31 - 1)
        for name in ("min_position_deg", "max_position_deg", "max_temperature_c"):
            finite_number(name, getattr(self, name))
        if not -128 <= self.max_temperature_c <= 127:
            raise ValueError("max_temperature_c must fit the signed temperature telemetry")
        if not -(2**31) / 100 <= self.min_position_deg < self.max_position_deg <= (2**31 - 1) / 100:
            raise ValueError("position limits must be ordered and fit the signed 0.01 degree field")
        for name in ("min_voltage_v", "max_voltage_v", "firmware_current_limit_a"):
            value = getattr(self, name)
            if value is not None and finite_number(name, value) <= 0:
                raise ValueError(f"{name} must be positive when supplied")
        if self.min_voltage_v is not None and self.max_voltage_v is not None:
            if self.min_voltage_v >= self.max_voltage_v:
                raise ValueError("min_voltage_v must be less than max_voltage_v")
        if self.firmware_current_limit_a is not None and self.firmware_current_limit_a > self.max_current_a:
            raise ValueError("firmware_current_limit_a must not exceed max_current_a")


@dataclass(frozen=True, slots=True)
class Health:
    temperature_c: int
    voltage_v: float
    error_flags: int

    @property
    def undervoltage(self) -> bool:
        return bool(self.error_flags & 0x01)

    @property
    def overtemperature(self) -> bool:
        return bool(self.error_flags & 0x08)

    @property
    def has_fault(self) -> bool:
        # Unknown bits also prevent motion: another firmware may define them.
        return self.error_flags != 0


@dataclass(frozen=True, slots=True)
class Feedback:
    temperature_c: int
    current_a: float
    speed_dps: int
    encoder_raw: int

    @property
    def speed_rpm(self) -> float:
        """Derived speed in revolutions/minute; wire telemetry remains in °/s."""
        return dps_to_rpm(self.speed_dps)


@dataclass(frozen=True, slots=True)
class Encoder:
    position_raw: int
    original_raw: int
    offset_raw: int


@dataclass(frozen=True, slots=True)
class PhaseCurrents:
    temperature_c: int
    phase_a_a: float
    phase_b_a: float
    phase_c_a: float


@dataclass(frozen=True, slots=True)
class PidParameters:
    """Six raw uint8 coefficients; the protocol supplies no physical gain scale."""

    position_kp: int
    position_ki: int
    speed_kp: int
    speed_ki: int
    current_kp: int
    current_ki: int

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            integer_range(name, getattr(self, name), 0, 255)

    def to_bytes(self) -> bytes:
        return bytes(getattr(self, name) for name in self.__dataclass_fields__)
