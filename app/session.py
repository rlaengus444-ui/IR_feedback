"""Standalone UI session and bounded, GUI-independent worker lifecycle."""

from collections import deque
from dataclasses import dataclass, field, replace
import json
import math
from pathlib import Path
import threading
import time

from adapter_contracts import AdapterError, AmbiguousWriteError, ControlUnavailable, ControlSuspended, SourceSuspended, MachineWindow
from layer_algorithm import LayerAdaptiveController, LayerSettings
from target_temperature import TargetTemperatureSetting
from excel_workbook import WorkbookController, WorkbookSpec
from slice_data import SliceDataset


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class SliceDatasetSetting:
    """Nonblocking staged image handoff; only the session worker activates it."""

    def __init__(self, dataset=None):
        self._lock = threading.Lock()
        self._active = (0, dataset)
        self._desired = self._active

    def request(self, dataset, *, force=False):
        if dataset is not None and not isinstance(dataset, SliceDataset):
            raise ValueError("완료된 슬라이싱 분석 데이터가 필요합니다.")
        with self._lock:
            desired = self._desired[1]
            if not force and (dataset is desired or
                    (dataset is not None and desired is not None and
                     dataset.fingerprint == desired.fingerprint and dataset.config == desired.config)):
                return self._desired[0]
            epoch = self._desired[0] + 1
            self._desired = (epoch, dataset)
            return epoch

    def desired(self):
        with self._lock:
            return self._desired

    def active(self):
        with self._lock:
            return self._active

    def activate(self, epoch):
        with self._lock:
            if self._desired[0] != epoch:
                return False
            self._active = self._desired
            return True

    def admit(self, epoch):
        # This is the admission boundary, not a lock around external I/O.
        # An admitted Save may finish/verify if a request arrives afterward;
        # activation is acknowledged only when that worker returns to its loop.
        with self._lock:
            return self._active[0] == self._desired[0] == epoch


