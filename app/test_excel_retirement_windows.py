"""Exercise process-handle retirement on Windows without launching Excel."""
import io
import os
import subprocess
import sys
import threading
import unittest

from test_excel_retirement import initialized
from test_excel_transport import Process


@unittest.skipUnless(os.name == 'nt', 'requires Windows process handles')
class WindowsRetirementTests(unittest.TestCase):
    def test_owned_process_handle_is_terminated_and_verified(self):
        # This child is owned by the test and waits only for its stdin. No
        # Excel, device software, or unrelated process is inspected or killed.
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        try:
            output = io.StringIO('{"event":"excel_starting"}\n'
                                 '{"event":"excel_started","pid":%d}\n' % child.pid)
            helper = Process(stdout=output)
            c = initialized(helper)
            c._reader_done.clear()
            c._reader_thread = threading.Thread(target=c._reader, args=(helper,))
            c._reader_thread.start()
            c._reader_thread.join(timeout=5)
            self.assertTrue(c._reader_done.is_set())
            self.assertTrue(c.retirement_status()['excel_ownership_known'])
            self.assertIsNotNone(c._excel_handle)
            c.close(force=True)
            child.wait(timeout=5)
            status = c.retirement_status()
            self.assertTrue(status['verified'], status)
            self.assertTrue(status['worker_exited'])
            self.assertTrue(status['reader_done'])
            self.assertTrue(status['excel_exited'])
            self.assertTrue(status['temp_deleted'])
            self.assertIsNone(c._excel_handle)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            if child.stdin is not None:
                child.stdin.close()


if __name__ == '__main__':
    unittest.main()
