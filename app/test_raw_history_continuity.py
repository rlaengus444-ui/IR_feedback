"""Integration of CSV age-out, UI wrapper, session and the real Excel owner."""
import csv
from datetime import datetime, timedelta
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from excel_async import AsyncWorkbookGroup, ExcelGroupFence
from resilient_adapter import ResilientUiAdapter
from session import SessionOptions, run_ui_session
from test_excel_session_async import SPEC
import test_windows_ui_adapter as fixture_module


class RawHistoryContinuityTests(unittest.TestCase):
    def test_real_reader_expiry_preserves_workbook_history(self):
        with tempfile.TemporaryDirectory() as directory:
            raw, state, calls, path, values, append, baseline, source, audit = fixture_module.AdapterTests.fixture(self, directory)
            reader = raw._csv
            target = raw._target
            raw.disconnect()
            raw._source_factory = lambda target: reader
            fake_now = [time.time()]
            reader.clock = lambda: fake_now[0]
            io_lock = threading.RLock()
            original_read = reader.read
            reads = []
            def locked_read():
                with io_lock:
                    result = original_read()
                    if result.sample:
                        reads.append((result.sample.layer_index, result.is_new, bool(result.token), fake_now[0]-result.sample.finished.timestamp()))
                    return result
            reader.read = locked_read
            fake_time = SimpleNamespace(time=lambda: fake_now[0], monotonic=time.monotonic)
            wrapper = ResilientUiAdapter(raw_factory=lambda source_factory: raw,
                source_factory=lambda target: reader)
            condition = threading.Condition()
            events, histories, resets, errors = [], [], [], []
            initializations = []
            stop = threading.Event()
            fence = ExcelGroupFence()
            class Controller:
                def __init__(self, spec, target, **kwargs):
                    self.history = []
                    self.closed = False
                    initializations.append(1)
                def set_target_temperature(self, target): pass
                def reset(self):
                    self.history.clear()
                    resets.append(1)
                def process(self, layer, temperature, power, *, slice_features=None):
                    self.history.append(layer)
                    histories.append(tuple(self.history))
                    return SimpleNamespace(power_percent=power + (1 if len(self.history) >= 3 else 0), reason='history')
                def close(self, force=False): self.closed = True
                def retirement_status(self): return {'verified': self.closed}
            def emit(kind, payload):
                with condition:
                    events.append((kind, payload))
                    condition.notify_all()
            def wait(predicate, timeout=6):
                deadline = time.monotonic() + timeout
                with condition:
                    while not predicate():
                        if errors: raise AssertionError(errors)
                        remaining = deadline - time.monotonic()
                        if remaining <= 0: raise AssertionError((histories, resets, events[-8:]))
                        condition.wait(min(.05, remaining))
            def work():
                try:
                    run_ui_session(SessionOptions(target, mode='control', workbook_spec=SPEC), stop,
                        emit, lambda: wrapper,
                        workbook_group_factory=lambda *a, **kw: AsyncWorkbookGroup(*a, fence=fence, **kw))
                except Exception as exc:
                    errors.append(exc)
                    with condition: condition.notify_all()
            with source.open(newline='', encoding='utf-8') as stream:
                csv_rows = csv.reader(stream)
                next(csv_rows)
                first_row = next(csv_rows)
            start_text = first_row[1]
            log = source.parents[2] / 'Logs' / '20261027_M1.log'
            def add_row(index):
                finish = datetime.fromtimestamp(fake_now[0]).replace(microsecond=0)
                row = [str(index + 1), start_text,
                    (finish - timedelta(seconds=24)).strftime('%m/%d/%Y %H:%M:%S'),
                    finish.strftime('%m/%d/%Y %H:%M:%S'), '43']
                with io_lock:
                    with source.open('ab') as stream: stream.write(fixture_module.csv_bytes([row]))
                    with log.open('a', encoding='utf-8') as stream:
                        stream.write(f"{finish.strftime('%Y-%m-%d%p%I%M%S')}...[BJT - AUTO]: Layer: {index + 1} -> Current Z = 1.1\n")
            def snapshots(): return [payload for kind, payload in events if kind == 'snapshot']
            with patch('windows_ui_adapter.time', fake_time), patch('session.WorkbookController', Controller):
                thread = threading.Thread(target=work, daemon=True)
                thread.start()
                try:
                    wait(lambda: bool(snapshots()))
                    accepted = 0
                    for layer in range(1, 12):
                        add_row(layer)
                        wait(lambda: any(x['snapshot'].layer_index == layer for x in snapshots()))
                        if layer < 3:
                            wait(lambda: sum(x['snapshot'].layer_index == layer for x in snapshots()) >= 2)
                            continue
                        wait(lambda: any(x['snapshot'].layer_index == layer and x['proposed'] for x in snapshots()))
                        rows = [x for x in snapshots() if x['snapshot'].layer_index == layer and x['proposed']]
                        accepted += 1
                        if accepted >= 3:
                            wait(lambda: state['saves'] >= accepted - 2)
                        if accepted < 4:
                            key = rows[-1]['snapshot'].observation_key
                            count=len(snapshots())
                            fake_now[0] += 7
                            wait(lambda: len(snapshots())>count and snapshots()[-1]['snapshot'].observation_key==key
                                 and snapshots()[-1]['snapshot'].observation_fresh
                                 and snapshots()[-1]['snapshot'].expired_observation_key is None)
                            expected_saves = max(0, accepted - 2)
                            self.assertEqual(state['saves'], expected_saves)
                            self.assertEqual(resets, [])
                        if accepted == 4:
                            break
                    self.assertEqual(accepted, 4)
                    self.assertEqual(len(initializations), 1)
                    self.assertEqual(resets, [])
                    self.assertEqual(len(histories), 4)
                    self.assertEqual(len(histories[-1]), 4)
                    self.assertEqual(histories[-1], tuple(range(histories[-1][0], histories[-1][0] + 4)))
                    self.assertEqual(state['saves'], 2)
                    self.assertEqual(errors, [])
                finally:
                    stop.set()
                    thread.join(5)
                    self.assertFalse(thread.is_alive())


if __name__ == '__main__': unittest.main()
