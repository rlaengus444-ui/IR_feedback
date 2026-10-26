"""Standalone UI session and bounded, GUI-independent worker lifecycle."""

from collections import deque
from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
import threading
import time

from adapter_contracts import AdapterError, AmbiguousWriteError, ControlUnavailable, MachineWindow
from layer_algorithm import LayerAdaptiveController, LayerSettings


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class SessionOptions:
    target: MachineWindow
    mode: str = "monitor"
    target_temperature_c: float = 65.0
    settings_path: Path | None = None

    def __post_init__(self):
        if self.target.machine not in ("M1", "S3", "unknown") or self.target.pid <= 0 or self.target.hwnd <= 0:
            raise ValueError("자동 검색에서 실행 중인 M1 또는 S3 창을 선택하세요.")
        if self.mode not in ("monitor", "preview", "control"):
            raise ValueError("올바른 동작 모드를 선택하세요.")
        if self.mode == "control" and self.target.machine == "unknown":
            raise ValueError("장비 자동 식별이 안 된 창은 조회 또는 미리보기로만 연결할 수 있습니다.")
        if not finite(self.target_temperature_c):
            raise ValueError("목표 온도는 유효한 숫자여야 합니다.")


def default_adapter_factory():
    # Importing or testing the controller never probes another application.
    from windows_ui_adapter import WindowsUiAdapter
    return WindowsUiAdapter()


def run_ui_session(options, stop_event, emit, adapter_factory=default_adapter_factory):
    """All external reads/writes and calculation execute in this worker only."""
    adapter = None
    controllers = {}
    held = {}
    reasons = {}
    baseline_seen = False
    last_key = None
    last_layer = None
    last_status = None
    control_admitted = False
    known_run_key = None
    expected_settings = {}
    wait_for_different_layer = None
    try:
        if options.mode != "monitor":
            path = options.settings_path or Path(__file__).with_name("workbook-replay.settings.json")
            if Path(path).stat().st_size > 262144:
                raise ValueError("설정 파일은 256 KiB 이하여야 합니다.")
            settings = LayerSettings.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
            settings = replace(settings, target_temperature_c=options.target_temperature_c)
            controllers = {heater: LayerAdaptiveController(settings) for heater in (1, 2)}
        if stop_event.is_set():
            return
        adapter = adapter_factory()
        adapter.connect(options.target, stop_event)
        if stop_event.is_set():
            return
        adapter.prepare_mode(options.mode, stop_event)
        if stop_event.is_set():
            return
        emit("connected", dict(target=options.target, mode=options.mode))
        while not stop_event.is_set():
            started = time.monotonic()
            snapshot = adapter.read_snapshot(stop_event)
            if stop_event.is_set():
                break
            if (snapshot.target.machine, snapshot.target.pid, snapshot.target.hwnd) != (
                    options.target.machine, options.target.pid, options.target.hwnd):
                raise AdapterError("선택한 장비와 읽은 창의 식별값이 달라 연결을 중단했습니다.")
            effective_mode = options.mode
            status = "조회 중" if options.mode == "monitor" else "새 T_recoating 기록 대기"
            automatic = str(snapshot.machine_state).strip().upper() in ("AUTO", "RUNNING")
            if options.mode != "monitor" and not automatic:
                for controller in controllers.values():
                    controller.reset()
                held.clear()
                reasons.clear()
                baseline_seen = False
                last_key = None
                last_layer = None
                known_run_key = None
                wait_for_different_layer = None
                effective_mode = "waiting"
                status = "비자동/미확인 상태 · 이력 초기화 후 AUTO/RUNNING 및 새 T_recoating 대기"
            elif options.mode == "control" and not snapshot.control_available:
                if control_admitted:
                    raise ControlUnavailable("UI 자동 적용 조건이 변경되어 연결을 중단합니다: " + snapshot.control_unavailable_reason)
                effective_mode = "control-unavailable"
                status = "제어 불가: " + snapshot.control_unavailable_reason
            elif options.mode == "control":
                control_admitted = True
            can_calculate = effective_mode in ("preview", "control")
            values = {1: snapshot.heater1_setting_percent, 2: snapshot.heater2_setting_percent}
            heaters = (1,) if snapshot.coupled_output else (1, 2)
            if effective_mode == "control":
                for heater in heaters:
                    if finite(values[heater]):
                        if heater in expected_settings and not math.isclose(values[heater], expected_settings[heater], rel_tol=0, abs_tol=1e-9):
                            raise ControlUnavailable("IR 설정이 외부에서 변경되어 자동 적용을 중단합니다. 수동/층별 설정을 확인하고 재연결하세요.")
                        expected_settings[heater] = values[heater]
            valid = (snapshot.observation_key is not None and finite(snapshot.t_recoating_c)
                     and isinstance(snapshot.layer_index, int) and not isinstance(snapshot.layer_index, bool)
                     and snapshot.layer_index >= 0)
            # Existing historical rows at connection time are shown but do not
            # create a control action. Wait for receipt of a new T_recoating row.
            if effective_mode == "waiting":
                pass
            elif not baseline_seen and not (valid and snapshot.observation_is_new is True):
                if valid:
                    baseline_seen = True
                    last_key = snapshot.observation_key
                    known_run_key = snapshot.run_key
            elif can_calculate and valid and snapshot.observation_key != last_key:
                baseline_seen = True
                run_changed = snapshot.run_key is not None and snapshot.run_key != known_run_key and last_layer is not None
                uncertain_same_layer = last_layer == snapshot.layer_index
                if wait_for_different_layer == snapshot.layer_index:
                    last_key = snapshot.observation_key
                    status = "이력 초기화됨 · 다른 새 층의 T_recoating 대기"
                elif run_changed or uncertain_same_layer:
                    for controller in controllers.values():
                        controller.reset()
                    held.clear()
                    reasons.clear()
                    last_layer = None
                    last_key = snapshot.observation_key
                    known_run_key = snapshot.run_key
                    wait_for_different_layer = snapshot.layer_index
                    status = "작업/동일 층 기록 변경 · 이력 초기화 후 다음 새 층 대기"
                else:
                    wait_for_different_layer = None
                    if snapshot.run_key is not None:
                        known_run_key = snapshot.run_key
                    if all(finite(values[heater]) for heater in heaters):
                        last_key = snapshot.observation_key
                        if last_layer is not None and snapshot.layer_index <= last_layer:
                            for controller in controllers.values():
                                controller.reset()
                        for heater in heaters:
                            result = controllers[heater].process(snapshot.layer_index + 1,
                                snapshot.t_recoating_c, values[heater])
                            held[heater] = result.power_percent
                            reasons[heater] = result.reason
                        last_layer = snapshot.layer_index
                        if effective_mode == "control":
                            writes = 0
                            for heater in heaters:
                                if stop_event.is_set():
                                    break
                                power = held[heater]
                                if not finite(power) or not 0 <= power <= 100:
                                    raise ControlUnavailable("기존 UI에 전달할 유효한 출력 범위를 벗어났습니다.")
                                if math.isclose(power, values[heater], rel_tol=0, abs_tol=1e-9):
                                    continue  # Holding a value needs no redundant Save.
                                adapter.apply_ir_power(heater, power, snapshot.observation_key, stop_event)
                                expected_settings[heater] = power
                                writes += 1
                            status = "T_recoating 수신 · UI 자동 적용 완료" if writes else "T_recoating 수신 · 기존 설정 유지(저장 없음)"
                        else:
                            status = "T_recoating 수신 · 계산만 표시"
                    else:
                        status = "T_recoating 수신 · 현재 IR 설정을 읽을 수 없어 계산 대기"
            if last_status != (effective_mode, status):
                emit("log", status)
                last_status = (effective_mode, status)
            emit("snapshot", dict(snapshot=snapshot, effective_mode=effective_mode,
                                  proposed=dict(held), reasons=dict(reasons), status=status))
            interval = snapshot.minimum_poll_seconds
            if not finite(interval):
                interval = 1.0
            stop_event.wait(max(0.0, max(0.1, min(5.0, interval)) - (time.monotonic() - started)))
    finally:
        if adapter is not None:
            adapter.disconnect()


