from contextlib import contextmanager
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

import csv_source
from adapter_contracts import MachineWindow
from csv_source import JobLog, JobLogReadUnavailable, CsvSourceError, MonitoringCsvReader

START = b'2026-10-27 AM060139...[BJT - AUTO]: AutoPrint started!\n'
LAYER = b'2026-10-27 AM060143...[BJT - AUTO]: Layer: 1058 -> Current Z = 1.23\n'
NEXT = b'2026-10-27 AM060150...[BJT - AUTO]: Layer: 1059 -> Current Z = 1.23\n'

class JobLogTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / 'Logs').mkdir()
        self.path = self.base / 'Logs/job_M1.log'
        self.path.write_bytes(START + LAYER)
        self.reader = JobLog(MachineWindow('M1', 1, 2, 'test', str(self.base / 'operator.exe')))
        self.initial = self.reader.read()

    def state(self):
        return tuple(getattr(self.reader, k) for k in ('context','path','identity','offset','pending','last_stat','window_start','window_hash'))

    def hook(self, action):
        original = csv_source.shared_binary
        @contextmanager
        def wrapped(path):
            with original(path) as stream:
                class Proxy:
                    def fileno(self): return stream.fileno()
                    def seek(self, *args): return stream.seek(*args)
                    def read(self, *args):
                        data = stream.read(*args)
                        action()
                        return data
                yield Proxy()
        return patch.object(csv_source, 'shared_binary', wrapped)

    def test_one_race_retries_and_keeps_same_run(self):
        calls = []
        def append_once():
            if not calls:
                calls.append(1)
                with self.path.open('ab') as f: f.write(NEXT)
        with self.hook(append_once):
            current = self.reader.read()
        self.assertEqual(self.initial.run_key, current.run_key)
        self.assertEqual(1059, current.current_layer)

    def test_retry_exhaustion_preserves_state_and_recovers(self):
        before = self.state()
        def append():
            with self.path.open('ab') as f: f.write(b'x\n')
        with self.hook(append):
            with self.assertRaises(JobLogReadUnavailable) as caught: self.reader.read()
        self.assertEqual('unstable log', caught.exception.reason)
        self.assertEqual(before, self.state())
        self.assertEqual(self.initial.run_key, self.reader.read().run_key)

    def test_incomplete_tail_never_publishes_and_then_completes(self):
        before = self.state()
        with self.path.open('ab') as f: f.write(NEXT[:-1])
        with self.assertRaises(JobLogReadUnavailable) as caught: self.reader.read()
        self.assertEqual('incomplete tail', caught.exception.reason)
        self.assertEqual(before, self.state())
        with self.path.open('ab') as f: f.write(b'\n')
        self.assertEqual(1059, self.reader.read().current_layer)

    def test_backlog_progresses_without_publishing_partial(self):
        self.reader.MAX_APPEND_BYTES = 80
        before = self.state()
        with self.path.open('ab') as f: f.write(b'noise\n'*40 + NEXT)
        for _ in range(20):
            try:
                result = self.reader.read()
                break
            except JobLogReadUnavailable:
                self.assertEqual(before, self.state())
        else: self.fail('backlog did not complete')
        self.assertEqual(1059, result.current_layer)

    def test_rewrite_with_same_mtime_discards_provisional(self):
        before = self.state()
        with self.path.open('ab') as f: f.write(NEXT[:-1])
        with self.assertRaises(JobLogReadUnavailable): self.reader.read()
        stat = self.path.stat()
        self.path.write_bytes((START + LAYER + NEXT).replace(b'1059', b'1060'))
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(1060, self.reader.read().current_layer)
        self.assertNotEqual(before, self.state())

    def test_missing_anchor_and_missing_log_are_unavailable(self):
        before = self.state()
        self.path.write_bytes(b'noise\n')
        with self.assertRaises(JobLogReadUnavailable) as caught: self.reader.read()
        self.assertEqual('missing job anchor', caught.exception.reason)
        self.assertEqual(before, self.state())
        self.path.unlink()
        with self.assertRaises(JobLogReadUnavailable) as caught: self.reader.read()
        self.assertEqual('no matching log', caught.exception.reason)
        self.assertEqual(before, self.state())

    def test_rotation_during_read_retries_and_publishes_new_job(self):
        called = []
        def rotate():
            if not called:
                called.append(True)
                replacement = self.path.with_suffix('.replacement')
                replacement.write_bytes(START.replace(b'060139', b'060140') + LAYER)
                os.replace(replacement, self.path)
        with self.hook(rotate):
            current = self.reader.read()
        self.assertNotEqual(self.initial.run_key, current.run_key)
        self.assertEqual(1058, current.current_layer)

    def test_unavailable_log_does_not_clear_csv_state(self):
        csv_reader = MonitoringCsvReader(self.reader.target, roots=[self.base], log=self.reader)
        csv_reader.run_key = self.initial.run_key
        csv_reader.path = self.base / 'retained.csv'
        csv_reader.offset = 123
        def append():
            with self.path.open('ab') as f: f.write(b'x\n')
        with self.hook(append):
            with self.assertRaises(JobLogReadUnavailable): csv_reader.read()
        self.assertEqual(self.initial.run_key, csv_reader.run_key)
        self.assertEqual(self.base / 'retained.csv', csv_reader.path)
        self.assertEqual(123, csv_reader.offset)

    def test_append_new_job_during_parse_is_retried(self):
        original = csv_source.re.search
        called = []
        def mutate(*args, **kwargs):
            if not called:
                called.append(True)
                with self.path.open('ab') as f:
                    f.write(START.replace(b'060139', b'060140') + LAYER)
            return original(*args, **kwargs)
        with self.path.open('ab') as f: f.write(NEXT)
        with patch.object(csv_source.re, 'search', mutate):
            current = self.reader.read()
        self.assertNotEqual(self.initial.run_key, current.run_key)

    def test_new_latest_file_during_parse_is_selected(self):
        original = csv_source.re.search
        called = []
        other = self.path.parent / 'new_M1.log'
        def mutate(*args, **kwargs):
            if not called:
                called.append(True)
                other.write_bytes(START.replace(b'060139', b'060140') + LAYER)
                stamp = self.path.stat().st_mtime_ns + 1000000000
                os.utime(other, ns=(stamp, stamp))
            return original(*args, **kwargs)
        with self.path.open('ab') as f: f.write(NEXT)
        with patch.object(csv_source.re, 'search', mutate):
            current = self.reader.read()
        self.assertEqual(other, self.reader.path)
        self.assertNotEqual(self.initial.run_key, current.run_key)

    def test_same_mtime_prefix_rewrite_during_parse_is_detected(self):
        original = csv_source.re.search
        called = []
        def mutate(*args, **kwargs):
            if not called:
                called.append(True)
                stat = self.path.stat()
                data = self.path.read_bytes().replace(b'1058', b'1057')
                self.path.write_bytes(data)
                os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            return original(*args, **kwargs)
        with self.path.open('ab') as f: f.write(NEXT)
        with patch.object(csv_source.re, 'search', mutate):
            current = self.reader.read()
        self.assertEqual(1057, current.first_layer)
        self.assertEqual(1059, current.current_layer)

    def test_korean_message_keeps_stable_reason(self):
        error = JobLogReadUnavailable('unstable log')
        self.assertEqual('unstable log', error.reason)
        self.assertIn('로그', str(error))
        self.assertNotIn('unstable log', str(error))

    def test_parser_failure_does_not_commit_cursor(self):
        before = self.state()
        with self.path.open('ab') as f:
            f.write(b'2026-10-27 AM060150...[BJT - AUTO]: Layer: 9999999 -> Current Z = 1\n')
        with self.assertRaises(CsvSourceError): self.reader.read()
        self.assertEqual(before, self.state())

if __name__ == '__main__': unittest.main()
