"""Real log/CSV/session integration with concurrent append injection."""
import csv
from contextlib import contextmanager
import csv_source
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


class LogSessionContinuityTests(unittest.TestCase):
    def test_transient_log_append_preserves_history_at_idle_and_preflight(self):
        self.run_scenario('transient')

    def test_exhausted_preflight_does_not_save_or_replay_and_preserves_previous_save(self):
        self.run_scenario('exhausted')

    def test_stable_new_job_rejects_old_decision_and_preserves_previous_save(self):
        self.run_scenario('new-job')

    def run_scenario(self, scenario):
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
            original_shared = csv_source.shared_binary
            injection = {'idle': False, 'preflight': 0, 'in_preflight': False, 'done': [], 'job_b': False}
            original_verify = raw._verify_csv_observation
            def verify(key):
                injection['in_preflight'] = True
                if injection['job_b']:
                    injection['job_b'] = False
                    stamp = datetime.fromtimestamp(fake_now[0]).strftime('%Y-%m-%d%p%I%M%S')
                    with log.open('a', encoding='utf-8') as stream:
                        stream.write(f'{stamp}...[BJT - AUTO]: AutoPrint started!\n')
                        stream.write(f'{stamp}...[BJT - AUTO]: Layer: 7 -> Current Z = 1.1\n')
                    injection['done'].append('job-b')
                try:
                    return original_verify(key)
                finally:
                    injection['in_preflight'] = False
            raw._verify_csv_observation = verify
            @contextmanager
            def shared(path):
                with original_shared(path) as stream:
                    class Proxy:
                        fired = False
                        def __getattr__(self, name): return getattr(stream, name)
                        def read(self, *args):
                            data = stream.read(*args)
                            if path == log and not self.fired:
                                stage = 'preflight' if injection['in_preflight'] else 'idle'
                                if injection[stage]:
                                    self.fired = True
                                    if stage == 'idle':
                                        injection[stage] = False
                                    else:
                                        injection[stage] -= 1
                                    with log.open('ab') as out:
                                        out.write(b'concurrent harmless append\n')
                                    injection['done'].append(stage)
                            return data
                    yield Proxy()
            with patch('csv_source.shared_binary', shared), patch('windows_ui_adapter.time', fake_time), patch('session.WorkbookController', Controller):
                thread = threading.Thread(target=work, daemon=True)
                thread.start()
                try:
                    wait(lambda: bool(snapshots()))
                    accepted = 0
                    for layer in range(1, 12):
                        if layer == 5 and scenario == 'transient':
                            injection['preflight'] = 1
                        if layer == 6 and scenario == 'exhausted':
                            injection['preflight'] = 3
                        if layer == 6 and scenario == 'new-job':
                            injection['job_b'] = True
                        add_row(layer)
                        wait(lambda: any(x['snapshot'].layer_index == layer for x in snapshots()))
                        if layer == 6 and scenario != 'transient':
                            deadline = time.monotonic() + 6
                            with condition:
                                while time.monotonic() < deadline and not (errors or
                                        (scenario == 'exhausted' and injection['done'].count('preflight') == 3
                                         and any('waiting' in x['effective_mode'] for x in snapshots())) or
                                        (scenario == 'new-job' and 'job-b' in injection['done'])):
                                    condition.wait(.05)
                            self.assertEqual(1, state['saves'])
                            self.assertIn('preflight' if scenario == 'exhausted' else 'job-b', injection['done'])
                            if scenario == 'exhausted':
                                self.assertEqual(3, injection['done'].count('preflight'))
                                self.assertTrue(any('waiting' in x['effective_mode'] for x in snapshots()))
                                count = len(snapshots())
                                wait(lambda: len(snapshots()) >= count + 3)
                                self.assertEqual(1, state['saves'])
                            else:
                                count = len(snapshots())
                                deadline = time.monotonic() + 3
                                with condition:
                                    while not errors and len(snapshots()) < count + 2 and time.monotonic() < deadline:
                                        condition.wait(.05)
                                self.assertTrue(errors or len(snapshots()) >= count + 2)
                                self.assertEqual(1, state['saves'])
                            from layer_charts import LayerHistory
                            history = LayerHistory()
                            for payload in snapshots():
                                history.record(payload)
                            self.assertTrue(any(p.ir1_saved is not None for p in history.points))
                            return
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
                            if accepted == 1 and scenario == 'transient':
                                previous_run = rows[-1]['snapshot'].run_key
                                previous_offset = reader.offset
                                snapshot_count = len(snapshots())
                                injection['idle'] = True
                                wait(lambda: 'idle' in injection['done'])
                                wait(lambda: len(snapshots()) > snapshot_count)
                                latest = snapshots()[-1]['snapshot']
                                self.assertEqual(previous_run, latest.run_key)
                                self.assertEqual(key, latest.observation_key)
                                self.assertEqual(previous_offset, reader.offset)
                            fake_now[0] += 7
                            wait(lambda: any(x['snapshot'].expired_observation_key == key for x in snapshots()))
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
                    self.assertEqual(injection['done'], ['idle', 'preflight'])
                    self.assertEqual(errors, [])
                finally:
                    stop.set()
                    thread.join(5)
                    self.assertFalse(thread.is_alive())


if __name__ == '__main__': unittest.main()
