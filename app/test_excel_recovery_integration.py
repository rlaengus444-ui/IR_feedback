"""Automatic recovery against real session and workbook-owner threads, with fake I/O."""
import tempfile
from pathlib import Path
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from excel_async import AsyncWorkbookGroup
from excel_calculation import ExcelCalculation
from excel_workbook import PreparedWorkbook, WorkbookTimeout
from test_excel_session_async import Harness
from test_excel_workbook import workbook, spec


class RecoveryHarness(Harness):
    def __init__(self, *, verified=True, faults=1, coupled=True):
        super().__init__(coupled=coupled)
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / 'source.xlsx'
        path.write_bytes(workbook())
        self.options = replace(self.options, workbook_spec=spec(path=str(path)))
        self.prepared = PreparedWorkbook.from_file(self.options.workbook_spec)
        self.now = 0.0
        self.verified = verified
        self.faults = faults
        self.instances = []
        self.groups = []
        self.calc = None
        h = self

        class Controller:
            def __init__(self, workbook_spec, target, **kwargs):
                self.closed = False
                self.prepared = kwargs.get('prepared_workbook')
                with h.condition:
                    if any(not c.closed for c in h.instances) and not coupled:
                        # Multiple controllers in one group are expected for two heaters.
                        pass
                    h.instances.append(self)
                    h.condition.notify_all()
            def set_target_temperature(self, target):
                pass
            def reset(self):
                pass
            def process(self, layer, temperature, power, *, slice_features=None):
                with h.condition:
                    h.calls.append((layer, temperature, power, slice_features))
                    if h.faults:
                        h.faults -= 1
                        raise WorkbookTimeout('controlled timeout', operation='process', elapsed=1,
                                              deadline=1, phase='calculate', process_state='unknown')
                return SimpleNamespace(power_percent=power + 1, reason='fake')
            def close(self, force=False):
                self.closed = True
            def retirement_status(self):
                return {'verified': self.closed and h.verified}
        self.factory = Controller

    def group_factory(self, *args, **kwargs):
        kwargs['prepare_factory'] = lambda spec, stop_event: self.prepared
        group = AsyncWorkbookGroup(*args, fence=self.fence, **kwargs)
        self.group = group
        self.groups.append(group)
        return group

    def start(self, wait_ready=True):
        def calculation(*args, **kwargs):
            self.calc = ExcelCalculation(*args, **kwargs, clock=lambda: self.now, retry_delays=(2, 5, 5))
            return self.calc
        self.calc_patch = patch('session.ExcelCalculation', side_effect=calculation)
        self.calc_patch.start()
        return super().start(wait_ready=wait_ready)

    def close(self):
        super().close()
        self.calc_patch.stop()
        self.temp.cleanup()

    def set_row(self, key, layer):
        with self.condition:
            self.frame = self.row(key, layer, self.frame.coupled_output)
            self.condition.notify_all()

    def trigger_fault(self, key='fault', layer=1):
        self.set_row(key, layer)
        self.wait(lambda: self.calc._recovery == 'retiring')

    def advance(self, value):
        self.now = value


