"""Standalone UI session and bounded, GUI-independent worker lifecycle."""

from collections import deque
from dataclasses import dataclass, field, replace
import json
import math
from pathlib import Path
import threading
import time

from adapter_contracts import AdapterError, AmbiguousWriteError, ControlUnavailable, ControlSuspended, ObservationSuspended, ObservationExpired, MachineWindow
from layer_algorithm import LayerAdaptiveController, LayerSettings
from target_temperature import TargetTemperatureSetting
from excel_workbook import WorkbookController, WorkbookSpec
from excel_calculation import ExcelCalculation
from excel_async import AsyncWorkbookGroup
from slice_data import SliceDataset
from output_limit import (validate_maximum_ir, LiveMaximumIrSetting, LiveMaximumIrChanged,
                          limit_ir_recommendation)


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class SliceDatasetSetting:
    """Nonblocking staged image handoff; only the session worker activates it."""

    def __init__(self, dataset=None):
        self._lock = threading.Lock()
        self._active = (0, dataset)
        self._desired = self._active
        self._desired_run = None

    def request(self, dataset, *, force=False, run_key=None):
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
            self._desired_run = run_key
            return epoch

    def desired(self):
        with self._lock:
            return self._desired

    def discard_stale_request(self, epoch, run_key):
        """Retire a previous job's pending image handoff without replaying it."""
        with self._lock:
            if (not run_key or self._desired[0] != epoch or self._active[0] == epoch
                    or self._desired_run is None or self._desired_run == run_key):
                return False
            self._desired = (epoch + 1, None)
            self._desired_run = run_key
            return True

    def active(self):
        with self._lock:
            return self._active

    def activate(self, epoch, run_key=None):
        with self._lock:
            if self._desired[0] != epoch or (self._desired_run is not None and self._desired_run != run_key):
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
    maximum_ir_percent: float = 100.0
    _target_temperature_setting: TargetTemperatureSetting = field(init=False, repr=False, compare=False)
    _slice_dataset_setting: SliceDatasetSetting = field(init=False, repr=False, compare=False)
    _maximum_ir_setting: LiveMaximumIrSetting = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if self.target.machine not in ("M1", "S3", "unknown") or self.target.pid <= 0 or self.target.hwnd <= 0:
            raise ValueError("자동 검색에서 실행 중인 M1 또는 S3 창을 선택하세요.")
        if self.mode not in ("monitor", "preview", "control"):
            raise ValueError("올바른 동작 모드를 선택하세요.")
        if self.mode == "control" and self.target.machine == "unknown":
            raise ValueError("장비 자동 식별이 안 된 창은 조회 또는 미리보기로만 연결할 수 있습니다.")
        object.__setattr__(self, "maximum_ir_percent", validate_maximum_ir(self.maximum_ir_percent))
        if self.workbook_spec is not None:
            self.workbook_spec.validate()
        if self.slice_dataset is not None and not isinstance(self.slice_dataset, SliceDataset):
            raise ValueError("검증된 슬라이싱 분석 데이터가 필요합니다.")
        object.__setattr__(self, "_target_temperature_setting",
                           TargetTemperatureSetting(self.target_temperature_c))
        object.__setattr__(self, "_slice_dataset_setting", SliceDatasetSetting(self.slice_dataset))
        object.__setattr__(self, "_maximum_ir_setting", LiveMaximumIrSetting(self.maximum_ir_percent))


def default_adapter_factory():
    # Importing or testing the controller never probes another application.
    from resilient_adapter import ResilientUiAdapter
    return ResilientUiAdapter()


