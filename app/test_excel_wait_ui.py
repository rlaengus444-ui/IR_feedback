"""New Excel wait/hold statuses must keep the actual GUI polling chain alive."""
import tempfile
from pathlib import Path
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from output_limit import MaximumIrPreference
from session import SessionController, WorkerEvent
from standalone_ir import ThermalFeedbackWindow
from test_session import TARGET, DeferredThread, snapshot


class ExcelWaitUITests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.controller = SessionController(thread_factory=DeferredThread)
        self.view = ThermalFeedbackWindow(self.root, controller=self.controller, auto_discover=False,
            maximum_ir_preference=MaximumIrPreference(Path(temporary.name)/'limit.json'))
        self.addCleanup(self.view.archive.close)
        self.view.targets = [TARGET]
        self.view._populate_hosts()
        self.view.connect()
        self.emit('connected', {})
        self.view._pump()

    def emit(self, kind, value):
        self.controller.events.put(WorkerEvent(self.controller.generation, kind, value))

    def test_wait_and_latched_hold_keep_connection_then_next_snapshot_reaches_chart(self):
        for index, mode in enumerate(('calculation-waiting', 'calculation-hold')):
            self.emit('snapshot', dict(snapshot=snapshot(str(index), index), effective_mode=mode,
                proposed={}, reasons={}, applied={}, status='Excel 대기 · 재연결 필요'))
            with patch.object(self.root, 'after', wraps=self.root.after) as schedule:
                self.view._pump()
            self.assertEqual('connected', self.controller.state)
            self.assertIn('Excel', self.view.status.get())
            self.assertTrue(any(call.args == (50, self.view._pump) for call in schedule.call_args_list))
        self.assertEqual(2, len(self.view.charts.history.points))
        self.assertEqual('normal', str(self.view.disconnect_button['state']))

    def test_calculation_hold_event_is_logged_without_finishing_or_clearing_records(self):
        self.emit('calculation_hold', 'operation=reset elapsed=30.1 phase=reset:open_workbook')
        self.view._pump()
        self.assertEqual('connected', self.controller.state)
        self.assertIn('operation=reset', self.view.log.get('1.0','end'))
        self.assertIsNotNone(self.controller.thread)
