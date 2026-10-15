"""Pure codec for LK-TECH CAN V2.35 single-motor frames (no I/O or limits).

All multibyte fields are little endian. This codec intentionally exposes
no ROM writes, broadcast commands, or unlimited-speed position commands.
Applications should send commands through Motor, which enforces limits.
"""

import struct
from enum import IntEnum
from fractions import Fraction

from .errors import ProtocolError
from .models import (
    CurrentScale, Encoder, Feedback, Health, PhaseCurrents, PidParameters,
    finite_number, integer_range,
)


class Command(IntEnum):
    READ_PID = 0x30
    WRITE_PID_RAM = 0x31
    READ_ACCELERATION = 0x33
    WRITE_ACCELERATION_RAM = 0x34
    OFF = 0x80
    STOP = 0x81
    RUN = 0x88
    READ_ENCODER = 0x90
    READ_ANGLE = 0x92
    READ_SINGLE_TURN_ANGLE = 0x94
    READ_HEALTH = 0x9A
    CLEAR_ERRORS = 0x9B
    READ_FEEDBACK = 0x9C
    READ_PHASE_CURRENTS = 0x9D
    CURRENT = 0xA1
    SPEED = 0xA2
    POSITION = 0xA4


def arbitration_id(node_id: int) -> int:
    return 0x140 + integer_range("node_id", node_id, 1, 32)


def query(command: Command) -> bytes:
    return bytes([command]) + bytes(7)


def scaled_integer(value: float, factor: float, name: str) -> int:
    """Quantize toward zero so current/speed never grow due to rounding."""
    finite_number(name, value)
    # Decimal strings preserve the API's decimal resolution. Fractions make
    # quantization independent of an application's decimal context/traps.
    return int(Fraction(str(value)) * Fraction(str(factor)))


def current_command(current_a: float, scale: CurrentScale) -> bytes:
    finite_number("current_a", current_a)
    if abs(current_a) > scale.full_scale_a:
        raise ValueError("current_a exceeds protocol current scale")
    raw = int(Fraction(str(current_a)) * 2048 / Fraction(str(scale.full_scale_a)))
    return struct.pack("<B3xh2x", Command.CURRENT, raw)


def speed_command(speed_dps: float) -> bytes:
    raw = scaled_integer(speed_dps, 100, "speed_dps")
    integer_range("speed raw value", raw, -(2**31), 2**31 - 1)
    return struct.pack("<B3xi", Command.SPEED, raw)


def position_command(position_deg: float, max_speed_dps: float) -> bytes:
    raw_angle = scaled_integer(position_deg, 100, "position_deg")
    raw_speed = scaled_integer(max_speed_dps, 1, "max_speed_dps")
    integer_range("position raw value", raw_angle, -(2**31), 2**31 - 1)
    integer_range("max_speed_dps after quantization", raw_speed, 1, 65535)
    return struct.pack("<BxHi", Command.POSITION, raw_speed, raw_angle)


def acceleration_command(acceleration_dps2: int) -> bytes:
    integer_range("acceleration_dps2", acceleration_dps2, 1, 2**31 - 1)
    return struct.pack("<B3xi", Command.WRITE_ACCELERATION_RAM, acceleration_dps2)


def pid_command(parameters: PidParameters) -> bytes:
    return bytes([Command.WRITE_PID_RAM, 0]) + parameters.to_bytes()


def validate_reply(payload: bytes, *commands: Command) -> bytes:
    if len(payload) != 8:
        raise ProtocolError(f"Expected 8 response bytes, received {len(payload)}")
    if payload[0] not in commands:
        raise ProtocolError(f"Unexpected response command 0x{payload[0]:02X}")
    return payload


def decode_health(payload: bytes) -> Health:
    validate_reply(payload, Command.READ_HEALTH, Command.CLEAR_ERRORS)
    return Health(struct.unpack_from("<b", payload, 1)[0],
                  struct.unpack_from("<H", payload, 3)[0] / 10, payload[7])


def decode_feedback(payload: bytes, scale: CurrentScale) -> Feedback:
    validate_reply(payload, Command.CURRENT, Command.SPEED, Command.POSITION, Command.READ_FEEDBACK)
    temperature, raw_current, speed, encoder = struct.unpack_from("<bhhH", payload, 1)
    if not -2048 <= raw_current <= 2048:
        raise ProtocolError(f"Current feedback {raw_current} exceeds V2.35 range ±2048")
    full_scale = scale.status_full_scale_a if payload[0] == Command.READ_FEEDBACK else scale.full_scale_a
    return Feedback(temperature, (raw_current / 2048) * full_scale, speed, encoder)


def decode_angle(payload: bytes, *, single_turn: bool = False) -> float:
    if single_turn:
        validate_reply(payload, Command.READ_SINGLE_TURN_ANGLE)
        raw = int.from_bytes(payload[4:8], "little")
        if raw >= 36000:
            raise ProtocolError("Single-turn angle must be in [0, 360) degrees")
    else:
        validate_reply(payload, Command.READ_ANGLE)
        raw = int.from_bytes(payload[1:8], "little", signed=True)
    return raw / 100


def decode_encoder(payload: bytes) -> Encoder:
    validate_reply(payload, Command.READ_ENCODER)
    return Encoder(*struct.unpack_from("<HHH", payload, 2))


def decode_phase_currents(payload: bytes) -> PhaseCurrents:
    validate_reply(payload, Command.READ_PHASE_CURRENTS)
    temperature, a, b, c = struct.unpack_from("<bhhh", payload, 1)
    return PhaseCurrents(temperature, a / 64, b / 64, c / 64)


def decode_pid(payload: bytes) -> PidParameters:
    validate_reply(payload, Command.READ_PID, Command.WRITE_PID_RAM)
    return PidParameters(*payload[2:8])


def decode_acceleration(payload: bytes) -> int:
    validate_reply(payload, Command.READ_ACCELERATION, Command.WRITE_ACCELERATION_RAM)
    return struct.unpack_from("<i", payload, 4)[0]
