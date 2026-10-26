"""Read-only discovery and explicit, capability-checked UI control contracts."""

from dataclasses import dataclass
from typing import Protocol


class AdapterError(RuntimeError):
    pass


class ControlUnavailable(AdapterError):
    pass


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


class UiAdapter(Protocol):
    def discover(self) -> list[MachineWindow]: ...

    def connect(self, target: MachineWindow, stop_event) -> None: ...

    def prepare_mode(self, mode: str, stop_event) -> None: ...

    def read_snapshot(self, stop_event) -> UiSnapshot: ...

    def apply_ir_power(self, heater: int, power_percent: float,
                       expected_observation_key: str, stop_event) -> None: ...

    def disconnect(self) -> None: ...
