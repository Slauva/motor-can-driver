"""Safe high-level control of motors using the LK-TECH CAN V2.35 protocol."""

from .errors import LimitError, MotorFaultError, ProtocolError, SafetyError
from .models import (
    MF_CURRENT_SCALE, MG_CURRENT_SCALE, CurrentScale, Encoder, Feedback, Health,
    MotorLimits, PhaseCurrents, PidParameters,
)
from .motor import Motor
from .transport import CanTransport
from .units import dps_to_rpm, rpm_to_dps

__all__ = [
    "CanTransport", "CurrentScale", "Encoder", "Feedback", "Health", "LimitError",
    "MF_CURRENT_SCALE", "MG_CURRENT_SCALE", "Motor", "MotorFaultError", "MotorLimits",
    "PhaseCurrents", "PidParameters", "ProtocolError", "SafetyError", "dps_to_rpm", "rpm_to_dps",
]
