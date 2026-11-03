import ctypes
import io
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from excel_workbook import PreparedWorkbook, WorkbookController
from test_excel_transport import Process, controller
from test_excel_workbook import spec, workbook


class Function:
    def __init__(self, function):
        self.function = function
    def __call__(self, *args):
        return self.function(*args)


class Kernel:
    def __init__(self, *, opened=123, wait=0, terminate=1):
        self.calls = []
        self.OpenProcess = Function(lambda *args: self.calls.append(('open', args)) or opened)
        self.WaitForSingleObject = Function(lambda *args: self.calls.append(('wait', args)) or wait)
        self.TerminateProcess = Function(lambda *args: self.calls.append(('terminate', args)) or terminate)
        self.CloseHandle = Function(lambda *args: self.calls.append(('close', args)) or 1)


def initialized(proc):
    c = controller(proc)
    c._constructor_done = threading.Event()
    c._constructor_done.set()
    c._reader_thread = None
    c._excel_ownership_known = True
    c._excel_exited = True
    c._cleanup_active = False
    c._retirement_reason = None
    c._retire_lock = threading.Lock()
    return c


class PreparedTests(unittest.TestCase):
    def test_snapshot_survives_source_replacement_and_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.xlsx'
            original = workbook()
            path.write_bytes(original)
            mapping = spec(path=str(path))
            pinned = PreparedWorkbook.from_file(mapping)
            path.write_bytes(b'changed')
            path.unlink()
            received = []
            def spawn(*args, **kwargs):
                class Stream(io.StringIO):
                    def write(self, value):
                        import json
                        command = json.loads(value)
                        if command['op'] == 'init':
                            received.append(Path(command['path']).read_bytes())
                        return super().write(value)
                return Process(stdin=Stream(), stdout=io.StringIO('{"ok":true}\n'))
            with patch('excel_workbook.subprocess.Popen', side_effect=spawn):
                c = WorkbookController(mapping, 65, prepared_workbook=pinned)
                c.close(force=True)
            self.assertEqual(received, [original])
            with self.assertRaises(TypeError):
                pinned.metadata['last_row'] = 1
            with self.assertRaises(ValueError):
                WorkbookController(spec(path='other.xlsx'), 65, prepared_workbook=pinned)


