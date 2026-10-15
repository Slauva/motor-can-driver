"""Errors raised above the python-can transport layer."""


class ProtocolError(RuntimeError):
    """The device returned a malformed or inconsistent protocol response."""


class SafetyError(RuntimeError):
    """A command was rejected by a host safety check before transmission."""


class LimitError(SafetyError, ValueError):
    """A requested value exceeds a configured limit."""


class MotorFaultError(SafetyError):
    """Fresh device telemetry prevents further motion commands."""
