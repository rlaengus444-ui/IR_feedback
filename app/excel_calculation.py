"""Freshness and admission rules for the asynchronous Excel owner."""
from __future__ import annotations

import time
from excel_async import AsyncWorkbookGroup
from excel_workbook import WorkbookController


class ExcelCalculation:
    def __init__(self, spec, target, maximum_ir_percent, stop_event,
                 controller_factory=WorkbookController, group_factory=AsyncWorkbookGroup,
                 clock=time.monotonic, retry_delays=(2, 5, 15, 30, 60)):
        self.spec, self.target, self.maximum_ir_percent = spec, target, maximum_ir_percent
        self.stop_event, self.controller_factory, self.group_factory = stop_event, controller_factory, group_factory
        self._clock, self._retry_delays = clock, tuple(retry_delays)
        self._failures = 0
        self.fault_generation = 0
        self._recovery = None
        self._topology_wait = False
        self._retry_at = None
        self._prepared = None
        self.group = None
        self.pending = False
        self.latched_reason = None
        self.diagnostic_reason = None
        self.accepted_stamp = None
        self.accepted_cap_stamp = None
        self._accepted_identity = None
        self.history_reset_count = 0
        self.last_history_reset = None
        self.last_calculation_seconds = None
        self._ticket = 0
        self._context = None
        self._completed = None
        self._completed_elapsed = None
        self._baseline = None
        self._needs_baseline = True
        self._clean = True
        self._resetting = False
        self._environment = None
        self._heaters = None
        self._context_restart = False
        self._resume_context = None
        self._restart_floor = None
        self._eligible = False

    def _identity(self, snapshot):
        return (getattr(snapshot, 'csv_generation', None), snapshot.observation_key,
                snapshot.run_key, snapshot.layer_index, snapshot.control_epoch)

    def _context_for(self, snapshot, powers, heaters, target_stamp, image_epoch, cap_stamp=None):
        return (self._identity(snapshot), target_stamp, image_epoch,
                tuple((heater, powers[heater]) for heater in heaters), self.maximum_ir_percent, cap_stamp)

    def reset(self, reason='제어 조건 재확인'):
        was_pending = self.pending
        invalidated_identity = self._context[0] if self._context is not None else None
        accepted_layer = self._accepted_identity[3] if self._accepted_identity is not None else None
        if type(accepted_layer) is not int or accepted_layer < 0:
            accepted_layer = None
        else:
            accepted_layer += 1
        info = {'reason': reason, 'had_pending': self.pending,
                'had_unconsumed': self._completed is not None,
                'had_accepted': self._accepted_identity is not None,
                'accepted_layer': accepted_layer}
        self.pending = False
        self._context = self._completed = self.accepted_stamp = None
        self.accepted_cap_stamp = None
        self._accepted_identity = None
        self._needs_baseline = True
        self._baseline = None
        if self.group is not None and not self.latched_reason and not self._resetting and not self._clean:
            self._ticket += 1
            if was_pending and self.group.state == 'busy':
                # A step has no time cutoff. Superseded work must be retired,
                # not left blocking a reset queued behind that same step.
                self.group.close()
                self._context_restart = True
                self._restart_floor = invalidated_identity
                self._resume_context = None
                self._recovery = 'retiring'
                self._resetting = True
                self.history_reset_count += 1
                self.last_history_reset = info
            elif self.group.reset(self._ticket):
                self._resetting = True
                self.history_reset_count += 1
                self.last_history_reset = info
        self._clean = True

    def watch(self, snapshot, *, eligible, powers, heaters, target_stamp, image_epoch, features=None,
              allow_idle_history=False, cap_stamp=None):
        heaters = tuple(heaters)
        valid = (snapshot.observation_key is not None and snapshot.layer_index is not None)
        identity = self._identity(snapshot) if valid else None
        self._eligible = bool(eligible and valid)
        if self._recovery in ('retiring', 'backoff'):
            if valid:
                self._baseline = identity
            if self._context_restart:
                self._resume_context = self._context_for(snapshot, powers, heaters, target_stamp, image_epoch, cap_stamp) if self._eligible else None
            self._environment = (getattr(snapshot, 'csv_generation', None), snapshot.run_key,
                                 snapshot.control_epoch, image_epoch)
            self._topology_wait = heaters != self._heaters
            if self._topology_wait:
                return
            if self._recovery == 'retiring':
                try:
                    status = self.group.retirement_status()
                except Exception:
                    status = {'verified': False}
                prepared = getattr(self.group, 'prepared_workbook', None)
                if status.get('verified') and prepared is not None:
                    self._prepared = prepared
                    delay = 0 if self._context_restart else self._retry_delays[min(self._failures - 1, len(self._retry_delays) - 1)]
                    self._retry_at = self._clock() + delay
                    self._recovery = 'backoff'
            if self._recovery == 'backoff' and self._clock() >= self._retry_at and not self.stop_event.is_set():
                self.group = self.group_factory(self.spec, self.target, self.maximum_ir_percent,
                                                self.stop_event, factory=self.controller_factory,
                                                prepared_workbook=self._prepared)
                self.group.start(self._heaters)
                self._recovery = 'starting'
                self._resetting = False
                self._clean = True
                self._needs_baseline = True
            else:
                return
        if self.group is None:
            if self.latched_reason or self.stop_event.is_set():
                return
            self._heaters = heaters
            self.group = self.group_factory(self.spec, self.target, self.maximum_ir_percent,
                                            self.stop_event, factory=self.controller_factory)
            self.group.start(heaters)
        if heaters != self._heaters:
            # A workbook group owns exactly the heater topology it was started with.
            self.latched_reason = '히터 구성 변경 확인 대기 · 자동 적용 일시정지'
            self.diagnostic_reason = self.latched_reason
            self.group.close()
            self.pending = False
            self._completed = None
            self._accepted_identity = None
            self.accepted_cap_stamp = None
            return
        environment = (getattr(snapshot, 'csv_generation', None), snapshot.run_key,
                       snapshot.control_epoch, image_epoch)
        if self._environment is not None and environment != self._environment:
            self.reset('CSV 작업·설정·이미지 환경 변경')
        self._environment = environment
        context = self._context_for(snapshot, powers, heaters, target_stamp, image_epoch, cap_stamp) if valid else None
        if (self.pending or self._completed is not None) and (not self._eligible or context != self._context):
            self.reset('진행 중인 계산의 관측 또는 설정 변경')
        event = self.group.poll()
        if event:
            kind = event['kind']
            if kind in ('timeout', 'error'):
                error = event['error']
                from excel_workbook import WorkbookTransportError
                if kind == 'timeout' or isinstance(error, WorkbookTransportError):
                    self._context_restart = False
                    self._resume_context = None
                    self._restart_floor = None
                    self.latched_reason = ('Excel 응답 지연' if kind == 'timeout' else 'Excel 통신 오류') + ' · 자동 적용 일시정지'
                    self.diagnostic_reason = (f'{self.latched_reason} · operation={getattr(error, "operation", None)}'
                                              f' · elapsed={getattr(error, "elapsed", None)}'
                                              f' · deadline={getattr(error, "deadline", None)}'
                                              f' · phase={getattr(error, "phase", None)}'
                                              f' · process_state={getattr(error, "process_state", None)}'
                                              f' · {error}')
                    self.pending = False
                    self._completed = None
                    self._context = self.accepted_stamp = None
                    self.accepted_cap_stamp = None
                    self._accepted_identity = None
                    self._failures += 1
                    self.fault_generation += 1
                    self._recovery = 'retiring'
                    self._needs_baseline = True
                    self._baseline = identity
                else:
                    raise error
            elif kind == 'ready':
                if not self._resetting or event.get('ticket') == self._ticket:
                    self._resetting = False
                    self._clean = True
                    self._needs_baseline = True
                    if self._recovery == 'starting':
                        self._recovery = None
                        self.latched_reason = None
                        if self._context_restart:
                            floor = self._restart_floor
                            newer = (identity is not None and floor is not None
                                and (identity[0],identity[2],identity[4]) == (floor[0],floor[2],floor[4])
                                and identity[1] != floor[1] and identity[3] > floor[3]
                                and snapshot.observation_is_new is True)
                            if self._eligible and context == self._resume_context and newer:
                                self._baseline = None
                                self._needs_baseline = False
                            self._context_restart = False
                            self._resume_context = None
                            self._restart_floor = None
            elif kind == 'result' and self.pending:
                if event.get('ticket') == self._ticket and self._eligible and context == self._context:
                    self.pending = False
                    self._completed = event['results']
                    self._completed_elapsed = event.get('elapsed_seconds')
                else:
                    self.reset('계산 결과의 관측 또는 설정 불일치')
        if valid and (self.group.state != 'ready' or self._resetting):
            self._baseline = identity
        if not self._eligible:
            expired_key = getattr(snapshot, 'expired_observation_key', None)
            expired_identity = (getattr(snapshot, 'csv_generation', None), expired_key,
                                snapshot.run_key, snapshot.layer_index, snapshot.control_epoch)
            idle_history = (allow_idle_history is True and snapshot.observation_key is None
                            and expired_key is not None and self._accepted_identity is not None
                            and expired_identity == self._accepted_identity
                            and getattr(snapshot, 'measurement_valid', None) is True
                            and getattr(snapshot, 'observation_fresh', None) is False
                            and snapshot.observation_is_new is False
                            and self.group.state == 'ready' and not self._resetting
                            and not self.pending and self._completed is None)
            if not idle_history:
                if not self._clean or self.pending or self._completed is not None:
                    self.reset('관측 또는 제어 조건 충족 안 됨')
                if valid:
                    self._baseline = identity
                self._needs_baseline = True
        elif self._needs_baseline and self.group.state == 'ready' and not self._resetting:
            if self._baseline is None:
                self._baseline = identity
            self._needs_baseline = False

    def calculate(self, snapshot, powers, heaters, target_stamp, image_epoch, features=None, cap_stamp=None):
        if self.group is None or self.latched_reason or not self._eligible:
            return None
        context = self._context_for(snapshot, powers, tuple(heaters), target_stamp, image_epoch, cap_stamp)
        if self._completed is not None:
            if context != self._context:
                self.reset('완료 결과의 관측 또는 설정 불일치')
                return None
            result, self._completed = self._completed, None
            self.last_calculation_seconds = self._completed_elapsed
            self._completed_elapsed = None
            self.accepted_stamp = (target_stamp, image_epoch)
            self.accepted_cap_stamp = cap_stamp
            self._accepted_identity = context[0]
            self._failures = 0
            return result
        if (self.pending or self._needs_baseline or self._resetting or
                self.group.state != 'ready' or self._identity(snapshot) == self._baseline or
                (self._baseline is not None and snapshot.layer_index == self._baseline[3]) or
                snapshot.observation_is_new is not True):
            return None
        self._ticket += 1
        if self.group.submit(self._ticket, snapshot.layer_index + 1, snapshot.t_recoating_c,
                             powers, target_stamp[1], features):
            self._context = context
            self.pending = True
            self._clean = False
            self.watch(snapshot, eligible=True, powers=powers, heaters=heaters,
                       target_stamp=target_stamp, image_epoch=image_epoch, features=features, cap_stamp=cap_stamp)
            if self._completed is not None:
                return self.calculate(snapshot, powers, heaters, target_stamp, image_epoch, features, cap_stamp)
        return None

    def waiting_status(self):
        if self._topology_wait:
            return '히터 구성 변경 확인 대기 · 자동 적용 일시정지'
        if self.group is not None and self.group.state == 'waiting-retirement':
            return '이전 Excel 종료 확인 대기 · 자동 적용 일시정지'
        if self._recovery == 'retiring':
            return 'Excel 종료 확인 대기 · 자동 적용 일시정지'
        if self._recovery == 'backoff':
            return f'Excel 자동 복구 대기 · {max(0, self._retry_at - self._clock()):.0f}초 · 자동 적용 일시정지'
        if self._recovery == 'starting':
            return 'Excel 자동 복구 중 · 자동 적용 일시정지'
        if self.latched_reason:
            return self.latched_reason
        if self.pending:
            status = 'Excel 계산 중'
        elif self._resetting:
            status = 'Excel 이력 초기화 중'
        elif self.group is None or self.group.state != 'ready':
            status = 'Excel 초기화 중'
        else:
            return '새 층의 T_recoating 기록 대기'
        info = self.describe()
        operation = {'init': '초기화', 'reset': '초기화', 'process': '계산',
                     'step': '계산'}.get(info.get('controller_operation') or info.get('operation'), '')
        raw_phase = str(info.get('phase') or '')
        phase = {'send': '요청 전송', 'start': '시작', 'open_workbook': '통합 문서 열기',
                 'clear_inputs': '입력 초기화', 'inputs': '입력 기록', 'calculate': '수식 계산',
                 'freeze': '결과 확정'}.get(raw_phase.split(':')[-1], '')
        if not phase and raw_phase in ('1', '2'):
            phase = '히터 ' + raw_phase
        elapsed = info.get('elapsed', 0)
        details = ' · '.join(str(part) for part in (operation, phase) if part)
        return f'{status} · {details + " · " if details else ""}{elapsed:.0f}초'

    def can_retain_after_expiry(self, snapshot, powers, heaters, target_stamp, image_epoch, cap_stamp):
        """Only a fully consumed result in its unchanged actual-input context."""
        return bool(self.group is not None and self.group.state == 'ready'
            and not self.latched_reason and not self.pending and self._completed is None
            and not self._resetting and self._recovery is None
            and self._accepted_identity == self._identity(snapshot)
            and self.accepted_stamp == (target_stamp, image_epoch)
            and self.accepted_cap_stamp == cap_stamp
            and self._context == self._context_for(snapshot, powers, tuple(heaters), target_stamp, image_epoch, cap_stamp))

    def describe(self):
        result = dict(self.group.describe()) if self.group is not None else {'state': 'new'}
        result.update(recovery_state=self._recovery, fault_generation=self.fault_generation,
                      history_reset_count=self.history_reset_count,
                      last_history_reset=(dict(self.last_history_reset) if self.last_history_reset is not None else None),
                      failure_streak=self._failures,
                      next_retry_seconds=(max(0.0, self._retry_at - self._clock())
                                          if self._recovery == 'backoff' else None))
        return result

    def close(self):
        if self.group is not None:
            self.group.close()
