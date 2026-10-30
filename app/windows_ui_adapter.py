"""Standalone Windows UI Automation reader for an unchanged BinderJet process.

Every provider operation runs in a disposable, time-bounded helper. Explicit
control mode uses the user's existing Sequence IR textbox and Save procedure.
The adapter never injects code, toggles a heater, or opens a communication port.
"""
from __future__ import annotations

import base64
import hashlib
import json
import itertools
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

try:
    from .adapter_contracts import AdapterError, AmbiguousWriteError, ControlUnavailable, ControlSuspended, SourceSuspended, ObservationSuspended, TargetIdentityError, UiReadTimeout, MachineWindow, UiSnapshot
    from . import sequence_persistence
    from .sequence_field_audit import assert_fields_match_persisted, SequenceFieldAuditError
    from .csv_source import MonitoringCsvReader
except ImportError:
    from adapter_contracts import AdapterError, AmbiguousWriteError, ControlUnavailable, ControlSuspended, SourceSuspended, ObservationSuspended, TargetIdentityError, UiReadTimeout, MachineWindow, UiSnapshot
    import sequence_persistence
    from sequence_field_audit import assert_fields_match_persisted, SequenceFieldAuditError
    from csv_source import MonitoringCsvReader


CONTROL_REASON = "기존 Sequence IR 입력·Save 경로와 저장 파일을 아직 확인하지 못했습니다."

# Tests can inject a synthetic-only helper. Production uses the adjacent
# native dispatcher; its sole UIA operation is isolated leaf-ID metadata.
_HELPER = ""



def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip().replace("°C", "").replace("℃", "").strip()
    if not re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?", text):
        return None
    result = float(text.replace(",", "."))
    return result if math.isfinite(result) else None


def _log_layer_hint(target: MachineWindow) -> tuple[int | None, str | None]:
    """Read an existing log tail only; it supplies coordinates, never temperature."""
    if target.machine not in {"M1", "S3"} or not target.image_path:
        return None, None
    directory = Path(target.image_path).parent / "Logs"
    try:
        with os.scandir(directory) as entries:
            candidates = [Path(entry.path) for entry in itertools.islice(entries, 4096)
                          if entry.is_file() and entry.name.lower().endswith("_" + target.machine.lower() + ".log")]
        if not candidates:
            return None, None
        path = max(candidates, key=lambda item: item.stat().st_mtime_ns)
        stat = path.stat()
        age = time.time() - stat.st_mtime
        if age < -5 or age > 15:
            return None, None
        with path.open("rb") as stream:
            stream.seek(max(0, stat.st_size - 262144))
            tail = stream.read(262144).decode("utf-8", errors="replace")
        layer = None
        run = None
        for line in tail.splitlines():
            if "AUTO" not in line:
                continue
            if "AutoPrint started!" in line:
                run = hashlib.sha256((str(path) + "\0" + line).encode()).hexdigest()
                layer = None
            match = re.search(r"\bLayer:\s*(\d+)\s*->\s*Current Z\s*=", line)
            if match:
                layer = int(match.group(1))
            if any(terminal in line for terminal in ("AutoPrint finished!", "AutoPrint cancelled.", "Stopping printing as requested.")):
                layer = None
        return layer, run
    except (OSError, ValueError):
        return None, None