class RecoveryIntegrationTests(unittest.TestCase):
    def harness(self, **kwargs):
        h = RecoveryHarness(**kwargs)
        self.addCleanup(h.close)
        return h.start()

    def poll_three(self, h):
        with h.condition:
            reads = h.reads
            snapshots = sum(k == 'snapshot' for k, _ in h.events)
        h.wait(lambda: h.reads >= reads + 3 and
               sum(k == 'snapshot' for k, _ in h.events) >= snapshots + 3)

    def test_recovers_same_session_using_same_prepared_workbook_and_fresh_row(self):
        h = self.harness()
        h.trigger_fault()
        h.wait(lambda: h.calc._recovery == 'backoff')
        self.assertEqual(1, len(h.instances))
        self.assertTrue(h.instances[0].closed)
        Path(h.options.workbook_spec.path).unlink()
        h.set_row('during-recovery', 2)
        self.poll_three(h)
        h.adapter.apply_ir_power.assert_not_called()
        h.advance(2)
        h.wait(lambda: len(h.instances) == 2 and h.calc._recovery is None)
        self.assertIs(h.instances[0].prepared, h.prepared)
        self.assertIs(h.instances[1].prepared, h.prepared)
        self.poll_three(h)
        h.adapter.apply_ir_power.assert_not_called()
        h.set_row('new-layer', 3)
        h.wait(lambda: h.adapter.apply_ir_power.call_count == 1)
        self.assertEqual('new-layer', h.adapter.apply_ir_power.call_args.args[2])
        h.adapter.connect.assert_called_once()
        self.assertTrue(h.thread.is_alive())
        self.assertEqual([2, 4], [x[0] for x in h.calls])

    def test_repeated_fault_backoff_is_not_reset_by_changes(self):
        h = self.harness(faults=2)
        h.trigger_fault()
        h.wait(lambda: h.calc._recovery == 'backoff')
        self.assertEqual(2, h.calc._retry_at)
        h.options._target_temperature_setting.set(70)
        h.set_row('during-first-backoff', 2)
        self.poll_three(h)
        self.assertEqual(2, h.calc._retry_at)
        h.advance(2)
        h.wait(lambda: len(h.instances) == 2 and h.calc._recovery is None)
        self.poll_three(h)
        h.set_row('second-fault', 3)
        h.wait(lambda: h.calc._recovery == 'backoff' and h.calc.fault_generation == 2)
        self.assertEqual(7, h.calc._retry_at)
        h.options._target_temperature_setting.set(65)
        self.poll_three(h)
        self.assertEqual(7, h.calc._retry_at)
        h.advance(6.9)
        self.poll_three(h)
        self.assertEqual(2, len(h.instances))
        h.advance(7)
        h.wait(lambda: len(h.instances) == 3 and h.calc._recovery is None)
        self.poll_three(h)
        h.adapter.apply_ir_power.assert_not_called()
        h.set_row('fresh-after-retries', 4)
        h.wait(lambda: h.adapter.apply_ir_power.call_count == 1)
        self.assertEqual('fresh-after-retries', h.adapter.apply_ir_power.call_args.args[2])
        self.assertTrue(all(c.closed for c in h.instances[:-1]))

    def test_unknown_retirement_keeps_polling_and_prevents_restart(self):
        h = self.harness(verified=False)
        h.trigger_fault()
        self.poll_three(h)
        h.advance(1000)
        self.poll_three(h)
        self.assertEqual('retiring', h.calc._recovery)
        self.assertEqual(1, len(h.instances))
        h.adapter.apply_ir_power.assert_not_called()
        h.verified = True
        with h.groups[0]._condition:
            h.groups[0]._condition.notify_all()

    def test_disconnect_in_backoff_prevents_restart(self):
        h = self.harness()
        h.trigger_fault()
        h.wait(lambda: h.calc._recovery == 'backoff')
        h.stop.set()
        h.thread.join(3)
        h.advance(1000)
        self.assertFalse(h.thread.is_alive())
        self.assertEqual(1, len(h.instances))
        h.adapter.apply_ir_power.assert_not_called()

    def test_two_heaters_recovered_before_save(self):
        h = self.harness(coupled=False)
        h.trigger_fault()
        h.wait(lambda: h.calc._recovery == 'backoff')
        self.assertTrue(all(c.closed for c in h.instances))
        h.advance(2)
        h.wait(lambda: len(h.instances) == 4 and h.calc._recovery is None)
        self.poll_three(h)
        h.adapter.apply_ir_power.assert_not_called()
        h.set_row('fresh', 2)
        h.wait(lambda: h.adapter.apply_ir_power.call_count == 2)
        self.assertEqual([1, 2], [c.args[0] for c in h.adapter.apply_ir_power.call_args_list])
        self.assertTrue(all(c.prepared is h.prepared for c in h.instances))


if __name__ == '__main__':
    unittest.main()
