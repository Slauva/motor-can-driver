"""Transport-independent motor API with host-side command and health limits."""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import RLock
from typing import TypeVar

import can

from . import protocol
from .errors import LimitError, MotorFaultError, ProtocolError, SafetyError
from .models import (
    CurrentScale,
    Encoder,
    Feedback,
    Health,
    MotorLimits,
    PhaseCurrents,
    PidParameters,
    finite_number,
)
from .protocol import Command
from .transport import CanTransport
from .units import rpm_to_dps

logger = logging.getLogger(__name__)
T = TypeVar("T")


class Motor:
    """One V2.35 node on a shared CanTransport.

    Construction never sends CAN frames. ``enabled`` describes permission
    in this Python object, not a measured device state. Use one Motor per
    node and one shared transport per physical bus. Values refer to the
    protocol shaft; no gearbox conversion or torque conversion is applied.
    """

    def __init__(
        self,
        transport: CanTransport,
        *,
        node_id: int = 1,
        current_scale: CurrentScale,
        limits: MotorLimits | None = None,
        timeout: float = 0.5,
    ) -> None:
        self._arbitration_id = protocol.arbitration_id(node_id)
        if finite_number("timeout", timeout) <= 0:
            raise ValueError("timeout must be positive")
        self._limits = limits if limits is not None else MotorLimits()
        if self._limits.max_current_a > current_scale.full_scale_a:
            raise ValueError("max_current_a must not exceed current_scale.full_scale_a")
        self._transport = transport
        self._node_id = node_id
        self._current_scale = current_scale
        self._timeout = timeout
        self._enabled = False
        self._lock = RLock()

    @property
    def enabled(self) -> bool:
        return self._enabled and self._transport.is_open

    @property
    def limits(self) -> MotorLimits:
        return self._limits

    @property
    def node_id(self) -> int:
        return self._node_id

    def _request(self, payload: bytes, *response_commands: Command) -> bytes:
        commands = response_commands or (Command(payload[0]),)
        reply = self._transport.request(
            self._arbitration_id,
            payload,
            timeout=self._timeout,
            response_commands=tuple(int(command) for command in commands),
        )
        return protocol.validate_reply(reply, *commands)

    def _read_decoded(self, command: Command, decoder: Callable[[bytes], T]) -> T:
        was_enabled = self._enabled
        try:
            return decoder(self._request(protocol.query(command)))
        except (can.CanError, ProtocolError):
            if was_enabled:
                self._best_effort_disable()
            raise

    def _acknowledge(self, payload: bytes) -> None:
        if self._request(payload) != payload:
            self._enabled = False
            raise ProtocolError("Device acknowledgement does not echo the command payload")

    def _best_effort_disable(self) -> None:
        with self._lock:
            self._enabled = False
            if not self._transport.is_open:
                return
            try:
                self._acknowledge(protocol.query(Command.OFF))
            except Exception:
                logger.warning("Could not confirm motor %d off", self.node_id, exc_info=True)

    def _health_violation(self, health: Health) -> str | None:
        if health.has_fault:
            return f"device error flags 0x{health.error_flags:02X}"
        if health.temperature_c >= self.limits.max_temperature_c:
            return f"temperature {health.temperature_c} °C reached host limit"
        if self.limits.min_voltage_v is not None and health.voltage_v < self.limits.min_voltage_v:
            return f"voltage {health.voltage_v:g} V is below host limit"
        if self.limits.max_voltage_v is not None and health.voltage_v > self.limits.max_voltage_v:
            return f"voltage {health.voltage_v:g} V exceeds host limit"
        return None

    def _check_health(self) -> None:
        health = protocol.decode_health(self._request(protocol.query(Command.READ_HEALTH)))
        reason = self._health_violation(health)
        if reason:
            raise MotorFaultError(reason)

    def _check_feedback(self, feedback: Feedback) -> None:
        if feedback.temperature_c >= self.limits.max_temperature_c:
            raise MotorFaultError("Feedback temperature reached host limit")
        if abs(feedback.current_a) > self.limits.max_current_a:
            raise MotorFaultError("Measured current exceeds host limit")
        if abs(feedback.speed_dps) > self.limits.max_speed_dps:
            raise MotorFaultError("Measured speed exceeds host limit")

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise SafetyError("Call enable() before requesting motion")

    def _require_disarmed(self) -> None:
        if self._enabled:
            raise SafetyError("Call disable() before changing controller parameters")

    def _check_closed_loop_configuration(self) -> None:
        if self.limits.firmware_current_limit_a is None:
            raise SafetyError(
                "Speed/position require an externally verified firmware current limit; "
                "set MotorLimits.firmware_current_limit_a after checking the device"
            )

    def _motion(self, payload: bytes, *, check_acceleration: bool = False) -> Feedback:
        self._require_enabled()
        try:
            self._check_health()
            if check_acceleration:
                acceleration = protocol.decode_acceleration(self._request(protocol.query(Command.READ_ACCELERATION)))
                if not 1 <= acceleration <= self.limits.max_acceleration_dps2:
                    raise MotorFaultError("Device acceleration is outside host limits; configure it while disabled")
            feedback = protocol.decode_feedback(self._request(payload), self._current_scale)
            self._check_feedback(feedback)
            return feedback
        except (can.CanError, ProtocolError, MotorFaultError):
            self._best_effort_disable()
            raise

    def enable(self) -> None:
        """Cancel old commands with OFF, check health, then send RUN.

        OFF clears the device's accumulated turns (V2.35 p.5). Establish
        the position reference after enabling; never assume it survives OFF.
        No fault is cleared and no persistent parameter is written.
        """
        with self._lock:
            self._enabled = False
            try:
                self._acknowledge(protocol.query(Command.OFF))
                self._check_health()
                self._acknowledge(protocol.query(Command.RUN))
                self._enabled = True
            except (can.CanError, ProtocolError, MotorFaultError):
                self._best_effort_disable()
                raise

    def disable(self) -> None:
        """Send OFF even if logically disabled; device turns/reference are cleared."""
        with self._lock:
            self._enabled = False
            self._acknowledge(protocol.query(Command.OFF))

    def stop(self) -> None:
        """Send STOP without clearing device turns, and revoke local motion permission."""
        with self._lock:
            self._enabled = False
            try:
                self._acknowledge(protocol.query(Command.STOP))
            except (can.CanError, ProtocolError):
                self._best_effort_disable()
                raise

    @contextmanager
    def control(self) -> Iterator["Motor"]:
        """Enable for a block, always attempt OFF, preserve an original exception.

        This cannot stop the motor over a disconnected bus or after process
        termination. A hardware stop and device watchdog remain independent.
        """
        with self._lock:
            self._require_disarmed()
        try:
            self.enable()
            yield self
        except BaseException:
            self._best_effort_disable()
            raise
        else:
            self.disable()

    def set_current(self, current_a: float) -> Feedback:
        """Set torque-producing current in A, quantized toward zero at ±2048 scale."""
        finite_number("current_a", current_a)
        if abs(current_a) > self.limits.max_current_a:
            raise LimitError(f"|current_a| must not exceed {self.limits.max_current_a:g} A")
        payload = protocol.current_command(current_a, self._current_scale)
        with self._lock:
            return self._motion(payload)

    def set_speed(self, speed_dps: float) -> Feedback:
        """Set signed degrees/second; firmware must independently cap current."""
        finite_number("speed_dps", speed_dps)
        if abs(speed_dps) > self.limits.max_speed_dps:
            raise LimitError(f"|speed_dps| must not exceed {self.limits.max_speed_dps:g}")
        self._check_closed_loop_configuration()
        payload = protocol.speed_command(speed_dps)
        with self._lock:
            return self._motion(payload, check_acceleration=True)

    def set_speed_rpm(self, speed_rpm: float) -> Feedback:
        """Set signed RPM using the same checks and limits as set_speed()."""
        return self.set_speed(rpm_to_dps(speed_rpm))

    def set_position(
        self,
        position_deg: float,
        *,
        max_speed_dps: float | None = None,
        max_speed_rpm: float | None = None,
    ) -> Feedback:
        """Set absolute multi-turn target using speed-limited 0xA4 only.

        Position limits constrain requested targets, not physical travel in
        current/speed mode. Choose at most one speed unit. The CAN speed
        bound always has whole-degree/second resolution, including RPM input.
        """
        if max_speed_dps is not None and max_speed_rpm is not None:
            raise ValueError("Specify only one of max_speed_dps and max_speed_rpm")
        if max_speed_rpm is not None:
            max_speed_dps = rpm_to_dps(max_speed_rpm)
        finite_number("position_deg", position_deg)
        if not self.limits.min_position_deg <= position_deg <= self.limits.max_position_deg:
            raise LimitError("position_deg is outside configured target limits")
        speed = self.limits.max_speed_dps if max_speed_dps is None else max_speed_dps
        finite_number("max_speed_dps", speed)
        if not 1 <= speed <= self.limits.max_speed_dps:
            raise LimitError("max_speed_dps must be between 1 and the configured speed limit")
        quantized_position = protocol.scaled_integer(position_deg, 100, "position_deg") / 100
        if not self.limits.min_position_deg <= quantized_position <= self.limits.max_position_deg:
            raise LimitError("Quantized position falls outside configured target limits")
        self._check_closed_loop_configuration()
        payload = protocol.position_command(position_deg, speed)
        with self._lock:
            return self._motion(payload, check_acceleration=True)

    def read_health(self) -> Health:
        """Read even while disabled; an active fault also attempts OFF."""
        with self._lock:
            health = self._read_decoded(Command.READ_HEALTH, protocol.decode_health)
            if self._enabled and self._health_violation(health):
                self._best_effort_disable()
            return health

    def read_feedback(self) -> Feedback:
        """Read 0x9C telemetry using status_full_scale_a from the selected profile."""
        with self._lock:
            try:
                feedback = self._read_decoded(
                    Command.READ_FEEDBACK,
                    lambda payload: protocol.decode_feedback(payload, self._current_scale),
                )
                if self._enabled:
                    self._check_feedback(feedback)
                return feedback
            except (can.CanError, ProtocolError, MotorFaultError):
                if self._enabled:
                    self._best_effort_disable()
                raise

    def read_angle(self, *, single_turn: bool = False) -> float:
        with self._lock:
            command = Command.READ_SINGLE_TURN_ANGLE if single_turn else Command.READ_ANGLE
            return self._read_decoded(command, lambda payload: protocol.decode_angle(payload, single_turn=single_turn))

    def read_encoder(self) -> Encoder:
        with self._lock:
            return self._read_decoded(Command.READ_ENCODER, protocol.decode_encoder)

    def read_phase_currents(self) -> PhaseCurrents:
        with self._lock:
            phases = self._read_decoded(Command.READ_PHASE_CURRENTS, protocol.decode_phase_currents)
            if self._enabled and phases.temperature_c >= self.limits.max_temperature_c:
                self._best_effort_disable()
                raise MotorFaultError("Phase-current feedback temperature reached host limit")
            return phases

    def clear_errors(self) -> Health:
        """Explicit recovery only; V2.35 lists 0x9A as reply opcode to 0x9B."""
        with self._lock:
            self._require_disarmed()
            self.disable()
            return protocol.decode_health(
                self._request(
                    protocol.query(Command.CLEAR_ERRORS),
                    Command.READ_HEALTH,
                    Command.CLEAR_ERRORS,
                )
            )

    def read_pid(self) -> PidParameters:
        with self._lock:
            return self._read_decoded(Command.READ_PID, protocol.decode_pid)

    def write_pid_ram(self, parameters: PidParameters) -> PidParameters:
        if not isinstance(parameters, PidParameters):
            raise TypeError("parameters must be PidParameters with raw uint8 gains")
        payload = protocol.pid_command(parameters)
        with self._lock:
            self._require_disarmed()
            self.disable()
            self._acknowledge(payload)
            return parameters

    def read_acceleration(self) -> int:
        with self._lock:
            return self._read_decoded(Command.READ_ACCELERATION, protocol.decode_acceleration)

    def write_acceleration_ram(self, acceleration_dps2: int) -> int:
        payload = protocol.acceleration_command(acceleration_dps2)
        if acceleration_dps2 > self.limits.max_acceleration_dps2:
            raise LimitError("acceleration_dps2 exceeds configured acceleration limit")
        with self._lock:
            self._require_disarmed()
            self.disable()
            self._acknowledge(payload)
            return acceleration_dps2