class RetirementTests(unittest.TestCase):
    def test_known_excel_exit_is_waited_before_handle_close(self):
        p = Process()
        c = initialized(p)
        c._excel_handle, c._excel_exited = 123, False
        kernel = Kernel(wait=0)
        with patch.object(ctypes, 'WinDLL', return_value=kernel, create=True):
            c.close(force=True)
        self.assertTrue(c.retirement_status()['verified'])
        self.assertEqual([x[0] for x in kernel.calls], ['wait', 'wait', 'close'])

    def test_unconfirmed_excel_exit_keeps_handle_and_cannot_verify(self):
        c = initialized(Process())
        c._excel_handle, c._excel_exited = 123, False
        kernel = Kernel(wait=258, terminate=0)
        with patch.object(ctypes, 'WinDLL', return_value=kernel, create=True):
            c.close(force=True)
        self.assertFalse(c.retirement_status()['verified'])
        self.assertEqual(c._excel_handle, 123)
        self.assertNotIn('close', [x[0] for x in kernel.calls])

    def test_unknown_ownership_and_live_reader_block_replacement(self):
        c = initialized(Process())
        c._excel_ownership_known = False
        c._excel_exited = False
        c._reader_done.clear()
        c.close(force=True)
        status = c.retirement_status()
        self.assertFalse(status['verified'])
        self.assertFalse(status['reader_done'])
        self.assertFalse(status['excel_ownership_known'])

    def test_late_pid_is_drained_then_retired_with_synchronize_access(self):
        release = threading.Event()
        entered = threading.Event()
        class DelayedOutput:
            def __iter__(self):
                yield '{"event":"excel_starting"}\n'
                entered.set()
                release.wait(2)
                yield '{"event":"excel_started","pid":77}\n'
        p = Process(stdout=DelayedOutput())
        c = initialized(p)
        c._reader_done.clear()
        c._reader_thread = threading.Thread(target=c._reader, args=(p,))
        kernel = Kernel()
        with patch.object(ctypes, 'WinDLL', return_value=kernel, create=True):
            c._reader_thread.start()
            self.assertTrue(entered.wait(1))
            closing = threading.Thread(target=c.close, kwargs={'force': True})
            closing.start()
            self.assertFalse(c.retirement_status()['verified'])
            release.set()
            closing.join(3)
            self.assertFalse(closing.is_alive())
        self.assertTrue(c.retirement_status()['verified'])
        self.assertEqual(kernel.calls[0][1][0], 0x00101001)
        self.assertEqual([x[0] for x in kernel.calls][-1], 'close')

    def test_open_process_denied_remains_unknown(self):
        p = Process(stdout=io.StringIO('{"event":"excel_starting"}\n{"event":"excel_started","pid":77}\n'))
        c = initialized(p)
        c._reader_done.clear()
        kernel = Kernel(opened=0)
        with patch.object(ctypes, 'WinDLL', return_value=kernel, create=True):
            c._reader(p)
            c.close(force=True)
        self.assertFalse(c.retirement_status()['verified'])
        self.assertFalse(c.retirement_status()['excel_ownership_known'])

    def test_invalid_pid_never_opens_process_and_ownership_stays_unknown(self):
        import json
        for pid in (True, '77', 0, -1, 0x100000000):
            with self.subTest(pid=pid):
                output = json.dumps({'event': 'excel_started', 'pid': pid}) + '\n'
                c = initialized(Process(stdout=io.StringIO(output)))
                kernel = Kernel()
                with patch.object(ctypes, 'WinDLL', return_value=kernel, create=True):
                    c._reader(c._proc)
                    c.close(force=True)
                self.assertEqual([], kernel.calls)
                self.assertFalse(c.retirement_status()['excel_ownership_known'])
                self.assertFalse(c.retirement_status()['verified'])

    def test_eof_before_starting_event_is_known_no_excel(self):
        p = Process(stdout=io.StringIO(''))
        c = initialized(p)
        c._init_requested = True
        c._reader(p)
        c.close(force=True)
        self.assertTrue(c.retirement_status()['verified'])

    def test_starting_without_pid_stays_unknown(self):
        p = Process(stdout=io.StringIO('{"event":"excel_starting"}\n'))
        c = initialized(p)
        c._init_requested = True
        c._reader(p)
        c.close(force=True)
        self.assertFalse(c.retirement_status()['excel_ownership_known'])
        self.assertFalse(c.retirement_status()['verified'])

    def test_malformed_stdout_during_init_cannot_imply_no_excel(self):
        for line in ('not-json\n', '[]\n'):
            with self.subTest(line=line):
                p = Process(stdout=io.StringIO(line))
                c = initialized(p)
                c._init_requested = True
                c._reader(p)
                c.close(force=True)
                self.assertFalse(c.retirement_status()['verified'])
                self.assertFalse(c.retirement_status()['excel_ownership_known'])

    def test_status_returns_conservative_busy_without_waiting(self):
        c = initialized(Process())
        with c._lifecycle_lock:
            status = c.retirement_status()
        self.assertFalse(status['verified'])
        self.assertEqual(status['reason'], 'lifecycle state busy')

    def test_worker_kill_failure_and_temp_failure_do_not_verify(self):
        class BrokenProcess(Process):
            def kill(self):
                raise OSError('denied')
        class BrokenTemp:
            def cleanup(self):
                raise OSError('locked')
        c = initialized(BrokenProcess())
        c._temp = BrokenTemp()
        c.close(force=True)
        status = c.retirement_status()
        self.assertFalse(status['verified'])
        self.assertFalse(status['worker_exited'])
        self.assertFalse(status['temp_deleted'])
        self.assertIsNotNone(c._proc)

    def test_constructor_in_flight_never_verifies(self):
        c = initialized(Process())
        c._constructor_done.clear()
        c.close(force=True)
        self.assertFalse(c.retirement_status()['verified'])
        self.assertFalse(c.retirement_status()['complete'])


if __name__ == '__main__':
    unittest.main()
