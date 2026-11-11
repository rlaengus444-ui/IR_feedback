"""Manual image selection and connected dataset handoff."""
import ast
from pathlib import Path
import tkinter as tk
import unittest
from unittest.mock import patch

from session import SessionController
from standalone_ir import ThermalFeedbackWindow
from test_session import DeferredThread, TARGET
from test_slice_handoff import A, B


class ManualSliceStructureTests(unittest.TestCase):
    def test_retired_source_modules_are_not_imported(self):
        root = Path(__file__).parent
        for name in ('slice_panel.py', 'standalone_ir.py'):
            tree = ast.parse((root / name).read_text(encoding='utf-8'))
            imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
            imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                        for alias in node.names]
            self.assertNotIn('auto_slice_source', imports)
            self.assertNotIn('windows_slice_source', imports)


class ManualSliceUITests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.root.withdraw()
        self.controller = SessionController(thread_factory=DeferredThread)
        self.view = ThermalFeedbackWindow(self.root, controller=self.controller, auto_discover=False)
        self.view.targets = [TARGET]
        self.view._populate_hosts()
        self.addCleanup(self.root.destroy)
        self.addCleanup(self.view.archive.close)

    def test_manual_file_dialogs_and_no_automatic_controls(self):
        panel = self.view.slice_panel
        self.assertFalse(hasattr(panel, 'auto_enabled'))
        self.assertFalse(hasattr(self.view, '_auto_source'))
        with patch('slice_panel.filedialog.askopenfilename', return_value='/tmp/images.zip'):
            panel.load_zip()
        self.assertEqual(('/tmp/images.zip',), panel.paths)
        with patch('slice_panel.filedialog.askopenfilenames', return_value=('/tmp/1.png', '/tmp/2.png')):
            panel.load_files()
        self.assertEqual(('/tmp/1.png', '/tmp/2.png'), panel.paths)
        self.assertEqual('normal', str(panel.configuration_widgets[0][0]['state']))

    def test_connected_replacement_and_old_generation_rejection(self):
        panel = self.view.slice_panel
        panel.dataset = A
        self.view.connect()
        self.assertEqual('connecting', self.controller.state)
        panel.mark_session_active(A, 0)
        self.view.current_run_key='job-A'
        self.view._publish_slice_dataset(B, (self.controller.generation - 1, "job-A"))
        self.assertIsNone(panel.pending_epoch)

        self.view._publish_slice_dataset(B, (self.controller.generation, "job-A"))
        epoch, desired = self.controller.slice_dataset_setting.desired()
        self.assertIs(B, desired)
        self.assertEqual(epoch, panel.pending_epoch)
        self.assertIs(A, panel.active_dataset)
        self.assertTrue(self.controller.slice_dataset_setting.activate(epoch,"job-A"))
        self.view._reconcile_slice_state()
        self.assertIs(B, panel.active_dataset)
        self.assertIsNone(panel.pending_epoch)

    def test_actual_pid_display_recovers_without_rebound_event(self):
        from dataclasses import replace
        from test_session import snapshot
        actual=replace(TARGET,pid=TARGET.pid+10,hwnd=TARGET.hwnd+10)
        self.view._show_snapshot(dict(snapshot=snapshot('new',1,actual_target=actual),
            effective_mode='control',proposed={},reasons={},applied={},status='fixture'))
        self.assertIn('PID '+str(actual.pid),self.view.host.get())
        self.assertEqual(actual,self.view.target_map[self.view.host.get()])


if __name__ == '__main__':
    unittest.main()