@dataclass(frozen=True)
class SessionOptions:
    target: MachineWindow
    mode: str = "monitor"
    target_temperature_c: float = 65.0
    settings_path: Path | None = None
    workbook_spec: WorkbookSpec | None = None
    slice_dataset: SliceDataset | None = None
    auto_source: object = None
    _target_temperature_setting: TargetTemperatureSetting = field(init=False, repr=False, compare=False)
    _slice_dataset_setting: SliceDatasetSetting = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if self.target.machine not in ("M1", "S3", "unknown") or self.target.pid <= 0 or self.target.hwnd <= 0:
            raise ValueError("자동 검색에서 실행 중인 M1 또는 S3 창을 선택하세요.")
        if self.mode not in ("monitor", "preview", "control"):
            raise ValueError("올바른 동작 모드를 선택하세요.")
        if self.mode == "control" and self.target.machine == "unknown":
            raise ValueError("장비 자동 식별이 안 된 창은 조회 또는 미리보기로만 연결할 수 있습니다.")
        if self.workbook_spec is not None:
            self.workbook_spec.validate()
        if self.slice_dataset is not None and not isinstance(self.slice_dataset, SliceDataset):
            raise ValueError("검증된 슬라이싱 분석 데이터가 필요합니다.")
        object.__setattr__(self, "_target_temperature_setting",
                           TargetTemperatureSetting(self.target_temperature_c))
        object.__setattr__(self, "_slice_dataset_setting", SliceDatasetSetting(self.slice_dataset))


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
    known_control_epoch = None
    control_holding = False
    recovery_baseline_pending = False
    recovery_baseline_layer = None
    effective_target = options.target_temperature_c
    slice_setting = options._slice_dataset_setting
    slice_epoch, slice_dataset = slice_setting.active()
    image_consuming = bool(options.mode != "monitor" and options.workbook_spec is not None
                           and options.workbook_spec.slice_start_cell)
    image_waiting = False
    image_baseline_layer = None
    auto_source = options.auto_source
    auto_baseline_pending = auto_source is not None and image_consuming
    auto_baseline_layer = None
    def hold_feedback():
        nonlocal control_holding, recovery_baseline_pending, recovery_baseline_layer
        nonlocal baseline_seen, last_key, last_layer, known_run_key, wait_for_different_layer
        if not control_holding:
            for controller in controllers.values():
                controller.reset()
        control_holding = True
        recovery_baseline_pending = True
        recovery_baseline_layer = None
        held.clear()
        reasons.clear()
        expected_settings.clear()
        baseline_seen = False
        last_key = last_layer = known_run_key = wait_for_different_layer = None

    try:
        if stop_event.is_set():
            return
        if options.mode != "monitor":
            if options.workbook_spec is not None:
                controllers = {1: WorkbookController(options.workbook_spec, options.target_temperature_c, stop_event=stop_event)}
            else:
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
        if auto_source is not None:
            auto_source.start(slice_setting)
        adapter.prepare_mode(options.mode, stop_event)
        if stop_event.is_set():
            return
        emit("connected", dict(target=options.target, mode=options.mode))
        while not stop_event.is_set():
            started = time.monotonic()
            applied = {}
            snapshot = adapter.read_snapshot(stop_event)
            if stop_event.is_set():
                break
            if (snapshot.target.machine, snapshot.target.pid, snapshot.target.hwnd) != (
                    options.target.machine, options.target.pid, options.target.hwnd):
                raise AdapterError("선택한 장비와 읽은 창의 식별값이 달라 연결을 중단했습니다.")
            effective_mode = options.mode
            status = "조회 중" if options.mode == "monitor" else "새 T_recoating 기록 대기"
            if options.mode == "control":
                epoch = snapshot.control_epoch
                if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
                    raise ControlUnavailable("제어 설정 확인 세대가 올바르지 않습니다.")
                if known_control_epoch is None:
                    known_control_epoch = epoch
                elif epoch != known_control_epoch:
                    if epoch < known_control_epoch:
                        raise ControlUnavailable("이전 설정 확인 결과를 재사용할 수 없습니다.")
                    hold_feedback()
                    known_control_epoch = epoch
                    control_holding = False
                    expected_settings.clear()
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
                if snapshot.control_recoverable:
                    hold_feedback()
                    effective_mode = "control-waiting"
                    status = "연결 유지 · IR 적용 대기: " + snapshot.control_unavailable_reason
                else:
                    if control_admitted:
                        raise ControlUnavailable("UI 자동 적용 조건이 변경되어 연결을 중단합니다: " + snapshot.control_unavailable_reason)
                    effective_mode = "control-unavailable"
                    status = "제어 불가: " + snapshot.control_unavailable_reason
            elif options.mode == "control":
                control_admitted = True
                if control_holding:
                    effective_mode = "control-waiting"
                    status = "연결 유지 · 변경된 설정의 재확인 대기"
            can_calculate = effective_mode in ("preview", "control")
            values = {1: snapshot.heater1_setting_percent, 2: snapshot.heater2_setting_percent}
            heaters = (1,) if snapshot.coupled_output else (1, 2)
            if (can_calculate and options.workbook_spec is not None and 2 in heaters
                    and 2 not in controllers):
                if stop_event.is_set():
                    break
                controllers[2] = WorkbookController(options.workbook_spec, effective_target, stop_event=stop_event)
            if effective_mode == "control":
                for heater in heaters:
                    if finite(values[heater]):
                        if heater in expected_settings and not math.isclose(values[heater], expected_settings[heater], rel_tol=0, abs_tol=1e-9):
                            adapter.suspend_control("IR 설정이 외부에서 변경되어 새 설정을 확인합니다.")
                            hold_feedback()
                            effective_mode = "control-waiting"
                            can_calculate = False
                            status = "연결 유지 · 외부 IR 설정 변경 확인 중"
                            break
                        expected_settings[heater] = values[heater]
            valid = (snapshot.observation_key is not None and finite(snapshot.t_recoating_c)
                     and isinstance(snapshot.layer_index, int) and not isinstance(snapshot.layer_index, bool)
                     and snapshot.layer_index >= 0)
            # Editing/analysis is staged outside the active session. Only a
            # complete latest result requests a new epoch. Never mutate frozen
            # Excel rows or interrupt an already admitted Save to switch images.
            desired_epoch, desired_dataset = slice_setting.desired()
            handoff_pending = desired_epoch != slice_epoch
            if handoff_pending:
                if image_consuming:
                    held.clear()
                    reasons.clear()
                    for controller in controllers.values():
                        controller.reset()  # Reopens its pristine private copy.
                if slice_setting.activate(desired_epoch):
                    slice_epoch, slice_dataset = desired_epoch, desired_dataset
                    if image_consuming:
                        baseline_seen = valid and auto_source is None
                        last_key = snapshot.observation_key if valid else None
                        last_layer = None
                        known_run_key = snapshot.run_key
                        wait_for_different_layer = None
                        image_waiting = True
                        image_baseline_layer = snapshot.layer_index if valid and auto_source is None else None
                    emit("slice_activated", dict(epoch=slice_epoch, dataset=slice_dataset,
                         image_consuming=image_consuming))
                else:
                    # A newer complete result superseded this handoff during
                    # reset. Wait; no calculation may use the intermediate one.
                    can_calculate = False
            auto_admission = None
            auto_barrier = False
            if auto_source is not None and image_consuming:
                auto_admission, auto_reason = auto_source.frame(snapshot, slice_epoch, slice_dataset)
                if auto_admission is None:
                    auto_barrier = True; auto_baseline_pending = True; auto_baseline_layer = None
                    held.clear(); reasons.clear()
                    status = '연결 유지 · 자동 이미지 대기: ' + auto_reason
                elif auto_baseline_pending:
                    auto_barrier = True; auto_baseline_pending = False
                    auto_baseline_layer = snapshot.layer_index
                    if image_waiting:
                        image_baseline_layer = snapshot.layer_index
                    status = '자동 이미지 확인 · 첫 새 측정을 기준으로 대기(저장 없음)'
                elif auto_baseline_layer is not None:
                    if (valid and snapshot.observation_is_new is True
                            and snapshot.layer_index != auto_baseline_layer and snapshot.observation_key != last_key):
                        auto_baseline_layer = None
                    else:
                        auto_barrier = True
                        status = '자동 이미지 확인 · 다음 새 층 대기(저장 없음)'
                if auto_barrier and valid:
                    last_key = snapshot.observation_key; known_run_key = snapshot.run_key
            image_hold = image_consuming and (handoff_pending or image_waiting or auto_barrier)
            if image_waiting and not handoff_pending:
                if valid and not auto_barrier and image_baseline_layer is None:
                    image_baseline_layer = snapshot.layer_index
                elif (valid and snapshot.observation_is_new is True and
                      snapshot.layer_index != image_baseline_layer and snapshot.observation_key != last_key and
                      not auto_barrier and can_calculate and all(finite(values[heater]) for heater in heaters)):
                    image_waiting = False
                    image_hold = False
                if image_hold and valid and not auto_barrier:
                    image_baseline_layer = snapshot.layer_index
                    baseline_seen = True
                    last_key = snapshot.observation_key
                    known_run_key = snapshot.run_key
            # Observations seen while readiness is absent are barriers too.
            # Restored AUTO/current-IR must not turn a cached held row into Save.
            if ((control_holding or recovery_baseline_pending or recovery_baseline_layer is not None)
                    and effective_mode != "control" and valid):
                recovery_baseline_layer = snapshot.layer_index
                last_key = snapshot.observation_key
                known_run_key = snapshot.run_key
            control_barrier = effective_mode == "control-waiting"
            if effective_mode == "control" and recovery_baseline_pending:
                control_barrier = True
                status = "설정 확인 완료 · 첫 유효 관찰을 기준으로 대기(저장 없음)"
                if valid and all(finite(values[heater]) for heater in heaters):
                    recovery_baseline_pending = False
                    recovery_baseline_layer = snapshot.layer_index
                    baseline_seen = True
                    last_key = snapshot.observation_key
                    known_run_key = snapshot.run_key
            elif effective_mode == "control" and recovery_baseline_layer is not None:
                if (valid and snapshot.observation_is_new is True
                        and snapshot.layer_index != recovery_baseline_layer
                        and snapshot.observation_key != last_key
                        and all(finite(values[heater]) for heater in heaters)):
                    recovery_baseline_layer = None
                else:
                    control_barrier = True
                    status = "설정 확인 완료 · 다음 새 층의 T_recoating 대기(저장 없음)"
                    if valid:
                        last_key = snapshot.observation_key
                        recovery_baseline_layer = snapshot.layer_index
                        known_run_key = snapshot.run_key
            # Existing historical rows at connection time are shown but do not
            # create a control action. Wait for receipt of a new T_recoating row.
            if control_barrier:
                pass
            elif image_hold and effective_mode in ("preview", "control"):
                if not auto_barrier:
                    status = "이미지 교체 중 · 새 층의 T_recoating 대기(저장 없음)"
            elif effective_mode == "waiting":
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
                        requested_target = options._target_temperature_setting.get()
                        if requested_target != effective_target:
                            for controller in controllers.values():
                                controller.set_target_temperature(requested_target)
                            effective_target = requested_target
                            emit("log", f"새 CSV 관찰부터 목표 온도 {effective_target:g} °C 적용")
                        last_key = snapshot.observation_key
                        if last_layer is not None and snapshot.layer_index <= last_layer:
                            for controller in controllers.values():
                                controller.reset()
                        for heater in heaters:
                            extra = {}
                            if options.workbook_spec is not None and options.workbook_spec.slice_start_cell:
                                extra['slice_features'] = (auto_admission.features if auto_admission is not None
                                    else slice_dataset.features(snapshot.layer_index + 1) if slice_dataset is not None else None)
                            result = controllers[heater].process(snapshot.layer_index + 1,
                                snapshot.t_recoating_c, values[heater], **extra)
                            held[heater] = result.power_percent
                            reasons[heater] = result.reason
                        last_layer = snapshot.layer_index
                        if effective_mode == "control":
                            writes = 0
                            source_write_held = False
                            for heater in heaters:
                                if stop_event.is_set():
                                    break
                                power = held[heater]
                                if image_consuming and not slice_setting.admit(slice_epoch):
                                    held.clear()
                                    reasons.clear()
                                    status = "이미지 교체 요청됨 · 이전 계산 적용 보류"
                                    break
                                if auto_source is not None and image_consuming and not auto_source.admit(auto_admission):
                                    held.clear(); reasons.clear()
                                    source_write_held = True
                                    status = '자동 이미지 경로 변경 · 이전 계산 적용 보류'
                                    break
                                if not finite(power) or not 0 <= power <= 100:
                                    raise ControlUnavailable("기존 UI에 전달할 유효한 출력 범위를 벗어났습니다.")
                                if math.isclose(power, values[heater], rel_tol=0, abs_tol=1e-9):
                                    continue  # Holding a value needs no redundant Save.
                                try:
                                    validate_epoch = getattr(adapter, "validate_control_epoch", None)
                                    if validate_epoch is not None:
                                        validate_epoch(snapshot.control_epoch)
                                    if auto_admission is not None:
                                        adapter.apply_ir_power(heater, power, snapshot.observation_key, stop_event,
                                                               source_guard=auto_admission.guard)
                                    else:
                                        adapter.apply_ir_power(heater, power, snapshot.observation_key, stop_event)
                                except SourceSuspended as error:
                                    auto_source.guard_failed(str(error))
                                    held.clear(); reasons.clear(); auto_baseline_pending = True
                                    source_write_held = True
                                    status = '연결 유지 · 이미지 대응 재확인: ' + str(error)
                                    break
                                except ControlSuspended as error:
                                    adapter.suspend_control(str(error))
                                    hold_feedback()
                                    effective_mode = "control-waiting"
                                    can_calculate = False
                                    status = "연결 유지 · IR 적용 대기: " + str(error)
                                    break
                                applied[heater] = power
                                expected_settings[heater] = power
                                writes += 1
                            if not control_holding and not source_write_held and not (image_consuming and slice_setting.desired()[0] != slice_epoch):
                                status = "T_recoating 수신 · UI 자동 적용 완료" if writes else "T_recoating 수신 · 기존 설정 유지(저장 없음)"
                        else:
                            status = "T_recoating 수신 · 계산만 표시"
                    else:
                        status = "T_recoating 수신 · 현재 IR 설정을 읽을 수 없어 계산 대기"
            if image_consuming and slice_setting.desired()[0] != slice_epoch:
                held.clear()
                reasons.clear()
                status = "이미지 교체 요청됨 · 이전 계산 적용 보류"
            if auto_source is not None and image_consuming and not auto_source.admit(auto_admission):
                held.clear(); reasons.clear(); auto_admission = None
            if last_status != (effective_mode, status):
                emit("log", status)
                last_status = (effective_mode, status)
            display_target = options._target_temperature_setting.get() if options.mode == "monitor" else effective_target
            emit("snapshot", dict(snapshot=snapshot, effective_mode=effective_mode,
                                  proposed=dict(held), reasons=dict(reasons), applied=applied, status=status,
                                  display_target_c=display_target,
                                  slice_epoch=slice_epoch,
                                  slice_fingerprint=slice_dataset.fingerprint if slice_dataset is not None else None,
                                  slice_features=(auto_admission.features if auto_admission is not None
                                      else None if auto_source is not None and image_consuming
                                      else slice_dataset.features(snapshot.layer_index + 1)
                                      if slice_dataset is not None and valid else None)))
            interval = snapshot.minimum_poll_seconds
            if not finite(interval):
                interval = 1.0
            stop_event.wait(max(0.0, max(0.1, min(5.0, interval)) - (time.monotonic() - started)))
    finally:
        try:
            if auto_source is not None:
                auto_source.stop()
            if adapter is not None:
                adapter.disconnect()
        finally:
            for controller in controllers.values():
                close = getattr(controller, "close", None)
                if close is not None:
                    close()


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

    @property
    def pending(self):
        with self._lock:
            return bool(self._items)


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
        self.target_temperature_setting = None
        self.slice_dataset_setting = None
        self.image_consuming = False

    def connect(self, options):
        if self.closed or self.thread is not None:
            return False
        self.generation += 1
        generation = self.generation
        self.stop_event = threading.Event()
        stop_event = self.stop_event
        self.state = "connecting"
        self.error = None
        self.target_temperature_setting = options._target_temperature_setting
        self.slice_dataset_setting = options._slice_dataset_setting
        self.image_consuming = bool(options.mode != "monitor" and options.workbook_spec is not None
                                    and options.workbook_spec.slice_start_cell)
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

    def update_target_temperature(self, value):
        """Schedule a new target for the next new valid CSV observation."""
        if (self.thread is None or self.closed or self.state not in ("connecting", "connected")
                or self.target_temperature_setting is None):
            raise ValueError("연결 중에만 목표 온도를 적용할 수 있습니다.")
        return self.target_temperature_setting.set(value)

    def update_slice_dataset(self, dataset):
        """Queue a complete dataset/clear; activation belongs to the worker."""
        if (self.thread is None or self.closed or self.state not in ("connecting", "connected")
                or self.slice_dataset_setting is None):
            raise ValueError("연결 중에만 이미지 데이터 교체를 예약할 수 있습니다.")
        return self.slice_dataset_setting.request(dataset)

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
                self.target_temperature_setting = None
                self.slice_dataset_setting = None
                self.image_consuming = False
                self.state = "error" if self.error else "disconnected"
            if event.kind == "snapshot" and self.state == "disconnecting":
                result.append(self._chart_event(event))
                continue
            if event.kind == "snapshot" and self.slice_dataset_setting is not None:
                desired_epoch = self.slice_dataset_setting.desired()[0]
                if event.payload.get("slice_epoch", 0) != desired_epoch:
                    payload = dict(event.payload, slice_features=None)
                    if self.image_consuming:
                        payload.update(proposed={}, reasons={},
                                       status="이미지 교체 예약 · 이전 권장값 표시 보류")
                    event = WorkerEvent(event.generation, event.kind, payload)
            result.append(event)
        return result

    @staticmethod
    def _chart_event(event):
        payload = event.payload
        chart = {"snapshot": payload.get("snapshot"), "applied": payload.get("applied", {})}
        if finite(payload.get("display_target_c")):
            chart["display_target_c"] = payload["display_target_c"]
        return WorkerEvent(event.generation, "chart_snapshot", chart)

    def drain_chart_events(self, maximum=64):
        """Closing-only data drain. Never restores state, proposals or admission."""
        if not self.closed:
            raise ValueError("차트 전용 종료 처리는 프로그램을 닫는 동안에만 가능합니다.")
        return [self._chart_event(event) for event in self.events.drain(maximum)
                if event.generation == self.generation and event.kind == "snapshot"]

    @property
    def chart_events_pending(self):
        return self.events.pending

    @property
    def worker_alive(self):
        return self.thread is not None and self.thread.is_alive()
