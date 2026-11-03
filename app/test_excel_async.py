import threading
import time
import unittest

from excel_async import AsyncWorkbookGroup as RealGroup, ExcelGroupFence
from excel_workbook import WorkbookTransportError


class AsyncWorkbookGroup(RealGroup):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('fence', ExcelGroupFence())
        super().__init__(*args, **kwargs)


class FakeController:
    instances = []
    gate = None
    entered = None
    block = None

    def __init__(self, spec, target, *, stop_event, maximum_ir_percent, on_created=None):
        self.calls = []
        self.closed = False
        self.owner = threading.get_ident()
        self.instances.append(self)
        if on_created:
            on_created(self)
        self._wait('init')

    def _wait(self, name):
        self.calls.append((name, threading.get_ident()))
        if self.block == name:
            self.entered.set()
            self.gate.wait(3)

    def set_target_temperature(self, value):
        self.calls.append(('target', threading.get_ident()))

    def reset(self):
        self._wait('reset')

    def process(self, layer, temperature, power, *, slice_features=None):
        self._wait('process')
        return (layer, power)

    def close(self, force=False):
        self.closed = True


def wait_for(predicate, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.005)
    raise AssertionError('timed out waiting for test condition')


class AsyncTests(unittest.TestCase):
    def setUp(self):
        FakeController.instances = []
        FakeController.gate = threading.Event()
        FakeController.entered = threading.Event()
        FakeController.block = None
        self.group = AsyncWorkbookGroup(None, 100, 100, threading.Event(), FakeController)
        self.addCleanup(self.group.close)
        self.addCleanup(FakeController.gate.set)

    def event(self):
        return wait_for(self.group.poll)

    def start(self):
        self.assertTrue(self.group.start(['A', 'B']))
        self.assertEqual(self.event()['kind'], 'ready')

    def test_both_heaters_owned_and_result_atomic(self):
        self.start()
        self.assertTrue(self.group.submit('t', 1, 80, {'A': 10, 'B': 20}, 100))
        result = self.event()
        self.assertEqual(result['results'], {'A': (1, 10), 'B': (1, 20)})
        self.assertEqual(result['ticket'], 't')
        self.assertGreaterEqual(result['elapsed_seconds'], 0)
        owners = {ident for c in FakeController.instances for _, ident in c.calls}
        self.assertEqual(len(owners), 1)
        self.assertNotIn(threading.get_ident(), owners)

    def test_prepares_once_and_shares_frozen_workbook(self):
        frozen = object()
        prepares, received = [], []
        def prepare(spec, *, stop_event):
            prepares.append(spec)
            return frozen
        class PreparedController(FakeController):
            def __init__(self, spec, target, *, prepared_workbook, **kwargs):
                received.append(prepared_workbook)
                super().__init__(spec, target, **kwargs)
        group = AsyncWorkbookGroup('spec', 100, 100, threading.Event(), PreparedController,
                                   prepare_factory=prepare)
        self.addCleanup(group.close)
        group.start(['A', 'B'])
        wait_for(group.poll)
        self.assertEqual(prepares, ['spec'])
        self.assertEqual(received, [frozen, frozen])
        self.assertIs(group.prepared_workbook, frozen)

    def test_retirement_requires_verified_cleanup(self):
        class Retiring(FakeController):
            def __init__(self, *args, **kwargs):
                self.verified = False
                super().__init__(*args, **kwargs)
            def retirement_status(self):
                return {'verified': self.verified}
        group = AsyncWorkbookGroup(None, 100, 100, threading.Event(), Retiring)
        group.start(['A'])
        wait_for(group.poll)
        group.close()
        wait_for(lambda: group._owner_done)
        self.assertFalse(group.retirement_status()['verified'])
        controller = group._owned[0]
        controller.verified = True
        with group._condition:
            group._condition.notify_all()
        wait_for(lambda: group.retirement_status()['verified'])

    def test_fence_blocks_new_group_until_previous_verified(self):
        class Retiring(FakeController):
            def __init__(self, *args, **kwargs):
                self.verified = False
                super().__init__(*args, **kwargs)
            def retirement_status(self):
                return {'verified': self.verified}
        fence = ExcelGroupFence()
        first = RealGroup(None, 100, 100, threading.Event(), Retiring, fence=fence)
        second = RealGroup(None, 100, 100, threading.Event(), Retiring, fence=fence)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.start(['A'])
        wait_for(first.poll)
        first.close()
        second.start(['A'])
        self.assertEqual(second.state, 'waiting-retirement')
        self.assertIsNone(second._thread)
        self.assertEqual(len(FakeController.instances), 1)
        FakeController.instances[0].verified = True
        with first._condition:
            first._condition.notify_all()
        wait_for(lambda: first.retirement_status()['verified'])
        wait_for(second.poll)
        self.assertEqual(second.state, 'ready')
        self.assertEqual(len(FakeController.instances), 2)

    def test_cancel_after_claim_before_thread_start_releases_fence(self):
        class PausedFence(ExcelGroupFence):
            def __init__(self):
                super().__init__()
                self.claimed = threading.Event()
                self.release = threading.Event()
                self.first = True
            def try_claim(self, group):
                result = super().try_claim(group)
                if result and self.first:
                    self.first = False
                    self.claimed.set()
                    self.release.wait(2)
                return result
        fence = PausedFence()
        stop = threading.Event()
        first = RealGroup(None, 100, 100, stop, FakeController, fence=fence)
        self.addCleanup(first.close)
        starter = threading.Thread(target=first.start, args=(['A'],))
        starter.start()
        wait_for(fence.claimed.is_set)
        stop.set()
        first.poll()
        fence.release.set()
        starter.join(2)
        self.assertEqual(first.state, 'closed')
        self.assertIsNone(first._thread)
        wait_for(lambda: first.retirement_status()['verified'])
        second = RealGroup(None, 100, 100, threading.Event(), FakeController, fence=fence)
        self.addCleanup(second.close)
        second.start(['A'])
        wait_for(second.poll)
        self.assertEqual(second.state, 'ready')

    def test_cancel_waiting_group_without_starting_thread(self):
        fence = ExcelGroupFence()
        first = RealGroup(None, 100, 100, threading.Event(), FakeController, fence=fence)
        first.start(['A'])
        wait_for(first.poll)
        self.addCleanup(first.close)
        stop = threading.Event()
        waiting = RealGroup(None, 100, 100, stop, FakeController, fence=fence)
        waiting.start(['A'])
        self.assertEqual(waiting.state, 'waiting-retirement')
        stop.set()
        waiting.poll()
        self.assertEqual(waiting.state, 'closed')
        self.assertIsNone(waiting._thread)
        wait_for(lambda: waiting.retirement_status()['verified'])

    def test_reset_supersedes_busy_and_discards_old_result(self):
        self.start()
        FakeController.block = 'process'
        self.assertTrue(self.group.submit('old', 1, 80, {'A': 10, 'B': 20}, 100))
        wait_for(FakeController.entered.is_set)
        for i in range(100):
            self.assertTrue(self.group.reset(i, 100 + i))
        self.assertFalse(self.group.submit('bad', 2, 80, {'A': 10, 'B': 20}, 100))
        FakeController.block = None
        FakeController.gate.set()
        event = self.event()
        self.assertEqual((event['kind'], event['ticket']), ('ready', 99))
        self.assertIsNone(self.group.poll())
        self.assertEqual(sum(name == 'reset' for c in FakeController.instances for name, _ in c.calls), 2)

    def test_watchdog_latches_and_ignores_late_results(self):
        self.start()
        FakeController.block = 'process'
        self.group.submit('old', 1, 80, {'A': 10, 'B': 20}, 100)
        wait_for(FakeController.entered.is_set)
        with self.group._condition:
            self.group._deadline = time.monotonic() - 1
        self.assertEqual(self.event()['kind'], 'timeout')
        self.assertFalse(self.group.reset())
        self.assertFalse(self.group.submit('new', 2, 80, {'A': 10, 'B': 20}, 100))
        FakeController.gate.set()
        time.sleep(.03)
        self.assertIsNone(self.group.poll())
        self.assertEqual(self.group.state, 'timeout')

    def test_watchdog_reports_controller_operation_and_phase(self):
        self.start()
        FakeController.block = 'process'
        self.group.submit('old', 1, 80, {'A': 10, 'B': 20}, 100)
        wait_for(FakeController.entered.is_set)
        controller = FakeController.instances[0]
        controller._lifecycle_lock = threading.Lock()
        controller._last_operation = 'step'
        controller._phase = 'step:calculate'
        controller._proc = type('Process', (), {'poll': lambda self: None})()
        with self.group._condition:
            self.group._deadline = time.monotonic() - 1
        error = self.event()['error']
        self.assertEqual((error.operation, error.phase, error.process_state),
                         ('step', 'step:calculate', 'running'))

    def test_close_is_nonblocking_during_initialization(self):
        FakeController.block = 'init'
        self.group.start(['A', 'B'])
        wait_for(FakeController.entered.is_set)
        start = time.monotonic()
        self.group.close()
        self.assertLess(time.monotonic() - start, .2)
        wait_for(lambda: FakeController.instances[0].closed)
        FakeController.gate.set()
        time.sleep(.03)
        self.assertIsNone(self.group.poll())
        self.assertEqual(len(FakeController.instances), 1)

    def test_reset_can_stall_without_blocking_poll_or_close(self):
        self.start()
        FakeController.block = 'reset'
        self.group.reset('reset')
        wait_for(FakeController.entered.is_set)
        start = time.monotonic()
        self.assertIsNone(self.group.poll())
        self.group.close()
        self.assertLess(time.monotonic() - start, .2)
        FakeController.gate.set()
        time.sleep(.03)
        self.assertIsNone(self.group.poll())

    def test_completion_after_deadline_is_rejected_without_poll(self):
        self.start()
        FakeController.block = 'process'
        self.group.submit('late', 1, 80, {'A': 10, 'B': 20}, 100)
        wait_for(FakeController.entered.is_set)
        with self.group._condition:
            self.group._deadline = time.monotonic() - 1
        FakeController.gate.set()
        wait_for(lambda: self.group.state == 'timeout')
        event = self.event()
        self.assertEqual((event['kind'], event['ticket']), ('timeout', 'late'))
        self.assertFalse(self.group.reset())

    def test_transport_error_is_not_a_timeout(self):
        class Broken(FakeController):
            def process(self, *args, **kwargs):
                raise WorkbookTransportError('EOF', operation='step', elapsed=0,
                                             deadline=8, phase='read', process_state='exited')
        self.group = AsyncWorkbookGroup(None, 100, 100, threading.Event(), Broken)
        self.addCleanup(self.group.close)
        self.start()
        self.group.submit('eof', 1, 80, {'A': 10, 'B': 20}, 100)
        event = self.event()
        self.assertEqual(event['kind'], 'error')
        self.assertIsInstance(event['error'], WorkbookTransportError)

    def test_unexpected_topology_rejected_before_any_calculation(self):
        self.start()
        self.assertFalse(self.group.submit('bad', 1, 80, {'A': 10}, 100))
        self.assertFalse(any(name == 'process' for c in FakeController.instances for name, _ in c.calls))


if __name__ == '__main__':
    unittest.main()