def run_ui_session(options, stop_event, emit, adapter_factory=default_adapter_factory, *, workbook_group_factory=None, preserve_adapter=False):
    """Only this worker reads BJT/CSV or writes IR; Excel has a separate owner."""
    adapter = None
    controllers = {}
    excel_calc = None
    excel_hold_reported = None
    excel_reset_reported = 0
    last_control_hold = None
    held = {}
    reasons = {}
    held_cap_stamp = None
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
    image_run_key = None
    image_authorized = True
    image_reset_epoch = None
    actual_target = options.target
    limit_setting = options._maximum_ir_setting
    hard_minimum, hard_maximum, hard_step = 0.0, 100.0, 5.0
    power_limit_holding = False
    def reset_controllers(reason="제어 조건 재확인"):
        for controller in controllers.values():
            controller.reset()
        if excel_calc is not None:
            excel_calc.reset(reason)

    def hold_feedback(reason="제어 조건 재확인"):
        nonlocal control_holding, recovery_baseline_pending, recovery_baseline_layer
        nonlocal baseline_seen, last_key, last_layer, known_run_key, wait_for_different_layer
        if not control_holding:
            reset_controllers(reason)
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
                excel_calc = ExcelCalculation(options.workbook_spec, options.target_temperature_c,
                    100.0, stop_event, controller_factory=WorkbookController,
                    group_factory=workbook_group_factory or AsyncWorkbookGroup)
            else:
                path = options.settings_path or Path(__file__).with_name("workbook-replay.settings.json")
                if Path(path).stat().st_size > 262144:
                    raise ValueError("설정 파일은 256 KiB 이하여야 합니다.")
                settings = LayerSettings.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
                settings = replace(settings, target_temperature_c=options.target_temperature_c)
                hard_minimum, hard_maximum, hard_step = (settings.minimum_power_percent,
                    settings.maximum_power_percent, settings.maximum_step_percent)
                controllers = {heater: LayerAdaptiveController(settings) for heater in (1, 2)}
        if options.mode != 'monitor' and not limit_setting.status()['ready']:
            limit_setting.bind_bounds(hard_minimum, hard_maximum, hard_step)
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
            applied = {}
            snapshot = adapter.read_snapshot(stop_event)
            snapshot_seconds = time.monotonic() - started
            if stop_event.is_set():
                break
            if (snapshot.target.machine, snapshot.target.pid, snapshot.target.hwnd) != (
                    options.target.machine, options.target.pid, options.target.hwnd):
                raise AdapterError("선택한 장비와 읽은 창의 식별값이 달라 연결을 중단했습니다.")
            if snapshot.actual_target is not None and snapshot.actual_target != actual_target:
                previous_target, actual_target = actual_target, snapshot.actual_target
                emit("target_rebound", actual_target)
                emit("log", f"{actual_target.machine} 컨트롤러 자동 재연결 · PID {previous_target.pid} → {actual_target.pid} · 새 관측과 설정 확인 후 IR 재개")
            if snapshot.run_key:
                if image_run_key is None:
                    image_run_key = snapshot.run_key
                elif snapshot.run_key != image_run_key:
                    image_run_key = snapshot.run_key
                    image_authorized = False
                    emit("image_lineage_hold", "새 작업 · 기존 이미지 분석은 보존하며 현재 작업 이미지 확인 전까지 이미지 참고값을 미확인으로 처리합니다.")
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
                    hold_feedback("제어 설정 확인 세대 변경")
                    known_control_epoch = epoch
                    control_holding = False
                    expected_settings.clear()
            automatic = str(snapshot.machine_state).strip().upper() in ("AUTO", "RUNNING")
            if options.mode != "monitor" and not automatic:
                reset_controllers("운전 상태 확인 대기: " + str(snapshot.machine_state))
                held.clear()
                reasons.clear()
                baseline_seen = False
                last_key = None
                last_layer = None
                known_run_key = None
                wait_for_different_layer = None
                effective_mode = "waiting"
                status = "비자동/미확인 상태 · 이력 초기화 후 AUTO/RUNNING 및 새 T_recoating 대기"
                if not snapshot.control_available:
                    status += ": " + snapshot.control_unavailable_reason
            elif options.mode == "control" and not snapshot.control_available:
                if snapshot.control_recoverable or snapshot.actuation_barrier:
                    hold_feedback("IR 적용 확인 대기: " + snapshot.control_unavailable_reason)
                    effective_mode = "control-waiting"
                    status = "연결 유지 · IR 적용 대기: " + snapshot.control_unavailable_reason
                else:
                    if control_admitted:
                        raise ControlUnavailable("UI 자동 적용 조건이 변경되어 연결을 중단합니다: " + snapshot.control_unavailable_reason)
                    effective_mode = "control-unavailable"
                    status = "제어 불가: " + snapshot.control_unavailable_reason
            elif options.mode == "control":
                control_admitted = True
                if control_holding and not power_limit_holding:
                    effective_mode = "control-waiting"
                    status = "연결 유지 · 변경된 설정의 재확인 대기"
            pause_reason = (snapshot.control_unavailable_reason if not snapshot.control_available
                            else "운전 상태 확인 대기: " + str(snapshot.machine_state))
            if options.mode == "control" and control_admitted and (not automatic or not snapshot.control_available):
                if pause_reason != last_control_hold:
                    emit("control_hold", f"IR 적용 대기 원인 · {pause_reason} · 설정 확인 세대={snapshot.control_epoch}")
                last_control_hold = pause_reason
            elif automatic and snapshot.control_available:
                last_control_hold = None
            can_calculate = effective_mode in ("preview", "control")
            values = {1: snapshot.heater1_setting_percent, 2: snapshot.heater2_setting_percent}
            heaters = (1,) if snapshot.coupled_output else (1, 2)
            if effective_mode == "control":
                for heater in heaters:
                    if finite(values[heater]):
                        if heater in expected_settings and not math.isclose(values[heater], expected_settings[heater], rel_tol=0, abs_tol=1e-9):
                            adapter.suspend_control("IR 설정이 외부에서 변경되어 새 설정을 확인합니다.")
                            hold_feedback("IR 설정이 외부에서 변경됨")
                            effective_mode = "control-waiting"
                            can_calculate = False
                            status = "연결 유지 · 외부 IR 설정 변경 확인 중"
                            break
                        expected_settings[heater] = values[heater]
            valid = (snapshot.observation_key is not None and finite(snapshot.t_recoating_c)
                     and isinstance(snapshot.layer_index, int) and not isinstance(snapshot.layer_index, bool)
                     and snapshot.layer_index >= 0)
            above_limit = any(finite(values[heater]) and not hard_minimum <= values[heater] <= hard_maximum for heater in heaters)
            if options.mode != "monitor" and above_limit:
                if not power_limit_holding:
                    hold_feedback("현재 IR이 알고리즘 허용 범위를 벗어남")
                power_limit_holding = True
                effective_mode = "power-limit-waiting"
                can_calculate = False
                status = "연결 유지 · 현재 IR이 알고리즘 허용 범위를 벗어났습니다(저장 없음)."
            elif power_limit_holding:
                # External settings recovery still owns its normal guards.
                # The first valid in-bound row is baseline only, never replayed.
                power_limit_holding = False
                control_holding = False
            # Editing/analysis is staged outside the active session. Only a
            # complete latest result requests a new epoch. Never mutate frozen
            # Excel rows or interrupt an already admitted Save to switch images.
            desired_epoch, desired_dataset = slice_setting.desired()
            if slice_setting.discard_stale_request(desired_epoch, snapshot.run_key):
                desired_epoch, desired_dataset = slice_setting.desired()
                emit("log", "이전 작업의 이미지 적용 예약 취소 · 현재 작업 이미지는 미확인으로 처리합니다.")
            handoff_pending = desired_epoch != slice_epoch
            if handoff_pending:
                if image_consuming and image_reset_epoch != desired_epoch:
                    held.clear()
                    reasons.clear()
                    reset_controllers("슬라이싱 이미지 교체")  # Reopens its pristine private copy.
                    image_reset_epoch = desired_epoch
                if slice_setting.activate(desired_epoch, snapshot.run_key):
                    slice_epoch, slice_dataset = desired_epoch, desired_dataset
                    image_run_key = snapshot.run_key
                    image_authorized = True
                    if image_consuming:
                        baseline_seen = valid
                        last_key = snapshot.observation_key if valid else None
                        last_layer = None
                        known_run_key = snapshot.run_key
                        wait_for_different_layer = None
                        image_waiting = True
                        image_baseline_layer = snapshot.layer_index if valid else None
                    emit("slice_activated", dict(epoch=slice_epoch, dataset=slice_dataset,
                         image_consuming=image_consuming))
                else:
                    # A newer complete result superseded this handoff during
                    # reset. Wait; no calculation may use the intermediate one.
                    can_calculate = False
            image_hold = image_consuming and (handoff_pending or image_waiting)
            if image_waiting and not handoff_pending:
                if valid and image_baseline_layer is None:
                    image_baseline_layer = snapshot.layer_index
                elif (valid and snapshot.observation_is_new is True and
                      snapshot.layer_index != image_baseline_layer and snapshot.observation_key != last_key and
                      can_calculate and all(finite(values[heater]) for heater in heaters)):
                    image_waiting = False
                    image_hold = False
                if image_hold and valid:
                    image_baseline_layer = snapshot.layer_index
                    baseline_seen = True
                    last_key = snapshot.observation_key
                    known_run_key = snapshot.run_key
            # Observations seen while readiness is absent are barriers too.
            # Restored AUTO/current-IR must not turn a cached held row into Save.
            if ((control_holding or recovery_baseline_pending or recovery_baseline_layer is not None)
                    and effective_mode not in ("control", "preview") and valid):
                recovery_baseline_layer = snapshot.layer_index
                last_key = snapshot.observation_key
                known_run_key = snapshot.run_key
            control_barrier = effective_mode in ("control-waiting", "power-limit-waiting")
            if effective_mode in ("control", "preview") and recovery_baseline_pending:
                control_barrier = True
                status = "설정 확인 완료 · 첫 유효 관찰을 기준으로 대기(저장 없음)"
                if valid and all(finite(values[heater]) for heater in heaters):
                    recovery_baseline_pending = False
                    recovery_baseline_layer = snapshot.layer_index
                    baseline_seen = True
                    last_key = snapshot.observation_key
                    known_run_key = snapshot.run_key
            elif effective_mode in ("control", "preview") and recovery_baseline_layer is not None:
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
            target_stamp = options._target_temperature_setting.snapshot()
            cap_stamp = limit_setting.snapshot()
            if held_cap_stamp is not None and held_cap_stamp != cap_stamp:
                held.clear()
                reasons.clear()
                held_cap_stamp = None
            features = (slice_dataset.features(snapshot.layer_index + 1)
                        if image_authorized and slice_dataset is not None and valid else None)
            if excel_calc is not None:
                excel_calc.watch(snapshot,
                    eligible=can_calculate and valid and not control_barrier and not image_hold
                             and all(finite(values[heater]) for heater in heaters),
                    allow_idle_history=can_calculate and not control_barrier and not image_hold
                                       and all(finite(values[heater]) for heater in heaters),
                    powers={heater: values[heater] for heater in heaters}, heaters=heaters,
                    target_stamp=target_stamp, image_epoch=slice_epoch if image_consuming else None,
                    features=features if image_consuming else None, cap_stamp=cap_stamp)
                if excel_calc.latched_reason:
                    held.clear(); reasons.clear()
                    effective_mode = "calculation-hold"
                    can_calculate = False
                    status = excel_calc.waiting_status()
                    if excel_hold_reported != excel_calc.fault_generation:
                        emit("calculation_hold", excel_calc.diagnostic_reason or excel_calc.latched_reason)
                        excel_hold_reported = excel_calc.fault_generation
            # Existing historical rows at connection time are shown but do not
            # create a control action. Wait for receipt of a new T_recoating row.
            if control_barrier:
                pass
            elif image_hold and effective_mode in ("preview", "control"):
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
                    reset_controllers("작업 또는 동일 층 기록 변경")
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
                        if last_layer is not None and snapshot.layer_index <= last_layer:
                            reset_controllers("층 순서 변경")
                        batch = None
                        if excel_calc is not None:
                            batch = excel_calc.calculate(snapshot,
                                {heater: values[heater] for heater in heaters}, heaters,
                                target_stamp, slice_epoch if image_consuming else None,
                                features if image_consuming else None, cap_stamp=cap_stamp)
                            if batch is None:
                                held.clear(); reasons.clear()
                                effective_mode = "calculation-hold" if excel_calc.latched_reason else "calculation-waiting"
                                status = excel_calc.waiting_status()
                        else:
                            batch = {}
                            for heater in heaters:
                                calculation_started = time.monotonic()
                                try:
                                    batch[heater] = controllers[heater].process(snapshot.layer_index + 1,
                                        snapshot.t_recoating_c, values[heater],
                                        maximum_power_percent=min(cap_stamp[1], hard_maximum))
                                finally:
                                    emit('log', f'소요시간 · {snapshot.layer_index + 1}층 IR {heater} · '
                                         f'CSV/UI 조회 {snapshot_seconds:.3f}초 · 기본 계산 '
                                         f'{time.monotonic() - calculation_started:.3f}초')
                        if batch is not None and limit_setting.snapshot() != cap_stamp:
                            reset_controllers("계산 중 IR 상한 변경")
                            held.clear(); reasons.clear()
                            last_key = snapshot.observation_key
                            last_layer = snapshot.layer_index
                            batch = None
                            status = "IR 상한 변경됨 · 다음 새 층 대기"
                        if batch is not None:
                            if excel_calc is not None and finite(excel_calc.last_calculation_seconds):
                                emit("log", f"소요시간 · {snapshot.layer_index + 1}층 · "
                                     f"CSV/UI 조회 {snapshot_seconds:.3f}초 · Excel 전체 계산 "
                                     f"{excel_calc.last_calculation_seconds:.3f}초")
                            if set(batch) != set(heaters):
                                raise ControlUnavailable("IR 전체 채널의 계산 결과를 확인하지 못했습니다.")
                            for heater, result in batch.items():
                                if not finite(result.power_percent) or not 0 <= result.power_percent <= 100:
                                    raise ControlUnavailable("계산된 IR 출력이 0~100 범위의 유효한 숫자가 아닙니다.")
                                limited = limit_ir_recommendation(values[heater], result.power_percent,
                                    cap_stamp[1], hard_minimum, hard_maximum, hard_step)
                                if excel_calc is None and not math.isclose(limited, result.power_percent,
                                                                           rel_tol=0, abs_tol=1e-9):
                                    raise ControlUnavailable("기본 알고리즘 출력이 최대 출력 또는 변경 폭과 일치하지 않습니다.")
                                held[heater] = limited
                                reasons[heater] = result.reason
                            if not limit_setting.activate(cap_stamp):
                                reset_controllers("계산 확정 전 IR 상한 변경")
                                held.clear(); reasons.clear()
                                batch = None
                                status = "IR 상한 변경됨 · 다음 새 층 대기"
                            else:
                                held_cap_stamp = cap_stamp
                            last_key = snapshot.observation_key
                            last_layer = snapshot.layer_index
                        if batch is not None:
                            if effective_mode == "control":
                                writes = 0
                                write_held = False
                                for heater in heaters:
                                    if stop_event.is_set():
                                        break
                                    power = held[heater]
                                    if limit_setting.snapshot() != cap_stamp:
                                        reset_controllers("IR 적용 전 상한 변경")
                                        held.clear(); reasons.clear(); write_held = True
                                        status = "IR 상한 변경됨 · 다음 새 층 대기"
                                        break
                                    if (excel_calc is not None and
                                            options._target_temperature_setting.snapshot() != target_stamp):
                                        reset_controllers("IR 적용 전 목표 온도 변경")
                                        held.clear(); reasons.clear(); write_held = True
                                        status = "목표 변경됨 · 이전 Excel 결과 적용 보류"
                                        break
                                    if image_consuming and not slice_setting.admit(slice_epoch):
                                        held.clear()
                                        reasons.clear()
                                        status = "이미지 교체 요청됨 · 이전 계산 적용 보류"
                                        break
                                    if (not finite(power) or not hard_minimum <= power <= hard_maximum
                                            or abs(power - values[heater]) > hard_step + 1e-9):
                                        raise ControlUnavailable("최대 IR 출력 또는 1회 최대 5%p 증감 제한을 벗어났습니다.")
                                    if math.isclose(power, values[heater], rel_tol=0, abs_tol=1e-9):
                                        continue  # Holding a value needs no redundant Save.
                                    apply_started = time.monotonic()
                                    admission_tokens = []
                                    gate_supported = getattr(adapter, 'supports_apply_admission', False) is True
                                    try:
                                        validate_epoch = getattr(adapter, "validate_control_epoch", None)
                                        if validate_epoch is not None:
                                            validate_epoch(snapshot.control_epoch)
                                        def admit():
                                            admission_tokens.append(limit_setting.begin_admission(cap_stamp, power, values[heater]))
                                        if gate_supported:
                                            adapter.set_apply_admission_gate(admit)
                                        else:
                                            admit()
                                        adapter.apply_ir_power(heater, power, snapshot.observation_key, stop_event)
                                    except LiveMaximumIrChanged:
                                        reset_controllers("IR 적용 직전 상한 변경")
                                        held.clear(); reasons.clear(); write_held = True
                                        status = "IR 상한 변경됨 · 다음 새 층 대기"
                                        break
                                    except ObservationExpired as error:
                                        context_ok = (options._target_temperature_setting.snapshot() == target_stamp
                                            and limit_setting.snapshot() == cap_stamp
                                            and (not image_consuming or slice_setting.admit(slice_epoch)))
                                        if context_ok and validate_epoch is None:
                                            context_ok = False
                                        if context_ok:
                                            try:
                                                validate_epoch(snapshot.control_epoch)
                                            except ControlUnavailable:
                                                context_ok = False
                                        if context_ok and excel_calc is not None:
                                            context_ok = excel_calc.can_retain_after_expiry(snapshot, values, heaters,
                                                target_stamp, slice_epoch if image_consuming else None, cap_stamp)
                                        if not context_ok:
                                            hold_feedback("유효 시간 만료 중 제어 조건 변경")
                                            control_holding = False
                                        held.clear(); reasons.clear(); held_cap_stamp = None
                                        effective_mode = "observation-waiting"
                                        can_calculate = False
                                        write_held = True
                                        status = "연결 유지 · 이번 계산 시간 만료 · 다음 새 층에서 자동 재개: " + str(error)
                                        break
                                    except ObservationSuspended as error:
                                        # No edit/Save started. Never retry the old
                                        # decision or retain its controller state.
                                        # Reuse the baseline/fresh-layer barrier,
                                        # without Sequence settings recovery.
                                        hold_feedback("CSV 관측 재확인: " + str(error))
                                        control_holding = False
                                        effective_mode = "observation-waiting"
                                        can_calculate = False
                                        write_held = True
                                        status = "연결 유지 · CSV 갱신 대기: " + str(error)
                                        break
                                    except ControlSuspended as error:
                                        adapter.suspend_control(str(error))
                                        hold_feedback("IR 적용 전 조건 재확인: " + str(error))
                                        effective_mode = "control-waiting"
                                        can_calculate = False
                                        status = "연결 유지 · IR 적용 대기: " + str(error)
                                        break
                                    except Exception:
                                        if applied:
                                            emit("snapshot", dict(snapshot=snapshot, effective_mode="control-waiting",
                                                proposed={}, reasons={}, applied=dict(applied),
                                                status="일부 IR 저장 확인 후 오류 발생 · 추가 적용 중단",
                                                display_target_c=effective_target, slice_epoch=slice_epoch,
                                                slice_features=features, decision_cap_stamp=held_cap_stamp))
                                        raise
                                    finally:
                                        if gate_supported:
                                            adapter.set_apply_admission_gate(None)
                                        for token in admission_tokens:
                                            limit_setting.end_admission(token)
                                        emit('log', f'소요시간 · {snapshot.layer_index + 1}층 IR {heater} · '
                                             f'재검증·저장 요청 {time.monotonic() - apply_started:.3f}초 '
                                             '(Save 성공 여부는 별도 기록)')
                                    applied[heater] = power
                                    expected_settings[heater] = power
                                    writes += 1
                                if not control_holding and not write_held and not (image_consuming and slice_setting.desired()[0] != slice_epoch):
                                    status = "T_recoating 수신 · UI 자동 적용 완료" if writes else "T_recoating 수신 · 기존 설정 유지(저장 없음)"
                            else:
                                status = "T_recoating 수신 · 계산만 표시"
                    else:
                        status = "T_recoating 수신 · 현재 IR 설정을 읽을 수 없어 계산 대기"
            if image_consuming and slice_setting.desired()[0] != slice_epoch:
                held.clear()
                reasons.clear()
                status = "이미지 교체 요청됨 · 이전 계산 적용 보류"
            if (excel_calc is not None and effective_mode in ("control", "preview")
                    and (excel_calc.pending or excel_calc.describe().get("state") in ("initializing", "resetting", "busy", "waiting-retirement"))):
                effective_mode = "calculation-waiting"
                status = excel_calc.waiting_status()
            if last_status != (effective_mode, status):
                emit("log", status)
                last_status = (effective_mode, status)
            if excel_calc is not None:
                reset_info = excel_calc.describe()
                reset_count = reset_info.get("history_reset_count", 0)
                if reset_count > excel_reset_reported:
                    cause = reset_info.get("last_history_reset") or {}
                    emit("history_reset", "Excel 이력 초기화 요청 · 원인=" + str(cause.get("reason", "미확인"))
                         + f" · 미확정 계산={cause.get('had_pending', False) or cause.get('had_unconsumed', False)}"
                         + f" · 완료 이력={cause.get('had_accepted', False)}"
                         + f" · 설정 확인 세대={snapshot.control_epoch}")
                    excel_reset_reported = reset_count
            display_target = options._target_temperature_setting.get() if options.mode == "monitor" else effective_target
            limit_status = limit_setting.status()
            actual_values = [values[heater] for heater in heaters if finite(values[heater])]
            limit_status['current_percent'] = max(actual_values) if actual_values else None
            limit_status['ramping'] = bool(limit_status['active_effective_cap'] is not None and
                any(value > limit_status['active_effective_cap'] for value in actual_values))
            emit("snapshot", dict(snapshot=snapshot, effective_mode=effective_mode,
                                  proposed=dict(held), reasons=dict(reasons), applied=applied, status=status,
                                  display_target_c=display_target,
                                  limit_status=limit_status,
                                  decision_cap_stamp=held_cap_stamp,
                                  slice_epoch=slice_epoch,
                                  image_authorized=image_authorized,
                                  slice_fingerprint=slice_dataset.fingerprint if slice_dataset is not None else None,
                                  slice_features=(slice_dataset.features(snapshot.layer_index + 1)
                                      if image_authorized and slice_dataset is not None and valid else None)))
            interval = snapshot.minimum_poll_seconds
            if not finite(interval):
                interval = 1.0
            stop_event.wait(max(0.0, max(0.1, min(5.0, interval)) - (time.monotonic() - started)))
    finally:
        try:
            if adapter is not None:
                if preserve_adapter and not stop_event.is_set():
                    adapter.drop_transport()
                else:
                    adapter.disconnect()
        finally:
            if excel_calc is not None:
                excel_calc.close()
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

    def __init__(self, runner=None, thread_factory=threading.Thread, error_logger=None):
        if runner is None:
            from observed_session import run_observed_session
            runner = run_observed_session
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
        self.maximum_ir_setting = None
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
        self.maximum_ir_setting = options._maximum_ir_setting
        self.image_consuming = bool(options.mode != "monitor" and options.workbook_spec is not None
                                    and options.workbook_spec.slice_start_cell)
        def emit(kind, payload):
            if kind in ("calculation_hold", "observer_hold", "actuation_barrier", "control_hold", "history_reset") and self.error_logger is not None:
                try:
                    self.error_logger(str(payload))
                except OSError:
                    pass
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
        if (self.thread is None or self.closed or self.state not in ("connecting", "connected", "recovering")
                or self.target_temperature_setting is None):
            raise ValueError("연결 중에만 목표 온도를 적용할 수 있습니다.")
        return self.target_temperature_setting.set(value)

    def update_slice_dataset(self, dataset, *, run_key=None):
        """Queue a complete dataset/clear; activation belongs to the worker."""
        if (self.thread is None or self.closed or self.state not in ("connecting", "connected", "recovering")
                or self.slice_dataset_setting is None):
            raise ValueError("연결 중에만 이미지 데이터 교체를 예약할 수 있습니다.")
        if not isinstance(run_key, str) or not run_key:
            raise ValueError("현재 작업이 확인된 뒤 이미지를 적용하세요.")
        return self.slice_dataset_setting.request(dataset, force=True, run_key=run_key)

    def update_maximum_ir(self, value):
        if (self.thread is None or self.closed or self.state not in ("connected", "recovering")
                or self.maximum_ir_setting is None):
            raise ValueError("연결 중에만 최대 IR 출력을 적용할 수 있습니다.")
        return self.maximum_ir_setting.set(value)

    def close(self):
        self.closed = True
        self.disconnect()

    def poll(self):
        result = []
        for event in self.events.drain():
            if event.generation != self.generation or self.closed:
                continue
            if event.kind == "connected" and self.state in ("connecting", "recovering"):
                self.state = "connected"
            elif event.kind == "observer_hold":
                self.state = "recovering"
            elif event.kind == "snapshot" and self.state == "recovering":
                self.state = "connected"
            elif event.kind == "error":
                self.error = str(event.payload)
                self.state = "error"
            elif event.kind == "finished":
                self.thread = None  # Cleanup completed before this event was emitted.
                self.target_temperature_setting = None
                self.slice_dataset_setting = None
                self.maximum_ir_setting = None
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
            if event.kind == 'snapshot' and self.maximum_ir_setting is not None:
                old_status = event.payload.get('limit_status') or {}
                current_status = self.maximum_ir_setting.status()
                if (old_status.get('active_stamp') == current_status.get('active_stamp')
                        and not current_status['pending']):
                    current_status['ramping'] = old_status.get('ramping', False)
                    current_status['current_percent'] = old_status.get('current_percent')
                else:
                    current_status.update(ramping=False, current_percent=None)
                event = WorkerEvent(event.generation, event.kind, dict(event.payload, limit_status=current_status))
                if ('decision_cap_stamp' in event.payload and event.payload['decision_cap_stamp'] is not None
                        and event.payload['decision_cap_stamp'] != current_status['requested_stamp']):
                    event = WorkerEvent(event.generation, event.kind,
                        dict(event.payload, proposed={}, reasons={},
                             status='IR 상한 변경 예약 · 이전 권장값 표시 보류'))
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
