import io
import queue
import threading
import unittest

from excel_workbook import WorkbookController, WorkbookTimeout, WorkbookTransportError


class Process:
    def __init__(self, stdin=None, stdout=None):
        self.stdin = stdin or io.StringIO()
        self.stdout = stdout or io.StringIO()
        self.stopped = False
        self.kills = 0

    def poll(self):
        return 0 if self.stopped else None

    def kill(self):
        self.kills += 1
        self.stopped = True

    def wait(self, timeout=None):
        return 0


def controller(proc):
    c = object.__new__(WorkbookController)
    c._proc, c._queue, c._excel_handle, c._temp = proc, queue.Queue(), None, None
    c._lifecycle_lock, c._closed = threading.Lock(), False
    c._reader_done = threading.Event()
    c._reader_done.set()
    c._phase = None
    c.timeout, c.stop_event = 0.02, threading.Event()
    return c


class TransportTests(unittest.TestCase):
    def test_deadline_closes_and_late_reply_cannot_be_reused(self):
        p = Process()
        c = controller(p)
        with self.assertRaises(WorkbookTimeout) as caught:
            c._call({'op': 'step'})
        self.assertEqual(caught.exception.operation, 'step')
        self.assertEqual(caught.exception.deadline, 0.02)
        self.assertEqual(caught.exception.process_state, 'running')
        self.assertGreaterEqual(caught.exception.elapsed, 0.02)
        self.assertEqual(p.kills, 1)
        c._queue.put({'value': 42})
        with self.assertRaises(InterruptedError):
            c._call({'op': 'step'})
        c.close(force=True)
        self.assertEqual(p.kills, 1)

    def test_broken_pipe_is_transport_failure(self):
        class Broken(io.StringIO):
            def write(self, value):
                raise BrokenPipeError('closed')
        p = Process(stdin=Broken())
        c = controller(p)
        with self.assertRaises(WorkbookTransportError) as caught:
            c._call({'op': 'reset'})
        self.assertEqual(caught.exception.phase, 'send')
        self.assertTrue(p.stopped)

    def test_eof_and_progress_are_not_normal_responses(self):
        class ReaderOnWrite(io.StringIO):
            def write(self, value):
                result = super().write(value)
                c._reader(p)
                return result
        p = Process(stdin=ReaderOnWrite(), stdout=io.StringIO('{"event":"progress","phase":"calculate"}\n'))
        c = controller(p)
        with self.assertRaises(WorkbookTransportError) as caught:
            c._call({'op': 'step'})
        self.assertEqual(caught.exception.operation, 'step')
        self.assertEqual(caught.exception.phase, 'calculate')
        self.assertTrue(p.stopped)

    def test_callback_can_abort_before_open_or_spawn(self):
        seen = []
        def abort(c):
            seen.append(c)
            c.close(force=True)
        with self.assertRaises(InterruptedError):
            WorkbookController(None, 60, on_created=abort)
        self.assertEqual(len(seen), 1)
        self.assertIsNone(seen[0]._proc)

    def test_formula_error_is_terminal_not_timeout_or_transport(self):
        p = Process()
        c = controller(p)
        c._queue.put({'error': 'bad formula'})
        with self.assertRaises(RuntimeError) as caught:
            c._call({'op': 'step'})
        self.assertNotIsInstance(caught.exception, (WorkbookTimeout, WorkbookTransportError))
        self.assertIn('bad formula', str(caught.exception))
        self.assertTrue(p.stopped)

    def test_cancel_before_write_closes_owned_worker(self):
        p = Process()
        c = controller(p)
        c.stop_event.set()
        with self.assertRaises(InterruptedError):
            c._call({'op': 'step'})
        self.assertEqual(p.stdin.getvalue(), '')
        self.assertEqual(p.kills, 1)

    def test_reset_has_longer_budget(self):
        c = object.__new__(WorkbookController)
        c._count, c._last_layer, c.timeout = 1, 4, 8
        calls = []
        c._call = lambda command, timeout=None: calls.append((command, timeout))
        c.reset()
        self.assertEqual(calls, [({'op': 'reset'}, 30.0)])
        self.assertEqual(c._count, 0)


if __name__ == '__main__':
    unittest.main()
