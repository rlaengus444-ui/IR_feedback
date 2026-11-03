import threading
import unittest
import time
from types import SimpleNamespace

from excel_calculation import ExcelCalculation
from excel_workbook import WorkbookTimeout, WorkbookTransportError


class Group:
    def __init__(self, *args, **kwargs):
        self.state = 'new'
        self.events = []
        self.jobs = []
        self.resets = []
        self.closed = False

    def start(self, heaters):
        self.state = 'initializing'
        self.heaters = heaters
        return True

    def poll(self):
        if not self.events:
            return None
        event = self.events.pop(0)
        if event['kind'] in ('ready', 'result'):
            self.state = 'ready'
        return event

    def submit(self, *args):
        self.jobs.append(args)
        self.state = 'busy'
        return True

    def reset(self, ticket):
        self.resets.append(ticket)
        self.state = 'resetting'
        return True

    def close(self):
        self.closed = True

    def describe(self):
        return {'state': self.state}


def row(key, *, new=True, **changes):
    values = dict(observation_key=key, layer_index=int(key), t_recoating_c=80,
                  observation_is_new=new, csv_generation='g', run_key='r', control_epoch=1)
    values.update(changes)
    return SimpleNamespace(**values)


class CalculationTests(unittest.TestCase):
    def setUp(self):
        self.calc = ExcelCalculation(None, 100, 80, threading.Event(), group_factory=Group)
        self.kw = dict(eligible=True, powers={1: 20, 2: 30}, heaters=(1, 2),
                       target_stamp=(1, 100), image_epoch=None)
        self.calc.watch(row('1'), **self.kw)
        self.group = self.calc.group
        self.group.events.append({'kind': 'ready'})
        self.calc.watch(row('1'), **self.kw)

    def watch(self, snapshot, **overrides):
        self.calc.watch(snapshot, **(self.kw | overrides))

    def calculate(self, snapshot, **overrides):
        kw = self.kw | overrides
        return self.calc.calculate(snapshot, kw['powers'], kw['heaters'], kw['target_stamp'], kw['image_epoch'])

    def test_ready_baseline_then_atomic_result_on_same_ticket(self):
        self.assertIsNone(self.calculate(row('1')))
        self.assertIsNone(self.calculate(row('2')))
        self.assertTrue(self.calc.pending)
        ticket = self.group.jobs[-1][0]
        self.group.events.append({'kind': 'result', 'ticket': ticket, 'results': {1: 5, 2: 6}, 'elapsed_seconds': 1.25})
        self.watch(row('2', new=False))
        self.assertEqual(self.calculate(row('2', new=False)), {1: 5, 2: 6})
        self.assertEqual(self.calc.accepted_stamp, ((1, 100), None))
        self.assertEqual(self.calc.last_calculation_seconds, 1.25)

    def test_new_row_while_busy_discards_and_resets_then_requires_fresh(self):
        self.calculate(row('2'))
        ticket = self.group.jobs[-1][0]
        self.watch(row('3'))
        self.assertEqual(len(self.group.resets), 1)
        self.watch(row('3'), eligible=False)
        self.assertEqual(len(self.group.resets), 1)
        self.group.events.append({'kind': 'result', 'ticket': ticket, 'results': {1: 7}})
        self.watch(row('3'), eligible=False)
        self.group.events.append({'kind': 'ready', 'ticket': self.group.resets[-1]})
        self.watch(row('3'))
        self.assertIsNone(self.calculate(row('3')))
        self.assertIsNone(self.calculate(row('4')))
        self.assertEqual(len(self.group.jobs), 2)

    def test_target_aba_invalidates_pending(self):
        self.calculate(row('2'))
        self.watch(row('2'), target_stamp=(2, 110))
        self.watch(row('2'), target_stamp=(3, 100))
        self.assertFalse(self.calc.pending)
        self.assertEqual(len(self.group.resets), 1)

    def test_idle_target_change_retains_accepted_history(self):
        self.calculate(row('2'))
        ticket = self.group.jobs[-1][0]
        self.group.events.append({'kind': 'result', 'ticket': ticket, 'results': {1: 5, 2: 6}})
        self.watch(row('2', new=False))
        self.assertEqual(self.calculate(row('2', new=False)), {1: 5, 2: 6})
        self.watch(row('3'), target_stamp=(2, 110))
        self.assertEqual(self.group.resets, [])
        self.calculate(row('3'), target_stamp=(2, 110))
        self.assertEqual(self.group.jobs[-1][4], 110)
        self.assertEqual(len(self.group.jobs), 2)

    def test_immediate_result_returned_without_waiting(self):
        class Immediate(Group):
            def submit(self, *args):
                super().submit(*args)
                self.events.append({'kind': 'result', 'ticket': args[0], 'results': {1: 7, 2: 8}})
                return True
        calc = ExcelCalculation(None, 100, 80, threading.Event(), group_factory=Immediate)
        calc.watch(row('1'), **self.kw)
        calc.group.events.append({'kind': 'ready'})
        calc.watch(row('1'), **self.kw)
        self.assertEqual(calc.calculate(row('2'), self.kw['powers'], (1, 2), (1, 100), None),
                         {1: 7, 2: 8})

    def test_transport_failure_latches_and_other_failure_raises(self):
        error = WorkbookTransportError('EOF', operation='step', elapsed=1, deadline=8,
                                       phase='read', process_state='exited')
        self.group.events.append({'kind': 'error', 'error': error})
        self.watch(row('2'))
        self.assertIn('통신 오류', self.calc.latched_reason)
        self.assertIsNone(self.calculate(row('3')))

    def test_timeout_retains_transport_diagnostics(self):
        error = WorkbookTimeout('x', operation='reset', elapsed=30.1, deadline=30,
                                phase='reset:open_workbook', process_state='running')
        self.group.events.append({'kind': 'timeout', 'error': error})
        self.watch(row('2'))
        for field in ('operation=reset', 'elapsed=30.1', 'deadline=30',
                      'phase=reset:open_workbook', 'process_state=running'):
            self.assertIn(field, self.calc.diagnostic_reason)
        self.assertEqual(self.calc.waiting_status(),
                         'Excel 종료 확인 대기 · 자동 적용 일시정지')

    def test_recovery_requires_retirement_and_reuses_prepared_bytes(self):
        now = [0]
        class Recoverable(Group):
            created = []
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.prepared_workbook = kwargs.get('prepared_workbook', object())
                self.verified = False
                self.created.append(self)
            def retirement_status(self):
                return {'verified': self.verified}
        calc = ExcelCalculation(None, 100, 80, threading.Event(), group_factory=Recoverable,
                                clock=lambda: now[0])
        calc.watch(row('1'), **self.kw)
        old = calc.group
        old.events.append({'kind': 'ready'})
        calc.watch(row('1'), **self.kw)
        error = WorkbookTimeout('x', operation='step', elapsed=8, deadline=8,
                                phase='step', process_state='running')
        old.events.append({'kind': 'timeout', 'error': error})
        calc.watch(row('2'), **self.kw)
        now[0] = 100
        calc.watch(row('3'), **self.kw)
        self.assertIs(calc.group, old)
        old.verified = True
        calc.watch(row('3'), **self.kw)
        now[0] += 1
        calc.watch(row('3'), **self.kw)
        self.assertIs(calc.group, old)
        now[0] += 1
        calc.watch(row('3'), **self.kw)
        self.assertIsNot(calc.group, old)
        self.assertIs(calc.group.prepared_workbook, old.prepared_workbook)
        calc.group.events.append({'kind': 'ready'})
        calc.watch(row('3'), **self.kw)
        self.assertIsNone(calc.calculate(row('3'), self.kw['powers'], (1, 2), (1, 100), None))
        self.assertEqual(calc.group.jobs, [])
        calc.calculate(row('4'), self.kw['powers'], (1, 2), (1, 100), None)
        self.assertEqual(len(calc.group.jobs), 1)

    def test_repeated_failures_back_off_until_accepted_batch(self):
        now = [0]
        class Recoverable(Group):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.prepared_workbook = kwargs.get('prepared_workbook', object())
            def retirement_status(self):
                return {'verified': True}
        calc = ExcelCalculation(None, 100, 80, threading.Event(), group_factory=Recoverable,
                                clock=lambda: now[0])
        calc.watch(row('1'), **self.kw)
        for fault_number, delay in enumerate((2, 5, 15, 30, 60, 60), 1):
            calc.group.events.append({'kind': 'ready'})
            calc.watch(row('1'), **self.kw)
            error = WorkbookTimeout('x', operation='step', elapsed=8, deadline=8,
                                    phase='step', process_state='running')
            calc.group.events.append({'kind': 'timeout', 'error': error})
            calc.watch(row('1'), **self.kw)
            self.assertEqual(calc.fault_generation, fault_number)
            calc.watch(row('1'), **self.kw)
            self.assertEqual(calc._retry_at, now[0] + delay)
            now[0] += delay
            calc.watch(row('1'), **self.kw)
        calc.group.events.append({'kind': 'ready'})
        calc.watch(row('1'), **self.kw)
        calc.calculate(row('2'), self.kw['powers'], (1, 2), (1, 100), None)
        ticket = calc.group.jobs[-1][0]
        calc.group.events.append({'kind': 'result', 'ticket': ticket, 'results': {1: 5, 2: 6}})
        calc.watch(row('2', new=False), **self.kw)
        self.assertEqual(calc.calculate(row('2', new=False), self.kw['powers'], (1, 2), (1, 100), None), {1: 5, 2: 6})
        self.assertEqual(calc._failures, 0)
        self.assertEqual(calc.fault_generation, 6)

    def test_topology_change_during_backoff_never_starts_old_group(self):
        now = [0]
        class Recoverable(Group):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.prepared_workbook = kwargs.get('prepared_workbook', object())
            def retirement_status(self):
                return {'verified': True}
        calc = ExcelCalculation(None, 100, 80, threading.Event(), group_factory=Recoverable,
                                clock=lambda: now[0])
        calc.watch(row('1'), **self.kw)
        old = calc.group
        old.events.append({'kind': 'timeout', 'error': WorkbookTimeout('x', operation='step',
                           elapsed=8, deadline=8, phase='step', process_state='running')})
        calc.watch(row('1'), **self.kw)
        calc.watch(row('1'), **self.kw)
        now[0] = 3
        calc.watch(row('2'), **(self.kw | {'heaters': (1,), 'powers': {1: 20}}))
        self.assertIs(calc.group, old)
        self.assertIn('히터 구성 변경', calc.waiting_status())
        calc.watch(row('2'), **self.kw)
        self.assertIsNot(calc.group, old)
        self.assertEqual(calc.fault_generation, 1)
        self.assertEqual(calc.describe()['failure_streak'], 1)

    def test_other_error_propagates(self):
        self.group.events.append({'kind': 'error', 'error': ValueError('bad formula')})
        with self.assertRaisesRegex(ValueError, 'bad formula'):
            self.watch(row('2'))

    def test_topology_change_latches(self):
        self.watch(row('2'), heaters=(1,), powers={1: 20})
        self.assertTrue(self.group.closed)
        self.assertIn('히터 구성 변경', self.calc.latched_reason)

    def test_baseline_same_layer_correction_is_not_fresh(self):
        corrected = row('1', observation_key='corrected')
        self.assertIsNone(self.calculate(corrected))
        self.assertEqual(self.group.jobs, [])

    def test_layer_seen_before_ready_is_baseline(self):
        calc = ExcelCalculation(None, 100, 80, threading.Event(), group_factory=Group)
        calc.watch(row('1'), **self.kw)
        calc.watch(row('2'), **self.kw)
        calc.group.events.append({'kind': 'ready', 'ticket': None})
        calc.watch(row('2'), **self.kw)
        self.assertIsNone(calc.calculate(row('2'), self.kw['powers'], (1, 2), (1, 100), None))
        self.assertEqual(calc.group.jobs, [])
        calc.calculate(row('3'), self.kw['powers'], (1, 2), (1, 100), None)
        self.assertEqual(len(calc.group.jobs), 1)

    def test_layer_first_seen_with_ready_is_fresh(self):
        calc = ExcelCalculation(None, 100, 80, threading.Event(), group_factory=Group)
        calc.watch(row('1'), **self.kw)
        calc.group.events.append({'kind': 'ready', 'ticket': None})
        calc.watch(row('2'), **self.kw)
        calc.calculate(row('2'), self.kw['powers'], (1, 2), (1, 100), None)
        self.assertEqual(len(calc.group.jobs), 1)

    def test_stale_ready_does_not_finish_reset(self):
        self.calculate(row('2'))
        self.watch(row('3'))
        self.group.events.append({'kind': 'ready', 'ticket': None})
        self.watch(row('3'))
        self.assertTrue(self.calc._resetting)

    def test_real_owner_passes_one_based_layer_to_controller(self):
        from excel_async import AsyncWorkbookGroup, ExcelGroupFence
        calls = []
        class Controller:
            def __init__(self, spec, target, **kwargs):
                pass
            def set_target_temperature(self, value):
                pass
            def process(self, layer, temperature, power, *, slice_features=None):
                calls.append((layer, slice_features))
                return power
            def close(self, force=False):
                pass
        def group_factory(*args, **kwargs):
            return AsyncWorkbookGroup(*args, **kwargs, fence=ExcelGroupFence())
        calc = ExcelCalculation(None, 100, 80, threading.Event(), controller_factory=Controller,
                                group_factory=group_factory)
        self.addCleanup(calc.close)
        calc.watch(row('1'), **self.kw)
        deadline = time.monotonic() + 2
        while calc.group.state != 'ready' and time.monotonic() < deadline:
            time.sleep(.005)
        calc.watch(row('1'), **self.kw)
        calc.calculate(row('2'), self.kw['powers'], self.kw['heaters'], (1, 100), None, {'a': 1})
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(calls, [(3, {'a': 1}), (3, {'a': 1})])


if __name__ == '__main__':
    unittest.main()
