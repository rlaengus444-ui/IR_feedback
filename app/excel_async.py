"""Single owner for asynchronous, isolated workbook calculations.

Only the owner thread performs ordinary controller operations. Cancellation may
force-close a registered controller from a separate daemon to unblock that owner.
"""
from __future__ import annotations

import inspect
import threading
import time

from excel_workbook import WorkbookController, WorkbookTimeout


class _CombinedStop:
    def __init__(self, local, external):
        self.local, self.external = local, external

    def is_set(self):
        return self.local.is_set() or (self.external is not None and self.external.is_set())


class AsyncWorkbookGroup:
    def __init__(self, spec, target_temperature, maximum_ir_percent, stop_event, factory=WorkbookController):
        self.spec, self.target = spec, target_temperature
        self.maximum_ir_percent, self.factory = maximum_ir_percent, factory
        self._local_stop = threading.Event()
        self._stop = _CombinedStop(self._local_stop, stop_event)
        self._condition = threading.Condition()
        self._controllers = {}
        self._partial = None
        self._heaters = ()
        self._pending = None
        self._event = None
        self._generation = 0
        self._state = 'new'
        self._operation = None
        self._started = None
        self._deadline = None
        self._phase = None
        self._active_ticket = None
        self._thread = None
        self._last_layers = {}
        self._active_controller = None
        self._abort_started = False
        self._owner_done = False

    @property
    def state(self):
        with self._condition:
            return self._state

    def start(self, heaters):
        with self._condition:
            if self._state != 'new' or self._stop.is_set():
                return False
            self._heaters = tuple(heaters)
            if not self._heaters:
                raise ValueError('At least one heater is required')
            self._state = 'initializing'
            self._pending = ('init', self._generation, None, None)
            self._thread = threading.Thread(target=self._run, daemon=True, name='excel-owner')
            self._thread.start()
            return True

    def submit(self, ticket, layer, temperature, powers, target, slice_features=None):
        with self._condition:
            if self._state != 'ready' or self._pending is not None or self._stop.is_set():
                return False
            if set(powers) != set(self._heaters):
                return False
            self._generation += 1
            self._event = None
            self._pending = ('process', self._generation, ticket,
                             (layer, temperature, dict(powers), target, None if slice_features is None else dict(slice_features)))
            self._state = 'busy'
            self._condition.notify()
            return True

    def reset(self, ticket=None, target=None):
        with self._condition:
            if self._state in ('new', 'closed', 'timeout', 'error') or self._stop.is_set():
                return False
            self._generation += 1
            self._event = None
            self._pending = ('reset', self._generation, ticket, target)
            self._state = 'resetting'
            self._condition.notify()
            return True

    def _begin(self, operation, phase, seconds, controller=None):
        with self._condition:
            self._operation, self._phase = operation, phase
            self._active_controller = controller
            self._started = time.monotonic()
            self._deadline = self._started + seconds

    def _check_deadline(self):
        diagnostic = self._diagnostic()
        with self._condition:
            if self._deadline is not None and time.monotonic() >= self._deadline:
                raise self._timeout_locked(diagnostic)

    def _diagnostic(self):
        operation, phase, state = None, None, 'unknown'
        controller = self._active_controller or self._partial
        lock = getattr(controller, '_lifecycle_lock', None)
        if lock is not None and lock.acquire(blocking=False):
            try:
                operation = getattr(controller, '_last_operation', None)
                phase = getattr(controller, '_phase', None)
                proc = getattr(controller, '_proc', None)
            finally:
                lock.release()
            try:
                state = 'missing' if proc is None else ('running' if proc.poll() is None else 'exited')
            except Exception:
                state = 'unknown'
        return controller, operation, phase, state

    def _timeout_locked(self, diagnostic):
        controller, operation, phase, state = diagnostic
        if controller is not (self._active_controller or self._partial):
            operation, phase, state = None, None, 'unknown'
        return WorkbookTimeout('Excel operation timed out', operation=operation or self._operation,
                               elapsed=time.monotonic() - self._started,
                               deadline=self._deadline - self._started,
                               phase=phase or self._phase, process_state=state)

    def _register(self, controller):
        with self._condition:
            self._partial = controller
            cancelled = self._stop.is_set()
            self._condition.notify_all()
        if cancelled:
            self._abort()

    def _abort(self):
        with self._condition:
            if self._abort_started:
                self._condition.notify_all()
                return
            self._abort_started = True
        def close_all():
            seen = set()
            while True:
                with self._condition:
                    controllers = list(self._controllers.values())
                    if self._partial is not None:
                        controllers.append(self._partial)
                    controllers = [c for c in controllers if id(c) not in seen]
                    if not controllers:
                        if self._owner_done:
                            return
                        self._condition.wait()
                        continue
                    seen.update(id(c) for c in controllers)
                for controller in controllers:
                    try:
                        controller.close(force=True)
                    except Exception:
                        pass
        threading.Thread(target=close_all, daemon=True, name='excel-abort').start()

    def close(self):
        with self._condition:
            if self._state == 'closed':
                return
            self._state = 'closed'
            self._generation += 1
            self._pending = self._event = None
            self._local_stop.set()
            self._condition.notify_all()
        self._abort()

    def describe(self):
        with self._condition:
            operation, phase = self._operation, self._phase
            controller = self._active_controller or self._partial
            lock = getattr(controller, '_lifecycle_lock', None)
            if lock is not None and lock.acquire(blocking=False):
                try:
                    operation = getattr(controller, '_last_operation', None) or operation
                    phase = getattr(controller, '_phase', None) or phase
                finally:
                    lock.release()
            return {'state': self._state, 'operation': self._operation,
                    'elapsed': max(0.0, time.monotonic() - self._started) if self._started is not None else 0.0,
                    'phase': phase, 'controller_operation': operation}

    def poll(self):
        abort = False
        diagnostic = self._diagnostic()
        with self._condition:
            if self._state not in ('new', 'closed', 'timeout', 'error'):
                if self._stop.is_set():
                    self._state = 'closed'
                    self._generation += 1
                    self._pending = self._event = None
                    self._local_stop.set()
                    self._condition.notify_all()
                    abort = True
                elif self._deadline is not None and time.monotonic() >= self._deadline:
                    error = self._timeout_locked(diagnostic)
                    self._state = 'timeout'
                    self._generation += 1
                    self._pending = None
                    self._event = {'kind': 'timeout', 'ticket': self._active_ticket, 'error': error}
                    self._local_stop.set()
                    self._condition.notify_all()
                    abort = True
            event, self._event = self._event, None
        if abort:
            self._abort()
        return event

    def _make_controller(self):
        kwargs = {'stop_event': self._stop, 'maximum_ir_percent': self.maximum_ir_percent}
        try:
            params = inspect.signature(self.factory).parameters.values()
            if any(p.name == 'on_created' or p.kind == p.VAR_KEYWORD for p in params):
                kwargs['on_created'] = self._register
        except (TypeError, ValueError):
            kwargs['on_created'] = self._register
        controller = self.factory(self.spec, self.target, **kwargs)
        self._register(controller)
        return controller

    def _run(self):
        try:
            while True:
                with self._condition:
                    while self._pending is None and not self._stop.is_set():
                        self._condition.wait(timeout=0.1)
                    if self._stop.is_set():
                        break
                    kind, generation, ticket, payload = self._pending
                    self._pending = None
                    self._active_ticket = ticket
                try:
                    if kind == 'init':
                        for heater in self._heaters:
                            self._begin('init', str(heater), 90.0)
                            controller = self._make_controller()
                            self._check_deadline()
                            with self._condition:
                                self._controllers[heater] = controller
                                self._partial = None
                                self._condition.notify_all()
                            if self._stop.is_set():
                                break
                        event = {'kind': 'ready', 'ticket': ticket}
                    elif kind == 'reset':
                        if payload is not None:
                            self.target = payload
                        for heater, controller in self._controllers.items():
                            self._begin('reset', str(heater), 30.0, controller)
                            controller.set_target_temperature(self.target)
                            controller.reset()
                            self._check_deadline()
                            if self._stop.is_set():
                                break
                        self._last_layers.clear()
                        event = {'kind': 'ready', 'ticket': ticket}
                    else:
                        layer, temperature, powers, target, features = payload
                        calculation_started = time.monotonic()
                        results = {}
                        for heater, controller in self._controllers.items():
                            previous = self._last_layers.get(heater)
                            budget = 45.0 if previous is not None and layer != previous + 1 else 8.0
                            self._begin('process', str(heater), budget, controller)
                            controller.set_target_temperature(target)
                            results[heater] = controller.process(layer, temperature, powers[heater], slice_features=features)
                            self._check_deadline()
                            self._last_layers[heater] = layer
                            with self._condition:
                                if self._stop.is_set() or generation != self._generation:
                                    break
                        event = {'kind': 'result', 'ticket': ticket, 'results': results,
                                 'elapsed_seconds': time.monotonic() - calculation_started}
                    diagnostic = self._diagnostic()
                    with self._condition:
                        if self._deadline is not None and time.monotonic() >= self._deadline:
                            raise self._timeout_locked(diagnostic)
                        self._deadline = self._started = self._operation = self._phase = None
                        if generation == self._generation and not self._stop.is_set():
                            self._event = event
                            self._state = 'ready'
                except Exception as error:
                    with self._condition:
                        self._deadline = self._started = self._operation = self._phase = None
                        if not self._stop.is_set():
                            self._generation += 1
                            self._pending = None
                            self._state = 'timeout' if isinstance(error, WorkbookTimeout) else 'error'
                            self._event = {'kind': self._state, 'ticket': ticket, 'error': error}
                            self._local_stop.set()
                    self._abort()
                    break
        finally:
            # This can block only the daemon owner, never the polling thread.
            for controller in list(self._controllers.values()):
                try:
                    controller.close(force=True)
                except Exception:
                    pass
            with self._condition:
                self._owner_done = True
                self._condition.notify_all()