class WindowsUiAdapter:
    MINIMUM_POLL_SECONDS = 0.25
    RECOVERY_INTERVAL_SECONDS = 1.0

    def __init__(self, helper_timeout: float = 3.0, *, runner: Callable | None = None, source_factory=MonitoringCsvReader):
        if not 0.1 <= helper_timeout <= 10:
            raise ValueError("UI helper timeout must be in 0.1..10 seconds.")
        self.helper_timeout = float(helper_timeout)
        self._runner = runner
        self._source_factory = source_factory
        self._csv = None
        self._csv_token = None
        self._new_sample_key = None
        self._baseline_sample_key = None
        self._status_at = 0.0
        self._machine_state = "unknown"
        self._last_applied_key = None
        self._settings_at = 0.0
        self._lock = threading.Lock()
        self._helper_lock = threading.Lock()
        self._active_helper: subprocess.Popen | None = None
        self._target: MachineWindow | None = None
        self._generation = 0
        self._last_read = 0.0
        self._last_snapshot: UiSnapshot | None = None
        self._last_settings: list[int | float | None] = [None, None]
        self._mode = "monitor"
        self._control_baseline: dict | None = None
        self._persisted_baseline = None
        self._expected_ir: str | None = None
        self._observation_row: dict | None = None
        self._control_fault: str | None = None
        self._control_hold_reason: str | None = None
        self._control_epoch = 0
        self._recovery_at = 0.0
        self._recovery_candidate = None
        self._run_key: str | None = None
        self._layer_hint: int | None = None
        self._native_handles: dict = {}
        self._native_fields: dict = {}
        self._retained_log_hint: int | None = None
        self._log_run_key: str | None = None

    def discover(self) -> list[MachineWindow]:
        response = self._request({"operation": "discover"}, None)
        result: list[MachineWindow] = []
        for row in response.get("windows", []):
            try:
                machine = str(row.get("machine", "unknown")).upper()
                result.append(MachineWindow(machine if machine in {"M1", "S3"} else "unknown", int(row["pid"]), int(row["hwnd"]), str(row.get("title", "")), str(row.get("image_path", ""))))
            except (KeyError, TypeError, ValueError):
                continue
        return result

    def connect(self, target: MachineWindow, stop_event) -> None:
        self._check_stopped(stop_event)
        if target.pid <= 0 or target.hwnd <= 0:
            raise AdapterError("유효한 원본 프로그램 창을 선택해야 합니다.")
        response = self._request({"operation": "validate", "pid": target.pid, "hwnd": target.hwnd, "machine": target.machine}, stop_event)
        reported = str(response.get("machine", "unknown")).upper()
        if target.machine in {"M1", "S3"} and reported in {"M1", "S3"} and reported != target.machine:
            raise AdapterError("선택한 창의 장비 종류가 변경되었습니다. 다시 검색하세요.")
        with self._lock:
            self._generation += 1
            self._target = target
            self._last_read = 0
            self._last_snapshot = None
            self._last_settings = [None, None]
            self._control_baseline = None
            self._persisted_baseline = None
            self._control_fault = None
            self._control_hold_reason = None
            self._control_epoch = 0
            self._recovery_at = 0.0
            self._recovery_candidate = None
            self._run_key = None
            self._native_handles = dict(response.get("handles") or {})
            self._native_fields = {}
            self._retained_log_hint = None
            self._log_run_key = None
            self._update_settings_locked(response)
            self._csv = self._source_factory(target)
            self._csv_token = None
            self._new_sample_key = None
            self._baseline_sample_key = None
            self._status_at = 0.0
            self._last_applied_key = None

    def prepare_mode(self, mode: str, stop_event) -> None:
        self._check_stopped(stop_event)
        if mode not in {"monitor", "preview", "control"}:
            raise ValueError("Unknown UI mode.")
        with self._lock:
            target, generation = self._target, self._generation
            self._mode = mode
            handles = dict(self._native_handles)
        if target is None:
            raise AdapterError("먼저 원본 프로그램 창에 연결하세요.")
        if mode != "control":
            return
        if target.machine not in {"M1", "S3"}:
            raise ControlUnavailable("M1/S3 장비 종류가 확인된 창에서만 자동 적용할 수 있습니다.")
        before = sequence_persistence.capture(target)
        if before is None:
            raise ControlUnavailable("기존 IR 저장 파일을 찾을 수 없어 Save 결과를 확인할 수 없습니다.")
        common = {"pid": target.pid, "hwnd": target.hwnd, "machine": target.machine, "image_path": target.image_path, "_generation": generation}
        entered = self._request(dict(common, operation="enter_sequence", handles=handles), stop_event)
        metadata = self._request(dict(common, operation="metadata"), stop_event)
        if not entered.get("entered", False):
            present = {str(item.get("id")) for item in metadata.get("records", [])
                       if isinstance(item, dict) and item.get("visible") and item.get("id") in {"textBoxHeatingOutput", "btnSave"}}
            if present and present != {"textBoxHeatingOutput", "btnSave"}:
                raise ControlUnavailable("Sequence 입력 화면이 불완전하여 재입장하거나 저장하지 않고 중단했습니다.")
            if not present:
                self._request(dict(common, operation="enter_sequence", handles=handles, force_sequence=True), stop_event)
                metadata = self._request(dict(common, operation="metadata"), stop_event)
        # The metadata client must exit before native field operations or
        # showing the large virtual Process table again.
        response = self._request(dict(common, operation="prepare_control", metadata=metadata.get("records", []), handles=handles), stop_event)
        fields = response.get("fields")
        displayed = _number(response.get("setting"))
        if not isinstance(fields, dict) or "textBoxHeatingOutput" not in fields or displayed is None or not math.isclose(displayed, before.power, rel_tol=0, abs_tol=1e-9):
            raise ControlUnavailable("Sequence IR 표시값과 기존 저장값이 일치하지 않아 자동 적용을 보류했습니다.")
        try:
            assert_fields_match_persisted(fields, before, target.machine)
        except ValueError as exc:
            raise ControlUnavailable("연결 전 미저장/다른 Sequence 설정을 확인해야 합니다. " + str(exc)) from exc
        after = sequence_persistence.capture(target)
        if after is None or after.sha256 != before.sha256 or after.non_ir != before.non_ir:
            raise ControlUnavailable("연결 준비 중 기존 설정 파일이 변경되었습니다. 다시 연결하세요.")
        handles = dict(response.get("handles") or {})
        native_fields = dict(response.get("field_map") or {})
        with self._lock:
            if self._generation != generation or self._target != target:
                raise AdapterError("연결 준비 중 대상이 변경되거나 연결이 해제되었습니다.")
            self._control_baseline = fields
            self._persisted_baseline = after
            self._expected_ir = str(fields["textBoxHeatingOutput"])
            self._native_handles = handles
            self._native_fields = native_fields
            self._last_settings = [before.power, before.power]
            self._last_snapshot = None

    def suspend_control(self, reason: str) -> None:
        """Enter connected HOLD. Never edit, Save, navigate or erase chart data."""
        with self._lock:
            if self._control_hold_reason is None:
                self._recovery_candidate = None
                self._recovery_at = 0.0
            self._control_hold_reason = str(reason)
            self._last_snapshot = None

    def validate_control_epoch(self, expected: int) -> None:
        with self._lock:
            if self._target is None or self._control_fault is not None:
                raise ControlUnavailable(self._control_fault or "연결된 제어 대상이 없습니다.")
            if self._control_hold_reason is not None or expected != self._control_epoch:
                raise ControlSuspended(self._control_hold_reason or "계산 후 제어 설정이 변경되어 적용을 대기합니다.")

    def _recover_control(self, target, generation, stop_event):
        now = time.monotonic()
        if now - self._recovery_at < self.RECOVERY_INTERVAL_SECONDS:
            return
        self._recovery_at = now
        try:
            before = sequence_persistence.capture(target)
            if before is None:
                raise ControlSuspended("저장 설정을 확인할 수 없어 IR 적용을 대기합니다.")
            with self._lock:
                handles, fields = dict(self._native_handles), dict(self._native_fields)
            response = self._request({"operation": "recover_control", "pid": target.pid,
                "hwnd": target.hwnd, "machine": target.machine, "image_path": target.image_path,
                "handles": handles, "field_map": fields, "_generation": generation}, stop_event)
            if str(response.get("machine", "unknown")).upper() != target.machine:
                raise TargetIdentityError("설정 확인 중 장비 식별값이 변경되었습니다.")
            if str(response.get("state", "unknown")).upper() not in {"AUTO", "RUNNING"}:
                raise ControlSuspended("AUTO/RUNNING 상태를 확인할 때까지 IR 적용을 대기합니다.")
            values, mapping = response.get("fields"), response.get("field_map")
            new_handles = response.get("handles")
            if not isinstance(values, dict) or not isinstance(mapping, dict) or not mapping or not isinstance(new_handles, dict):
                raise ControlSuspended("Sequence 입력 전체와 저장 경로를 다시 확인하는 중입니다.")
            displayed = _number(response.get("setting"))
            if displayed is None or not math.isclose(displayed, before.power, rel_tol=0, abs_tol=1e-9):
                raise ControlSuspended("화면 IR 값과 저장 설정이 일치할 때까지 대기합니다.")
            assert_fields_match_persisted(values, before, target.machine)
            after = sequence_persistence.capture(target)
            if (after is None or before.path != after.path or before.sha256 != after.sha256
                    or before.non_ir != after.non_ir or before.power != after.power):
                raise ControlSuspended("설정이 저장 중입니다. 일치하는 설정을 다시 확인합니다.")
            self._check_stopped(stop_event)
            signature = json.dumps({"identity": [target.machine, target.pid, target.hwnd, target.image_path],
                "settings_path": after.path, "power": after.power, "non_ir": dict(after.non_ir), "fields": values,
                "handles": new_handles, "field_map": mapping}, sort_keys=True, allow_nan=False)
            with self._lock:
                if self._generation != generation or self._target != target:
                    raise TargetIdentityError("설정 확인 중 연결 대상이 변경되었습니다.")
                previous = self._recovery_candidate
                if previous is None or previous[0] != signature:
                    self._recovery_candidate = (signature, time.monotonic())
                    self._control_hold_reason = "새 설정 확인 중 · 안정된 설정을 한 번 더 확인합니다."
                    return
                if time.monotonic() - previous[1] < self.RECOVERY_INTERVAL_SECONDS:
                    return
                self._control_baseline = dict(values)
                self._persisted_baseline = after
                self._expected_ir = str(values["textBoxHeatingOutput"])
                self._native_handles, self._native_fields = dict(new_handles), dict(mapping)
                self._last_settings = [after.power, after.power]
                self._machine_state = str(response["state"])
                self._control_epoch += 1
                self._control_hold_reason = None
                self._recovery_candidate = None
                self._last_snapshot = None
        except (ControlSuspended, UiReadTimeout, sequence_persistence.PersistenceUnavailable, SequenceFieldAuditError) as error:
            self._check_stopped(stop_event)
            self._recovery_candidate = None
            self.suspend_control(str(error))

    def _update_settings_locked(self, response: dict) -> None:
        displayed = _number(response.get("setting"))
        if displayed is not None and 0 <= displayed <= 100:
            self._last_settings = [displayed, displayed]
        powers = response.get("status_powers", {})
        if isinstance(powers, dict):
            for heater in (1, 2):
                power = _number(powers.get(str(heater)))
                # An OFF status is not a recipe change. Preserve the last known
                # command setting across the normal production off window.
                if power is not None and 0 < power <= 100:
                    self._last_settings[heater - 1] = power

    def read_snapshot(self, stop_event) -> UiSnapshot:
        self._check_stopped(stop_event)
        with self._lock:
            target, generation = self._target, self._generation
            handles = dict(self._native_handles)
            if target is None or self._csv is None:
                raise AdapterError("원본 프로그램에 연결되어 있지 않습니다.")
            if self._last_snapshot is not None and time.monotonic() - self._last_read < self.MINIMUM_POLL_SECONDS:
                return self._last_snapshot
        source = self._csv.read()
        sample = source.sample
        if self._mode == "control" and time.monotonic() - self._status_at >= 1.0:
            try:
                status = self._request({"operation": "read_status", "pid": target.pid, "hwnd": target.hwnd,
                                        "machine": target.machine, "handles": handles, "_generation": generation}, stop_event)
                self._machine_state = str(status.get("state", "unknown"))
            except ControlSuspended as error:
                self._check_stopped(stop_event)
                self.suspend_control(str(error))
                self._machine_state = "unknown"
            except AdapterError as error:
                self._check_stopped(stop_event)
                if not any(text in str(error) for text in ("WM_GETTEXT", "UI 응답 제한", "not responding")):
                    raise
                # This request contains no edit or Save. Keep reading the CSV,
                # but UNKNOWN makes the worker reset learning and suspend IR.
                self._machine_state = "unknown"
            self._status_at = time.monotonic()
        machine_state = self._machine_state if self._mode == "control" else source.context.state
        persisted = None
        if source.is_new or time.monotonic() - self._settings_at >= 1.0:
            try:
                persisted = sequence_persistence.capture(target)
                if self._mode == "control" and self._persisted_baseline is not None:
                    expected = _number(self._expected_ir)
                    if (persisted is None or expected is None
                            or not math.isclose(persisted.power, expected, rel_tol=0, abs_tol=1e-9)
                            or persisted.non_ir != self._persisted_baseline.non_ir):
                        self.suspend_control("BJT 저장 설정 변경 · 새 설정을 확인할 때까지 IR 적용 대기")
            except sequence_persistence.PersistenceUnavailable as error:
                if self._mode != "control" or self._persisted_baseline is None:
                    raise
                self.suspend_control(str(error))
            self._settings_at = time.monotonic()
        if self._mode == "control" and self._control_hold_reason is not None and self._control_fault is None:
            with self._lock:
                self._last_settings = [persisted.power, persisted.power] if persisted is not None else [None, None]
            self._recover_control(target, generation, stop_event)
            persisted = None  # Do not overwrite an adopted baseline with the earlier read.
            machine_state = self._machine_state
        with self._lock:
            if self._generation != generation or self._target != target:
                raise AdapterError("관측 중 연결이 해제되거나 변경되었습니다.")
            if persisted is not None:
                self._last_settings = [persisted.power, persisted.power]
            if self._run_key != source.context.run_key:
                self._baseline_sample_key = None
                self._new_sample_key = None
            self._run_key = source.context.run_key
            if sample and source.token:
                if self._baseline_sample_key is None:
                    self._baseline_sample_key = sample.key
                if source.is_new:
                    self._new_sample_key = sample.key
            fresh = sample is not None and -2 <= time.time() - sample.finished.timestamp() <= MonitoringCsvReader.MAX_NEW_AGE_SECONDS
            key = sample.key if sample and source.token and (
                sample.key == self._baseline_sample_key or (sample.key == self._new_sample_key and fresh)) else None
            details = source.details
            if self._mode == "control" and machine_state == "unknown":
                details += " 원본 UI 응답 대기 · CSV 조회만 계속하며 IR 적용은 정지합니다."
            if sample and sample.delay_seconds is not None:
                details += f" 리코팅 완료→인쇄 완료 기록 차이 {sample.delay_seconds:.1f}초."
            ready = self._mode == "control" and self._control_baseline is not None and self._control_fault is None and self._control_hold_reason is None
            snapshot = UiSnapshot(target=target, layer_index=sample.layer_index if sample else None,
                t_recoating_c=sample.temperature if sample else None, observation_key=key,
                heater1_setting_percent=self._last_settings[0], heater2_setting_percent=self._last_settings[1],
                machine_state=machine_state, control_available=ready,
                control_unavailable_reason=self._control_fault or self._control_hold_reason or CONTROL_REASON,
                details=details, coupled_output=True, minimum_poll_seconds=self.MINIMUM_POLL_SECONDS,
                run_key=self._run_key, observation_is_new=bool(sample and sample.key == self._new_sample_key and fresh),
                control_recoverable=self._control_hold_reason is not None and self._control_fault is None,
                control_epoch=self._control_epoch,
                slicing_image_path=getattr(sample, 'slicing_image_path', ''),
                recoating_finished=getattr(sample, 'recoating_finished', None),
                slice_lineage=getattr(sample, 'slice_lineage', ''),
                csv_generation=getattr(sample, 'source_generation', ''))
            self._csv_token = source.token
            self._layer_hint = source.context.current_layer
            self._observation_row = {"row_index": sample.layer_index, "temperature": sample.temperature,
                                     "start": sample.start} if sample else None
            self._last_snapshot, self._last_read = snapshot, time.monotonic()
            return snapshot

    def _verify_csv_observation(self, expected_key, expected_lineage=None):
        with self._lock:
            reader, generation = self._csv, self._generation
        if reader is None:
            raise AdapterError("연결이 해제되어 CSV 검증을 중단했습니다.")
        source = reader.read()
        with self._lock:
            if generation != self._generation:
                raise AdapterError("연결이 변경되어 CSV 검증을 중단했습니다.")
            if source.is_new and source.sample is not None and source.context.run_key == self._run_key:
                self._new_sample_key = source.sample.key
        sample = source.sample
        if source.context.run_key != self._run_key:
            raise ControlUnavailable("CSV의 작업이 변경되어 이전 작업의 계산값을 저장하지 않았습니다.")
        if source.token is None:
            raise ObservationSuspended("CSV 기록 중 · 이번 계산을 취소하고 완전한 완료 행을 기다립니다.")
        if sample is None or sample.key != expected_key:
            raise ObservationSuspended("CSV 완료 층 갱신 · 이전 계산을 취소하고 새 관찰을 확인합니다.")
        if expected_lineage is not None and getattr(sample, 'slice_lineage', '') != expected_lineage:
            raise SourceSuspended('CSV 이미지 경로 또는 리코팅 시각이 변경되어 자동 적용을 대기합니다.')
        age = time.time() - sample.finished.timestamp()
        if sample.key != self._new_sample_key or not -2 <= age <= MonitoringCsvReader.MAX_NEW_AGE_SECONDS:
            raise ControlUnavailable("오래된 CSV 기록은 새 제어 명령에 재사용하지 않습니다.")
        hint = source.context.current_layer
        if hint is None or not sample.layer_index <= hint <= sample.layer_index + 1:
            raise ControlUnavailable("CSV 완료 층과 현재 운전 로그의 층이 일치하지 않습니다.")
        return source.token

    def verify_image_observation(self, key, lineage, stop_event):
        """Read-only pre-learning check. Critical CSV parse failures still raise."""
        self._check_stopped(stop_event)
        try:
            self._verify_csv_observation(key,lineage)
        except ObservationSuspended:
            with self._lock:
                self._last_snapshot = None
                self._last_read = 0
            return False
        except ControlUnavailable:
            return False  # A newer/different row needs a new baseline, no Save.
        return True

    def apply_ir_power(self, heater: int, power_percent: int | float, expected_observation_key: str, stop_event, *, source_guard=None) -> None:
        try:
            return self._apply_ir_power(heater, power_percent, expected_observation_key, stop_event, source_guard=source_guard)
        except ObservationSuspended:
            with self._lock:
                self._last_snapshot = None
                self._last_read = 0
            raise
        except ControlSuspended as error:
            self.suspend_control(str(error))
            raise

    def _apply_ir_power(self, heater: int, power_percent: int | float, expected_observation_key: str, stop_event, *, source_guard=None) -> None:
        self._check_stopped(stop_event)
        if heater not in (1, 2) or isinstance(power_percent, bool) or not isinstance(power_percent, (int, float)) or not math.isfinite(power_percent) or not 0 <= power_percent <= 100:
            raise ValueError("Heater must be 1 or 2 and power must be finite in 0..100.")
        power_text = format(power_percent, ".17g")
        with self._lock:
            target, snapshot = self._target, self._last_snapshot
            baseline, persisted = self._control_baseline, self._persisted_baseline
            expected_ir, observation = self._expected_ir, self._observation_row
            generation = self._generation
            handles = dict(self._native_handles)
            if self._mode != "control" or self._control_fault or baseline is None or persisted is None or target is None:
                raise ControlUnavailable(self._control_fault or CONTROL_REASON)
            if self._control_hold_reason is not None:
                raise ControlSuspended(self._control_hold_reason)
            if snapshot is None or snapshot.observation_key != expected_observation_key or observation is None or time.monotonic() - self._last_read > 3:
                raise ControlUnavailable("최신 T_recoating 관측값을 다시 확인해야 합니다.")
            if self._last_applied_key == expected_observation_key:
                raise ControlUnavailable("같은 CSV 행에는 IR을 두 번 저장하지 않습니다.")
            if not snapshot.control_available or self._layer_hint is None or not self._layer_hint - 1 <= observation["row_index"] <= self._layer_hint:
                raise ControlUnavailable("현재 생산 층과 일치하는 최신 관측값이 필요합니다.")
            layer_hint = self._layer_hint
        current_hint, _ = _log_layer_hint(target)
        if current_hint is not None:
            if not current_hint - 1 <= observation["row_index"] <= current_hint:
                raise ControlUnavailable("계산 이후 생산 층이 진행되어 이전 관측값의 적용을 중단했습니다.")
            layer_hint = current_hint
        try:
            current = sequence_persistence.capture(target)
        except sequence_persistence.PersistenceUnavailable as error:
            raise ControlSuspended("설정 저장 중 · IR 입력 전 새 설정 확인 대기: " + str(error)) from error
        expected_number = _number(expected_ir)
        if current is None or expected_number is None or not math.isclose(current.power, expected_number, rel_tol=0, abs_tol=1e-9) or current.non_ir != persisted.non_ir:
            raise ControlSuspended("기존 IR 또는 다른 저장 설정이 변경되어 자동 적용을 중단했습니다.")
        common = {"pid": target.pid, "hwnd": target.hwnd, "machine": target.machine, "image_path": target.image_path, "_generation": generation}
        guarded_at = time.monotonic()
        expected_lineage = source_guard.get('lineage') if source_guard is not None else None
        if source_guard is not None and not expected_lineage:
            raise SourceSuspended('자동 이미지와 CSV의 대응 확인값이 필요합니다.')
        csv_token = self._verify_csv_observation(expected_observation_key, expected_lineage)
        fresh = self._request(dict(common, operation="verify_control", handles=handles,
                                   field_map=self._native_fields, observation=observation, layer_hint=layer_hint,
                                   csv_token=csv_token), stop_event)
        fresh_fields = fresh.get("fields")
        if not isinstance(fresh_fields, dict) or fresh_fields != dict(baseline, textBoxHeatingOutput=expected_ir):
            raise ControlSuspended("IR 또는 다른 Sequence 입력값이 변경되어 자동 저장을 중단했습니다.")
        try:
            current = sequence_persistence.capture(target)
        except sequence_persistence.PersistenceUnavailable as error:
            raise ControlSuspended("설정 저장 중 · IR 입력 전 새 설정 확인 대기: " + str(error)) from error
        if current is None or current.non_ir != persisted.non_ir or not math.isclose(current.power, expected_number, rel_tol=0, abs_tol=1e-9):
            raise ControlSuspended("IR 입력 전 기존 저장 설정이 변경되었습니다.")
        try:
            assert_fields_match_persisted(fresh_fields, current, target.machine)
        except SequenceFieldAuditError as exc:
            raise ControlSuspended("IR 입력 전에 다른 설정의 미저장 변경을 발견했습니다. " + str(exc)) from exc
        if time.monotonic() - guarded_at > 3:
            raise ControlUnavailable("관측 확인 후 시간이 지나 이번 적용을 중단했습니다. 다시 연결해 새 관측을 확인하세요.")
        handles = dict(fresh.get("handles") or handles)
        native_fields = dict(fresh.get("field_map") or self._native_fields)
        csv_token = self._verify_csv_observation(expected_observation_key, expected_lineage)
        with tempfile.TemporaryDirectory(prefix="binderjet-save-") as directory:
            marker = Path(directory) / "save-started.txt"
            edit_marker = Path(directory) / "edit-started.txt"
            try:
                response = self._request({"operation": "apply", "pid": target.pid, "hwnd": target.hwnd, "machine": target.machine, "image_path": target.image_path,
                                          "_generation": generation,
                                          "baseline": baseline, "expected_ir": expected_ir, "observation": observation,
                                          "handles": handles, "field_map": native_fields,
                                          "csv_token": csv_token,
                                          "layer_hint": layer_hint,
                                          "power": power_text, "source_guard": source_guard,
                                          "save_marker": str(marker), "edit_marker": str(edit_marker)}, stop_event)
                readback = _number(response.get("setting"))
                if not response.get("save_invoked") or readback is None or not math.isclose(readback, power_percent, rel_tol=0, abs_tol=1e-9):
                    raise AdapterError("IR Save 확인 응답이 일치하지 않습니다.")
                verified = sequence_persistence.verify_after(current, power_percent, timeout=1.0)
                if verified is None:
                    raise AdapterError("IR 저장 결과를 확인할 수 없습니다.")
            except Exception as exc:
                if marker.exists() or edit_marker.exists():
                    with self._lock:
                        self._control_fault = "자동 적용 결과를 확인할 수 없습니다. 다시 연결하기 전에 원본 설정을 확인하세요."
                    raise AmbiguousWriteError("IR 입력값이 수정되었거나 Save가 실행되었을 수 있으나 완료 여부를 확인하지 못했습니다. 같은 명령을 자동 재시도하지 않습니다. " + str(exc)) from exc
                if not isinstance(exc, (ControlSuspended, SourceSuspended, ObservationSuspended)):
                    with self._lock:
                        self._control_fault = "자동 적용을 중단했습니다. 결과를 확인한 뒤 다시 연결하세요."
                raise
        with self._lock:
            if self._generation != generation or self._target != target:
                raise AmbiguousWriteError("IR 저장 확인 중 연결이 변경되었습니다. 같은 명령을 자동 재시도하지 않습니다.")
            self._persisted_baseline = verified
            self._native_handles = handles
            self._native_fields = native_fields
            self._control_baseline = dict(fresh_fields, textBoxHeatingOutput=str(response["setting"]))
            self._expected_ir = str(response["setting"])
            self._last_settings = [power_percent, power_percent]
            self._last_applied_key = expected_observation_key
            self._last_snapshot = None
            self._last_read = 0

    def disconnect(self) -> None:
        with self._lock:
            self._generation += 1
            self._target = None
            self._last_snapshot = None
            self._control_baseline = None
            self._persisted_baseline = None
            self._native_handles = {}
            self._native_fields = {}
            self._csv = None
            self._csv_token = None
            helper = self._active_helper
        if helper is not None and helper.poll() is None:
            try:
                helper.kill()  # Only the helper process created by this adapter.
            except OSError:
                pass

    @staticmethod
    def _check_stopped(stop_event) -> None:
        if stop_event is not None and stop_event.is_set():
            raise AdapterError("관측이 중지되었습니다.")

    @staticmethod
    def _response_error(request, response, fallback):
        response = response if isinstance(response, dict) else {}
        kind = response.get("error_kind")
        if kind == 'slice_wait':
            return SourceSuspended(str(response.get('error') or fallback))
        if kind == 'observation_wait':
            return ObservationSuspended(str(response.get('error') or fallback))
        error_type = ControlSuspended if kind == "control_wait" else TargetIdentityError if kind == "identity" else AdapterError
        return error_type("[" + str(request.get("operation", "unknown")) + "] " + str(response.get("error", fallback)))

    def _request(self, request: dict, stop_event) -> dict:
        self._check_stopped(stop_event)
        request = dict(request, bridge_path=str(Path(__file__).with_name("UiaBridge.dll")))
        expected_generation = request.get("_generation")
        with self._lock:
            if expected_generation is not None and expected_generation != self._generation:
                raise AdapterError("연결이 변경되어 이전 작업을 중단했습니다.")
        if self._runner is not None:
            response = self._runner(request, stop_event)
            if not isinstance(response, dict):
                raise AdapterError("UI 읽기 도우미가 올바른 응답을 반환하지 않았습니다.")
            if not response.get("success", False):
                raise self._response_error(request, response, "UI 읽기에 실패했습니다.")
            return response
        if os.name != "nt":
            raise AdapterError("기존 프로그램 화면 연결은 Windows에서만 지원합니다.")
        deadline_seconds = max(self.helper_timeout, 5.0) if request.get("operation") in {"prepare_control", "apply"} else self.helper_timeout
        if not self._helper_lock.acquire(timeout=deadline_seconds):
            raise AdapterError("이전 UI 읽기가 아직 종료되지 않았습니다.")
        process = None
        try:
            helper_directory = tempfile.TemporaryDirectory(prefix="binderjet-uia-")
            helper_path = Path(helper_directory.name) / "reader.ps1"
            helper_source = _HELPER or Path(__file__).with_name("native_helper.ps1").read_text(encoding="utf-8-sig")
            helper_path.write_text(helper_source, encoding="utf-8-sig")
            powershell = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
            loader = "& ([scriptblock]::Create([IO.File]::ReadAllText('" + str(helper_path).replace("'", "''") + "')))"
            encoded_loader = base64.b64encode(loader.encode("utf-16-le")).decode("ascii")
            process = subprocess.Popen([powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Mta", "-EncodedCommand", encoded_loader],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with self._lock:
                connection_changed = expected_generation is not None and expected_generation != self._generation
                if not connection_changed:
                    self._active_helper = process
            if connection_changed:
                # The child is still waiting for stdin: no UI operation has
                # received a request, so disconnect cannot start a late Save.
                raise AdapterError("연결이 해제되어 이전 UI 작업을 취소했습니다.")
            deadline = time.monotonic() + deadline_seconds
            payload: str | None = json.dumps(request, ensure_ascii=False)
            while True:
                self._check_stopped(stop_event)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UiReadTimeout(f"원본 UI 응답 제한 시간({deadline_seconds:g}초)을 초과했습니다.")
                try:
                    stdout, stderr = process.communicate(payload, timeout=min(0.05, remaining))
                    break
                except subprocess.TimeoutExpired:
                    payload = None
            try:
                response = json.loads(stdout.lstrip("\ufeff").strip())
            except (json.JSONDecodeError, TypeError) as exc:
                raise AdapterError("Windows UI 읽기 도우미가 올바른 응답을 반환하지 않았습니다. " + stderr[-300:]) from exc
            if not isinstance(response, dict) or not response.get("success", False):
                raise self._response_error(request, response, "Windows UI 읽기에 실패했습니다.")
            return response
        finally:
            if process is not None:
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
                try:
                    process.communicate(timeout=0.5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            with self._lock:
                if self._active_helper is process:
                    self._active_helper = None
            self._helper_lock.release()
            if "helper_directory" in locals():
                helper_directory.cleanup()
