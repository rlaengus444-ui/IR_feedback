"""Continuously observe a verified job through read-only connection failures."""
from __future__ import annotations

import ntpath
import math
import time
from dataclasses import replace

try:
    from .adapter_contracts import (AdapterError, AmbiguousWriteError, SaveConfirmationPending, ControlSuspended,
        ControlUnavailable, ObservationSuspended, TargetIdentityError, ReadTransportError,
        ProtocolError, UiSnapshot)
    from .csv_source import MonitoringCsvReader, CsvSourceError, CsvReadUnavailable
    from .windows_ui_adapter import WindowsUiAdapter
    from .sequence_persistence import PersistenceUnavailable
except ImportError:
    from adapter_contracts import (AdapterError, AmbiguousWriteError, SaveConfirmationPending, ControlSuspended,
        ControlUnavailable, ObservationSuspended, TargetIdentityError, ReadTransportError,
        ProtocolError, UiSnapshot)
    from csv_source import MonitoringCsvReader, CsvSourceError, CsvReadUnavailable
    from windows_ui_adapter import WindowsUiAdapter
    from sequence_persistence import PersistenceUnavailable



def _identity(target):
    return (ntpath.normcase(ntpath.normpath(target.image_path)), target.machine.upper())


class ResilientUiAdapter:
    """One logical session, retaining CSV evidence and write barriers on reconnect."""
    supports_apply_admission = True
    _DELAYS = (2, 5, 15, 30, 60)

    def __init__(self, raw_factory=WindowsUiAdapter, discoverer=None,
                 source_factory=MonitoringCsvReader, clock=time.monotonic):
        self._raw_factory, self._discoverer = raw_factory, discoverer
        self._apply_admission_gate = None
        self._source_factory, self._clock = source_factory, clock
        self._raw = self._reader = None
        self._target = self.current_target = None
        self._mode = 'monitor'
        self._stop = None
        self._stopped = False
        self._next_retry = 0
        self._failures = 0
        self._consumed_save_reconciliation = None
        self._sticky = ''
        self._fault = None
        self._fault_callback = None
        self._fault_delivered = False
        self._pinned_job = None
        self._pinned_proof = None
        self._retired_jobs = set()
        self._job_highwater = None
        self._job_handoff = False
        self._job_candidate = None
        self._handoff_proof = None
        self._rebind_audit_needed = False
        self._watermark = {}
        self._prepared_mode = None
        self._initial_prepare_allowed = True
        self._initial_recovery_grant = False
        self._control_prepare_attempted = False
        self._last_snapshot = None
        self._was_ready = False
        self._epoch = 0
        self._raw_epoch = None
        self._needs_baseline = True
        self._baseline = None
        self.recovery_state = 'disconnected'

    def _check(self, event):
        if self._stopped or (event is not None and event.is_set()):
            self._initial_recovery_grant = False
            self._initial_prepare_allowed = False
            raise AdapterError('관측이 중지되었습니다.')

    def _new_raw(self):
        return self._raw_factory(source_factory=lambda target: self._reader)

    def set_apply_admission_gate(self, callback):
        if callback is not None and not callable(callback):
            raise TypeError('Apply admission gate must be callable or None.')
        if self._raw is not None:
            setter = getattr(self._raw, 'set_apply_admission_gate', None)
            if setter is None and callback is not None:
                raise ControlUnavailable('원본 어댑터가 적용 승인 검사를 지원하지 않습니다.')
            if setter is not None:
                setter(callback)
        self._apply_admission_gate = callback

    def discover(self):
        self._check(self._stop)
        if self._discoverer:
            return self._discoverer()
        raw = self._new_raw()
        try:
            return raw.discover()
        finally:
            raw.disconnect()

    def connect(self, target, stop_event):
        self._check(stop_event)
        if self._target is not None and target != self._target:
            self._initial_recovery_grant = False
            self._initial_prepare_allowed = False
            raise TargetIdentityError('기존 관측 대상이 변경되었습니다.')
        if not target.image_path or target.machine not in {'M1', 'S3'}:
            self._initial_recovery_grant = False
            self._initial_prepare_allowed = False
            raise TargetIdentityError('장비와 실행 파일 경로를 확인해야 합니다.')
        self._target, self._stop = target, stop_event
        if self._reader is None:
            try:
                self._reader = self._source_factory(target)
            except BaseException:
                self._initial_prepare_allowed = False
                self._initial_recovery_grant = False
                raise
        if self._raw is None:
            self._attempt(target, stop_event)

    def _attempt(self, target, event):
        self._check(event)
        try:
            raw = self._new_raw()
        except BaseException:
            self._initial_prepare_allowed = False
            self._initial_recovery_grant = False
            raise
        phase = 'setup'
        try:
            setter = getattr(raw, 'set_apply_admission_gate', None)
            if self._apply_admission_gate is not None and setter is None:
                raise ControlUnavailable('원본 어댑터가 적용 승인 검사를 지원하지 않습니다.')
            if setter is not None:
                setter(self._apply_admission_gate)
            phase = 'connect'
            raw.connect(target, event)
            phase = 'monitor'
            raw.prepare_mode('monitor', event)
        except (ReadTransportError, TargetIdentityError, ControlUnavailable, PersistenceUnavailable, OSError) as exc:
            if getattr(exc, "actuation_possible", False):
                self.freeze_control(exc)
            eligible = (phase == 'connect' and isinstance(exc, ReadTransportError)
                        and exc.operation == 'validate' and exc.actuation_possible is False
                        and exc.helper_retired is True and not self._sticky
                        and not self._control_prepare_attempted
                        and (self._initial_prepare_allowed or self._initial_recovery_grant)
                        and self._same_original_root(target))
            self._dispose_failed_raw(raw)
            self._outage(str(exc), preserve_initial_grant=eligible)
            return
        except (ProtocolError, CsvSourceError) as exc:
            self._dispose_failed_raw(raw)
            self.freeze_control(exc)
            self._outage(str(exc))
            return
        except BaseException:
            self._initial_recovery_grant = False
            self._initial_prepare_allowed = False
            self._dispose_failed_raw(raw)
            raise
        self._rebind_audit_needed = self.current_target is not None
        self._raw, self.current_target = raw, target
        self._reader.target = target
        if hasattr(self._reader, 'log'):
            self._reader.log.target = target
        self._prepared_mode = 'monitor'
        self._raw_epoch = None
        self._needs_baseline, self._baseline = True, None
        self._epoch += 1
        self.recovery_state = 'connected'
        if self._initial_recovery_grant and self._mode == 'control':
            self._initial_recovery_grant = False
            self._prepare_authorized(event)

    def _dispose_failed_raw(self, raw):
        try:
            raw.disconnect()
        except BaseException:
            self._initial_prepare_allowed = False
            self._initial_recovery_grant = False
            raise

    def _same_original_root(self, target):
        return (self._target is not None and target.pid == self._target.pid and
                target.hwnd == self._target.hwnd and _identity(target) == _identity(self._target))

    def _outage(self, reason, *, preserve_initial_grant=False):
        self._job_candidate = None
        self._handoff_proof = None
        self._was_ready = False
        self._initial_prepare_allowed = False
        self._initial_recovery_grant = bool(preserve_initial_grant)
        self._prepared_mode = None
        if self._raw is not None and getattr(self._raw, '_pending_save', None) is not None:
            self.freeze_control(AmbiguousWriteError('Save 처리 추적 중 연결이 변경되어 종료 증거를 보존합니다.'))
        try:
            if self._raw is not None:
                self._raw.disconnect()
        except BaseException:
            self._initial_recovery_grant = False
            raise
        finally:
            self._raw = None
            self._epoch += 1
            self._needs_baseline, self._baseline = True, None
            self._next_retry = self._clock() + self._DELAYS[min(self._failures, len(self._DELAYS)-1)]
            self._failures += 1
            self.recovery_state = 'waiting: ' + reason

    def _reconnect(self, event):
        self._check(event)
        if self._clock() < self._next_retry:
            return
        try:
            if self._discoverer is not None:
                discovered = self.discover()
            else:
                probe = self._new_raw()
                try:
                    discovered = probe.discover_matching(self._target)
                finally:
                    probe.disconnect()
            candidates = [x for x in discovered if _identity(x) == _identity(self._target)]
            if len(candidates) != 1:
                self._outage('동일 실행 파일과 장비의 창을 유일하게 확인할 수 없습니다.')
                return
            if self._initial_recovery_grant and not self._same_original_root(candidates[0]):
                self._initial_recovery_grant = False
            self._attempt(candidates[0], event)
        except (ReadTransportError, TargetIdentityError, OSError) as exc:
            preserve = (self._initial_recovery_grant and isinstance(exc, ReadTransportError)
                        and exc.operation == 'discover' and exc.actuation_possible is False
                        and exc.helper_retired is True and not self._sticky)
            self._outage(str(exc), preserve_initial_grant=preserve)
        except (ProtocolError, CsvSourceError) as exc:
            self.freeze_control(exc)
            self._outage(str(exc))

        except BaseException:
            self._initial_recovery_grant = False
            self._initial_prepare_allowed = False
            raise

    def prepare_mode(self, mode, stop_event):
        self._check(stop_event)
        if mode not in {'monitor', 'preview', 'control'}:
            raise ValueError('Unknown UI mode.')
        self._mode = mode
        # Control preparation is deferred until the observed job is authorized.
        if self._raw and mode == 'preview':
            self._initial_recovery_grant = False
            self._raw.prepare_mode(mode, stop_event)
            self._prepared_mode = mode
        elif self._raw and mode == 'control' and (self._initial_prepare_allowed or self._initial_recovery_grant):
            self._initial_prepare_allowed = False
            self._initial_recovery_grant = False
            self._prepare_authorized(stop_event)

    def _prepare_authorized(self, stop_event):
        if self._mode != 'control' or self._prepared_mode == 'control' or self._sticky:
            return
        self._control_prepare_attempted = True
        self._initial_prepare_allowed = False
        self._initial_recovery_grant = False
        try:
            self._raw.prepare_mode('control', stop_event)
            self._prepared_mode = 'control'
            if self._rebind_audit_needed:
                self._rebind_audit_needed = False
                self.suspend_control('컨트롤러 재연결 · 전체 설정을 두 번 확인합니다.')
        except (ReadTransportError, TargetIdentityError, ControlUnavailable, PersistenceUnavailable, OSError) as exc:
            if getattr(exc, 'actuation_possible', False):
                self.freeze_control(exc)
            self._outage(str(exc))
        except ProtocolError as exc:
            self.freeze_control(exc)
            self._outage(str(exc))

    def set_fault_callback(self, callback):
        """Deliver a newly latched permanent barrier synchronously, including on stop."""
        self._fault_callback = callback
        if callback is not None and self._fault is not None and not self._fault_delivered:
            self._fault_delivered = True
            callback(self._fault)

    def freeze_control(self, reason):
        self._initial_recovery_grant = False
        self._initial_prepare_allowed = False
        self._was_ready = False
        if not self._sticky:
            self._fault = reason if isinstance(reason, BaseException) else AdapterError(str(reason) or '자동 적용 보류')
            self._sticky = str(self._fault) or '자동 적용 보류'
            self._epoch += 1
            if self._fault_callback is not None and not self._fault_delivered:
                self._fault_delivered = True
                self._fault_callback(self._fault)
        self.recovery_state = 'observer: ' + self._sticky
        if self._raw:
            self._raw.suspend_control(self._sticky)

    def suspend_control(self, reason):
        self._job_candidate = None
        if self._job_handoff:
            self._handoff_proof = None
        self._was_ready = False
        self._epoch += 1
        self._needs_baseline, self._baseline = True, None
        if self._raw:
            self._raw.suspend_control(reason)

    def _job_proof(self, snap):
        values = (snap.job_auto_started, snap.job_first_started)
        if (not snap.run_key or any(isinstance(x, bool) or not isinstance(x, (int, float))
                or not math.isfinite(x) or x <= 0 for x in values)
                or values[0] > values[1] or isinstance(snap.job_first_layer, bool)
                or not isinstance(snap.job_first_layer, int) or not 0 <= snap.job_first_layer <= 1000000
                or type(snap.job_start_offset) is not int or snap.job_start_offset < 0
                or not isinstance(snap.job_log_identity, str) or not snap.job_log_identity):
            return None
        return (snap.run_key, snap.job_first_layer, snap.job_first_started, snap.job_auto_started,
                snap.job_log_identity, snap.job_start_offset)

    @staticmethod
    def _newer_job(proof, previous):
        if proof[4] == previous[4]:
            return proof[5] > previous[5]
        return proof[3] > previous[3]

    def _admit_job(self, snap, native_auto, event):
        """Forward-only admission of a new, byte-proven production run.

        A different observed run immediately fences the former run. Confirmation
        cannot span an endpoint, invalid observation, stop, or settings hold.
        Native apply independently checks the same job bytes before edit/Save.
        """
        proof = self._job_proof(snap)
        fresh = bool(native_auto and snap.measurement_valid and snap.observation_fresh
                     and snap.observation_key and isinstance(snap.layer_index, int)
                     and not isinstance(snap.layer_index, bool))
        if self._pinned_job is None and (not snap.run_key or not native_auto):
            self._job_candidate = None
            return False
        if self._pinned_job is None:
            self._pinned_job = snap.run_key
            # Establish chronology from the first proven log context, even if
            # its CSV temperature is old/missing. It does not authorize Save.
            self._pinned_proof = proof
            self._job_highwater = proof
        if (snap.run_key == self._pinned_job and not self._job_handoff
                and snap.run_key not in self._retired_jobs):
            if self._pinned_proof is not None and proof != self._pinned_proof:
                self._job_candidate = None
                if self._was_ready or self._baseline is not None:
                    self.suspend_control('현재 작업 시작 근거 재확인 대기')
                return False
            if fresh:
                self._prepare_authorized(event)
            return True
        if not snap.run_key:
            self._job_candidate = None
            self._handoff_proof = None
            return False
        if snap.run_key in self._retired_jobs or (proof and self._pinned_proof
                and not self._newer_job(proof, self._pinned_proof)):
            self._job_candidate = None
            if self._was_ready or self._baseline is not None:
                self.suspend_control('종료된 이전 작업 기록은 적용하지 않습니다.')
            return False
        if not self._job_handoff:
            self._job_handoff = True
            self._job_candidate = None
            self.suspend_control('새 프린팅 작업 확인 중 · 이전 계산값 폐기')
        aligned = (isinstance(snap.job_current_layer, int) and not isinstance(snap.job_current_layer, bool)
                   and snap.layer_index is not None
                   and snap.layer_index <= snap.job_current_layer <= snap.layer_index + 1)
        eligible = bool(not self._sticky and native_auto and proof and self._pinned_proof
                        and snap.run_key not in self._retired_jobs and snap.run_key != self._pinned_job
                        and self._newer_job(proof, self._pinned_proof)
                        and (self._job_highwater is None or proof == self._job_highwater
                             or self._newer_job(proof, self._job_highwater)))
        if not eligible:
            self._job_candidate = None
            self._handoff_proof = None
            return False
        signature = (proof, self.current_target)
        if self._handoff_proof != signature:
            # Once a credible later run is observed, no older run may re-arm,
            # even if the source disappears or temporarily regresses.
            if self._job_highwater is not None and self._job_highwater[0] != proof[0]:
                self._retired_jobs.add(self._job_highwater[0])
            self._retired_jobs.add(self._pinned_job)
            self._job_highwater = proof
            self._job_candidate = None
            self._prepare_authorized(event)
            if self._raw is not None:
                self.suspend_control('새 작업 · 전체 설정을 두 번 확인합니다.')
                self._handoff_proof = signature
            return False
        if not fresh or not aligned or not snap.control_available:
            self._job_candidate = None
            return False
        now = self._clock()
        if self._job_candidate is None or self._job_candidate[0] != signature:
            self._job_candidate = (signature, now)
            return False
        if now - self._job_candidate[1] < 1.0:
            return False
        self._pinned_job, self._pinned_proof = snap.run_key, proof
        self._job_handoff = False
        self._job_candidate = self._handoff_proof = None
        self._was_ready = False
        self._epoch += 1
        self._needs_baseline, self._baseline = True, None
        return False  # Never publish the pre-adoption observation as ready.

    def _offline(self):
        try:
            source = self._reader.read()
            sample = source.sample
            # Preserve read-only CSV display during transport recovery. These
            # points grant no control authority while the native state is unknown.
            fresh = bool(sample and sample.finished is not None
                         and time.time() - sample.finished.timestamp() >= -2)
            value = getattr(sample, 'temperature', None)
            valid = bool(getattr(sample, 'measurement_valid', True) and isinstance(value, (int,float))
                         and not isinstance(value, bool) and math.isfinite(value) and value != -1)
            return UiSnapshot(target=self._target, actual_target=self.current_target,
                layer_index=sample.layer_index if sample else None,
                t_recoating_c=sample.temperature if sample and fresh and valid else None,
                observation_key=sample.key if sample and fresh and valid and source.token else None,
                machine_state='unknown', details=source.details, run_key=source.context.run_key,
                observation_is_new=False, observation_fresh=False,
                measurement_valid=valid, invalid_reason=getattr(sample, 'invalid_reason', ''),
                slicing_image_path=getattr(sample, 'slicing_image_path', ''),
                recoating_finished=getattr(sample, 'recoating_finished', None),
                slice_lineage=getattr(sample, 'slice_lineage', ''),
                csv_generation=getattr(sample, 'source_generation', ''),
                control_epoch=self._epoch, control_recoverable=not bool(self._sticky), actuation_barrier=bool(self._sticky),
                control_unavailable_reason=self._sticky or self.recovery_state)
        except CsvReadUnavailable as exc:
            return UiSnapshot(target=self._target, actual_target=self.current_target,
                details=str(exc), control_epoch=self._epoch, control_unavailable_reason=self._sticky or str(exc), actuation_barrier=bool(self._sticky))
        except CsvSourceError as exc:
            self.freeze_control(exc)
            return UiSnapshot(target=self._target, actual_target=self.current_target,
                details=str(exc), control_epoch=self._epoch, control_unavailable_reason=self._sticky, actuation_barrier=True)

    def read_snapshot(self, stop_event):
        self._check(stop_event)
        if self._raw is None:
            self._reconnect(stop_event)
        if self._raw is None:
            return self._offline()
        try:
            snap = self._raw.read_snapshot(stop_event)
        except PersistenceUnavailable as exc:
            self.suspend_control(str(exc))
            observed = self._offline()
            return replace(observed, details=observed.details + " " + str(exc),
                control_unavailable_reason=self._sticky or str(exc))
        except CsvReadUnavailable as exc:
            self.suspend_control(str(exc))
            return UiSnapshot(target=self._target, actual_target=self.current_target,
                details=str(exc), control_epoch=self._epoch, control_unavailable_reason=self._sticky or str(exc), actuation_barrier=bool(self._sticky))
        except (ReadTransportError, TargetIdentityError, OSError) as exc:
            self._outage(str(exc))
            return self._offline()
        except (CsvSourceError, ProtocolError) as exc:
            self.freeze_control(exc)
            self._outage(str(exc))
            return self._offline()
        if self._raw_epoch != snap.control_epoch:
            self._raw_epoch = snap.control_epoch
            self._epoch += 1
            self._job_candidate = None
        if not snap.measurement_valid:
            self._was_ready = False
            if not self._needs_baseline or self._baseline is not None:
                self.suspend_control(snap.invalid_reason or '유효하지 않은 온도')
            else:
                self._raw.suspend_control(snap.invalid_reason or '유효하지 않은 온도')
            self._needs_baseline, self._baseline = True, None
        native_auto = snap.machine_state.strip().upper() in {'AUTO', 'RUNNING'}
        if self._prepared_mode != 'control' and snap.run_key:
            try:
                native_state = self._raw.read_native_state(stop_event)
                native_auto = native_state.strip().upper() in {'AUTO', 'RUNNING'}
            except ControlSuspended as exc:
                self.suspend_control(str(exc))
                return self._offline()
            except (ReadTransportError, TargetIdentityError, OSError) as exc:
                self._outage(str(exc))
                return self._offline()
            except ProtocolError as exc:
                self.freeze_control(exc)
                self._outage(str(exc))
                return self._offline()
        if not native_auto:
            self._job_candidate = None
            if self._was_ready or self._baseline is not None:
                self.suspend_control('AUTO/RUNNING 및 새 관측 대기')
        job_admitted = self._admit_job(snap, native_auto, stop_event)
        if self._raw is None:
            return self._offline()
        reconciled = snap.save_reconciliation
        if (job_admitted and native_auto and snap.control_available and snap.measurement_valid
                and snap.observation_fresh and snap.observation_is_new is True
                and isinstance(reconciled, tuple) and len(reconciled) == 3
                and reconciled != self._consumed_save_reconciliation
                and reconciled[1] == snap.run_key and type(reconciled[2]) is int
                and snap.layer_index is not None and snap.layer_index > reconciled[2]):
            self._needs_baseline = False
            self._consumed_save_reconciliation = reconciled
        if job_admitted and native_auto and self._needs_baseline and snap.measurement_valid and snap.observation_fresh and snap.observation_key and snap.layer_index is not None:
            if not snap.control_available or self._baseline is None:
                self._baseline = (snap.observation_key, snap.layer_index)
            elif (snap.observation_is_new and snap.observation_key != self._baseline[0]
                  and snap.layer_index > self._baseline[1]):
                self._needs_baseline = False
        ready = bool(job_admitted and native_auto and snap.control_available and not self._sticky and self._pinned_job
                     and snap.run_key == self._pinned_job and not self._needs_baseline)
        if ready:
            if not self._was_ready:
                self._epoch += 1
            self._failures = 0
        self._was_ready = ready
        if self._sticky:
            reason = self._sticky
        elif not snap.measurement_valid:
            reason = snap.invalid_reason or '유효하지 않은 온도 관측 · 새 측정 확인 대기'
        elif not snap.control_available:
            reason = snap.control_unavailable_reason
        elif not snap.run_key:
            reason = '현재 작업 식별 정보를 확인할 수 없어 IR 적용 대기'
        elif self._job_handoff:
            reason = '관측된 작업이 이전 작업과 달라 새 작업·설정 확인 및 새 층 대기'
        elif not self._pinned_job:
            reason = 'AUTO/RUNNING 상태와 작업 식별 확인 대기'
        elif self._needs_baseline:
            reason = '새 기준 및 새 관측 대기'
        else:
            reason = snap.control_unavailable_reason
        result = replace(snap, target=self._target, actual_target=self.current_target,
            control_available=ready, control_epoch=self._epoch,
            control_unavailable_reason=reason,
            control_recoverable=not ready and not bool(self._sticky), actuation_barrier=bool(self._sticky))
        self._last_snapshot = result
        return result

    def validate_control_epoch(self, expected):
        if expected != self._epoch or self._raw is None or self._sticky:
            raise ControlSuspended(self._sticky or '제어 상태가 변경되었습니다.')
        self._raw.validate_control_epoch(self._raw_epoch)

    def apply_ir_power(self, heater, power_percent, expected_observation_key, stop_event):
        self._check(stop_event)
        snap = self._last_snapshot
        if not snap or not snap.control_available or snap.observation_key != expected_observation_key:
            raise ControlSuspended('최신 승인 관측을 기다립니다.')
        if snap.layer_index is None:
            raise ControlSuspended('층 번호를 확인할 수 없습니다.')
        watermark = (snap.run_key, 1 if snap.coupled_output else heater)
        if snap.layer_index <= self._watermark.get(watermark, -1):
            raise ControlSuspended('이 작업의 층은 이미 적용을 시도했습니다.')
        self._watermark[watermark] = snap.layer_index
        try:
            self._raw.apply_ir_power(heater, power_percent, expected_observation_key, stop_event)
        except SaveConfirmationPending:
            self._was_ready = False
            self._needs_baseline = True
            self._baseline = (snap.observation_key, snap.layer_index)
            raise
        except AmbiguousWriteError as exc:
            self.freeze_control(exc)
            raise ControlSuspended(self._sticky) from exc
        except (ControlSuspended, ObservationSuspended):
            raise
        except ControlUnavailable as exc:
            self.suspend_control(str(exc))
            raise ControlSuspended(str(exc)) from exc
        except (CsvReadUnavailable, PersistenceUnavailable) as exc:
            self.suspend_control(str(exc))
            raise ControlSuspended(str(exc)) from exc
        except ReadTransportError as exc:
            if exc.actuation_possible:
                self.freeze_control(exc)
            self._outage(str(exc))
            raise ControlSuspended(str(exc)) from exc
        except Exception as exc:
            self.freeze_control(exc)
            raise ControlSuspended(self._sticky) from exc

    def drop_transport(self):
        self._job_candidate = None
        self._handoff_proof = None
        self._initial_recovery_grant = False
        self._initial_prepare_allowed = False
        self._was_ready = False
        try:
            if self._raw:
                self._raw.disconnect()
        finally:
            self._raw = None
            self._prepared_mode = None
            self._epoch += 1
            self._needs_baseline, self._baseline = True, None
            self._next_retry = self._clock()

    def disconnect(self):
        if self._stop is None or self._stop.is_set():
            self._stopped = True
        self.drop_transport()
