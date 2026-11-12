"""Read-only discovery and explicit, capability-checked UI control contracts."""

from dataclasses import dataclass
from typing import Protocol


class AdapterError(RuntimeError):
    pass


class ControlUnavailable(AdapterError):
    pass


class ControlSuspended(ControlUnavailable):
    """Known settings/UI change before any IR edit; keep observing while held."""
    pass


class ObservationSuspended(ControlUnavailable):
    """CSV evidence changed before any edit; discard the decision, keep reading."""
    pass


class ObservationExpired(ObservationSuspended):
    """Only a known pre-edit time budget expired; consume, never replay it."""
    pass


class SaveConfirmationPending(ControlSuspended):
    """One owned Save is settling; observe and reconcile, never replay it."""
    pass


class TargetIdentityError(AdapterError):
    """The verified root/PID/machine identity was lost; do not rebind it."""
    pass


class ReadTransportError(AdapterError):
    """A read-only operation could not reach the native helper."""
    def __init__(self, operation, message, actuation_possible=False, helper_retired=True):
        super().__init__(message)
        self.operation = operation
        self.actuation_possible = actuation_possible
        self.helper_retired = helper_retired


class ProtocolError(AdapterError):
    """The helper returned an invalid protocol response."""


class UiReadTimeout(ReadTransportError):
    """A bounded helper timed out; only a known read-only operation may wait."""
    def __init__(self, message, operation="unknown", actuation_possible=False):
        super().__init__(operation, message, actuation_possible)


class AmbiguousWriteError(AdapterError):
    """Save may have executed; do not retry, restore, or infer its result."""
    pass


@dataclass(frozen=True)
class MachineWindow:
    machine: str
    pid: int
    hwnd: int
    title: str
    image_path: str


@dataclass(frozen=True)
class UiSnapshot:
    target: MachineWindow
    layer_index: int | None = None
    t_recoating_c: float | None = None
    observation_key: str | None = None
    heater1_setting_percent: float | None = None
    heater2_setting_percent: float | None = None
    machine_state: str = "unknown"
    control_available: bool = False
    control_unavailable_reason: str = "Existing UI control capability has not been verified."
    details: str = ""
    coupled_output: bool = True
    minimum_poll_seconds: float = 1.0
    run_key: str | None = None
    observation_is_new: bool | None = None
    control_recoverable: bool = False
    control_epoch: int = 0
    slicing_image_path: str = ""
    recoating_finished: object = None
    slice_lineage: str = ""
    csv_generation: str = ""
    # Current authorized job/layer evidence, with no elapsed-time TTL.
    observation_fresh: bool = False
    measurement_valid: bool = True
    invalid_reason: str = ""
    actual_target: MachineWindow | None = None
    actuation_barrier: bool = False
    expired_observation_key: str | None = None
    job_first_layer: int | None = None
    job_first_started: float | None = None
    job_auto_started: float | None = None
    job_current_layer: int | None = None
    job_start_offset: int | None = None
    job_log_identity: str = ""
    save_reconciliation: tuple | None = None


class UiAdapter(Protocol):
    def set_apply_admission_gate(self, callback) -> None: ...

    def discover(self) -> list[MachineWindow]: ...

    def connect(self, target: MachineWindow, stop_event) -> None: ...

    def prepare_mode(self, mode: str, stop_event) -> None: ...

    def read_snapshot(self, stop_event) -> UiSnapshot: ...

    def apply_ir_power(self, heater: int, power_percent: float,
                       expected_observation_key: str, stop_event) -> None: ...

    def suspend_control(self, reason: str) -> None: ...

    def validate_control_epoch(self, expected: int) -> None: ...

    def disconnect(self) -> None: ...
