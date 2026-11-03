"""Continuously observe a verified job through read-only connection failures."""
from __future__ import annotations

import ntpath
import time
from dataclasses import replace

try:
    from .adapter_contracts import (AdapterError, AmbiguousWriteError, ControlSuspended,
        ControlUnavailable, ObservationSuspended, TargetIdentityError, ReadTransportError,
        ProtocolError, UiSnapshot)
    from .csv_source import MonitoringCsvReader, CsvSourceError, CsvReadUnavailable
    from .windows_ui_adapter import WindowsUiAdapter
    from .sequence_persistence import PersistenceUnavailable
except ImportError:
    from adapter_contracts import (AdapterError, AmbiguousWriteError, ControlSuspended,
        ControlUnavailable, ObservationSuspended, TargetIdentityError, ReadTransportError,
        ProtocolError, UiSnapshot)
    from csv_source import MonitoringCsvReader, CsvSourceError, CsvReadUnavailable
    from windows_ui_adapter import WindowsUiAdapter
    from sequence_persistence import PersistenceUnavailable



def _identity(target):
    return (ntpath.normcase(ntpath.normpath(target.image_path)), target.machine.upper())


class ResilientUiAdapter:
    """One logical session, retaining CSV evidence and write barriers on reconnect."""
    _DELAYS = (2, 5, 15, 30, 60)

    def __init__(self, raw_factory=WindowsUiAdapter, discoverer=None,
                 source_factory=MonitoringCsvReader, clock=time.monotonic):
        self._raw_factory, self._discoverer = raw_factory, discoverer
        self._source_factory, self._clock = source_factory, clock
        self._raw = self._reader = None
        self._target = self.current_target = None
        self._mode = 'monitor'
        self._stop = None
        self._stopped = False
        self._next_retry = 0
        self._failures = 0
        self._sticky = ''
        self._fault = None
        self._fault_callback = None
        self._fault_delivered = False
        self._pinned_job = None
        self._watermark = {}
        self._prepared_mode = None
        self._initial_prepare_allowed = True
        self._last_snapshot = None
        self._was_ready = False
        self._epoch = 0
        self._raw_epoch = None
        self._needs_baseline = True
        self._baseline = None
        self.recovery_state = 'disconnected'

    def _check(self, event):
        if self._stopped or (event is not None and event.is_set()):
            raise AdapterError('관측이 중지되었습니다.')

    def _new_raw(self):
        return self._raw_factory(source_factory=lambda target: self._reader)

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
            raise TargetIdentityError('기존 관측 대상이 변경되었습니다.')
        if not target.image_path or target.machine not in {'M1', 'S3'}:
            raise TargetIdentityError('장비와 실행 파일 경로를 확인해야 합니다.')
        self._target, self._stop = target, stop_event
        if self._reader is None:
            self._reader = self._source_factory(target)
        if self._raw is None:
            self._attempt(target, stop_event)

    def _attempt(self, target, event):
        self._check(event)
        raw = self._new_raw()
        try:
            raw.connect(target, event)
            raw.prepare_mode('monitor', event)
        except (ReadTransportError, TargetIdentityError, ControlUnavailable, PersistenceUnavailable, OSError) as exc:
            if getattr(exc, "actuation_possible", False):
                self.freeze_control(exc)
            raw.disconnect()
            self._outage(str(exc))
            return
        except (ProtocolError, CsvSourceError) as exc:
            raw.disconnect()
            self.freeze_control(exc)
            self._outage(str(exc))
            return
        self._raw, self.current_target = raw, target
        self._reader.target = target
        if hasattr(self._reader, 'log'):
            self._reader.log.target = target
        self._prepared_mode = 'monitor'
        self._raw_epoch = None
        self._needs_baseline, self._baseline = True, None
        self._epoch += 1
        self.recovery_state = 'connected'

    def _outage(self, reason):
        self._was_ready = False
        self._initial_prepare_allowed = False
        self._prepared_mode = None
        if self._raw is not None:
            self._raw.disconnect()
            self._raw = None
        self._epoch += 1
        self._needs_baseline, self._baseline = True, None
        self._next_retry = self._clock() + self._DELAYS[min(self._failures, len(self._DELAYS)-1)]
        self._failures += 1
        self.recovery_state = 'waiting: ' + reason

    def _reconnect(self, event):
        if self._clock() < self._next_retry:
            return
        self._check(event)
        try:
            candidates = [x for x in self.discover() if _identity(x) == _identity(self._target)]
            if len(candidates) != 1:
                self._outage('동일 실행 파일과 장비의 창을 유일하게 확인할 수 없습니다.')
                return
            self._attempt(candidates[0], event)
        except (ReadTransportError, TargetIdentityError, OSError) as exc:
            self._outage(str(exc))
        except (ProtocolError, CsvSourceError) as exc:
            self.freeze_control(exc)
            self._outage(str(exc))

    def prepare_mode(self, mode, stop_event):
        self._check(stop_event)
        if mode not in {'monitor', 'preview', 'control'}:
            raise ValueError('Unknown UI mode.')
        self._mode = mode
        # Control preparation is deferred until the observed job is authorized.
        if self._raw and mode == 'preview':
            self._raw.prepare_mode(mode, stop_event)
            self._prepared_mode = mode
        elif self._raw and mode == 'control' and self._initial_prepare_allowed:
            self._initial_prepare_allowed = False
            self._prepare_authorized(stop_event)

    def _prepare_authorized(self, stop_event):
        if self._mode != 'control' or self._prepared_mode == 'control' or self._sticky:
            return
        try:
            self._raw.prepare_mode('control', stop_event)
            self._prepared_mode = 'control'
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
        self._was_ready = False
        self._epoch += 1
        self._needs_baseline, self._baseline = True, None
        if self._raw:
            self._raw.suspend_control(reason)

    def _offline(self):
        try:
            source = self._reader.read()
            sample = source.sample
            fresh = bool(sample and sample.finished is not None and -2 <= time.time() - sample.finished.timestamp() <= MonitoringCsvReader.MAX_NEW_AGE_SECONDS)
            valid = getattr(sample, 'measurement_valid', True)
            return UiSnapshot(target=self._target, actual_target=self.current_target,
                layer_index=sample.layer_index if sample else None,
                t_recoating_c=sample.temperature if sample and fresh and valid else None,
                observation_key=sample.key if sample and fresh and valid and source.token else None,
                machine_state='unknown', details=source.details, run_key=source.context.run_key,
                observation_is_new=bool(source.is_new and fresh and valid), observation_fresh=fresh,
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
        if not snap.measurement_valid:
            self._was_ready = False
            if not self._needs_baseline or self._baseline is not None:
                self.suspend_control(snap.invalid_reason or '유효하지 않은 온도')
            else:
                self._raw.suspend_control(snap.invalid_reason or '유효하지 않은 온도')
            self._needs_baseline, self._baseline = True, None
        native_auto = snap.machine_state.strip().upper() in {'AUTO', 'RUNNING'}
        if (self._prepared_mode != 'control' and snap.run_key
                and (self._pinned_job is None or snap.run_key == self._pinned_job)):
            try:
                native_state = self._raw.read_native_state(stop_event)
                native_auto = native_state.strip().upper() in {'AUTO', 'RUNNING'}
            except (ReadTransportError, TargetIdentityError, OSError) as exc:
                self._outage(str(exc))
                return self._offline()
            except ProtocolError as exc:
                self.freeze_control(exc)
                self._outage(str(exc))
                return self._offline()
        if self._pinned_job is None and snap.run_key and native_auto:
            self._pinned_job = snap.run_key
        if (self._pinned_job and snap.run_key == self._pinned_job and native_auto
                and snap.measurement_valid and snap.observation_fresh and snap.observation_key):
            self._prepare_authorized(stop_event)
        if self._raw is None:
            return self._offline()
        if self._needs_baseline and snap.measurement_valid and snap.observation_fresh and snap.observation_key and snap.layer_index is not None:
            if not snap.control_available or self._baseline is None:
                self._baseline = (snap.observation_key, snap.layer_index)
            elif (snap.observation_is_new and snap.observation_key != self._baseline[0]
                  and snap.layer_index != self._baseline[1]):
                self._needs_baseline = False
        ready = bool(snap.control_available and not self._sticky and self._pinned_job
                     and snap.run_key == self._pinned_job and not self._needs_baseline)
        if ready:
            if not self._was_ready:
                self._epoch += 1
            self._failures = 0
        self._was_ready = ready
        result = replace(snap, target=self._target, actual_target=self.current_target,
            control_available=ready, control_epoch=self._epoch,
            control_unavailable_reason=self._sticky or ('새 기준 및 새 관측 대기' if not ready else snap.control_unavailable_reason),
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
        self._was_ready = False
        if self._raw:
            self._raw.disconnect()
            self._raw = None
        self._prepared_mode = None
        self._epoch += 1
        self._needs_baseline, self._baseline = True, None
        self._next_retry = self._clock()

    def disconnect(self):
        if self._stop is None or self._stop.is_set():
            self._stopped = True
        self.drop_transport()