@dataclass(frozen=True)
class WorkerEvent:
    generation: int
    kind: str
    payload: object


class BoundedEvents:
    def __init__(self, capacity=128):
        self._items = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def put(self, event):
        with self._lock:
            self._items.append(event)

    def drain(self, maximum=64):
        with self._lock:
            count = min(maximum, len(self._items))
            return [self._items.popleft() for _ in range(count)]


class SessionController:
    """Call connect/disconnect/poll only on the view thread; worker emits data only."""

    def __init__(self, runner=run_ui_session, thread_factory=threading.Thread, error_logger=None):
        self.runner = runner
        self.thread_factory = thread_factory
        self.error_logger = error_logger
        self.events = BoundedEvents()
        self.generation = 0
        self.thread = None
        self.stop_event = None
        self.state = "disconnected"
        self.closed = False
        self.error = None

    def connect(self, options):
        if self.closed or self.thread is not None:
            return False
        self.generation += 1
        generation = self.generation
        self.stop_event = threading.Event()
        stop_event = self.stop_event
        self.state = "connecting"
        self.error = None
        def emit(kind, payload):
            self.events.put(WorkerEvent(generation, kind, payload))
        def work():
            try:
                self.runner(options, stop_event, emit)
            except Exception as error:
                if not stop_event.is_set() or isinstance(error, AmbiguousWriteError):
                    if self.error_logger is not None:
                        try:
                            self.error_logger(str(error))
                        except OSError:
                            pass
                    emit("error", str(error))
            finally:
                emit("finished", None)
        self.thread = self.thread_factory(target=work, daemon=True, name=f"IR-UI-{generation}")
        try:
            self.thread.start()
        except Exception:
            self.thread = None
            self.state = "disconnected"
            raise
        return True

    def disconnect(self):
        if self.stop_event is not None:
            self.stop_event.set()
        if self.thread is not None:
            self.state = "disconnecting"

    def close(self):
        self.closed = True
        self.disconnect()

    def poll(self):
        result = []
        for event in self.events.drain():
            if event.generation != self.generation or self.closed:
                continue
            if event.kind == "connected" and self.state == "connecting":
                self.state = "connected"
            elif event.kind == "error":
                self.error = str(event.payload)
                self.state = "error"
            elif event.kind == "finished":
                self.thread = None  # Cleanup completed before this event was emitted.
                self.state = "error" if self.error else "disconnected"
            if event.kind == "snapshot" and self.state == "disconnecting":
                continue
            result.append(event)
        return result

    @property
    def worker_alive(self):
        return self.thread is not None and self.thread.is_alive()
