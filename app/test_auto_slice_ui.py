"""Offline product flow: explicit source choice, configuration and late events."""
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from session import SessionController
from standalone_ir import ThermalFeedbackWindow
from test_session import TARGET, DeferredThread
from windows_slice_source import PathBinding, SourceSelection


class AutoSourceUITests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw();self.addCleanup(self.root.destroy)
        self.controller=SessionController(thread_factory=DeferredThread)
        self.view=ThermalFeedbackWindow(self.root,controller=self.controller,auto_discover=False)
        self.view.targets=[TARGET];self.view._populate_hosts()
        self.binding=PathBinding(TARGET,101,201,(),'Home')
        self.selection=SourceSelection('C:\\slices','C:\\slices\\001.png',self.binding)
        self.addCleanup(self.view.archive.close)

    def test_default_preserves_manual_mode_and_initial_binding_is_explicit(self):
        panel=self.view.slice_panel
        self.assertFalse(panel.auto_enabled.get())
        self.assertEqual('normal',str(panel.manual_widgets[0]['state']))
        self.assertIsNone(self.view._source_binding)
        self.view._binding_serial=1
        self.view._binding_queue.put((1,self.controller.generation,TARGET,self.binding,self.selection,None))
        with patch('standalone_ir.messagebox.askyesno',return_value=True) as choose:
            self.view._poll_source_binding()
        choose.assert_called_once()
        self.assertIs(self.binding,self.view._source_binding)
        self.assertTrue(panel.auto_enabled.get())
        self.assertEqual('disabled',str(panel.manual_widgets[0]['state']))

    def test_old_connection_binding_preview_cannot_bind_new_connection(self):
        self.view._binding_serial=2
        self.view._binding_queue.put((2,self.controller.generation-1,TARGET,self.binding,self.selection,None))
        with patch('standalone_ir.messagebox.askyesno') as choose:self.view._poll_source_binding()
        choose.assert_not_called();self.assertIsNone(self.view._source_binding)

    def test_auto_connect_needs_binding_and_radius_and_remains_reconfigurable(self):
        panel=self.view.slice_panel;panel.auto_enabled.set(True)
        self.view.connect();self.assertIsNone(self.controller.thread)
        self.view._source_binding=self.binding
        self.view.connect();self.assertIsNone(self.controller.thread)
        panel.radius.set('5');self.view.connect()
        self.assertEqual('connecting',self.controller.state)
        self.assertIsNotNone(self.view._auto_source)
        self.assertEqual(1,self.view._auto_source.config.filename_origin)
        self.assertEqual('disabled',str(panel.auto_box['state']))
        self.assertEqual('normal',str(panel.bind_button['state']))
        panel.radius.set('7');self.assertEqual(7,self.view._auto_source.config.radius_mm)
        panel.auto_origin.set('0');self.assertEqual(0,self.view._auto_source.config.filename_origin)
        panel.radius.set('');self.assertIsNone(self.view._auto_source.config)
        self.assertFalse(self.view._auto_source.view()['ready'])
        self.view._auto_source.stop()

    def test_canceling_path_choice_preserves_previous_explicit_binding(self):
        self.view._source_binding=self.binding;self.view._binding_serial=3
        self.view._binding_queue.put((3,self.controller.generation,TARGET,self.binding,self.selection,None))
        with patch('standalone_ir.messagebox.askyesno',return_value=False):self.view._poll_source_binding()
        self.assertIs(self.binding,self.view._source_binding)

    def test_binding_error_is_visible_logged_and_not_overwritten_by_watcher(self):
        self.view._source_binding = self.binding
        self.view._binding_serial = 4
        watcher = Mock()
        watcher.view.return_value = dict(path='C:\\old', status='자동 경로 감시 중', needs_reload=False, epoch=None)
        self.view._auto_source = watcher
        self.view._binding_queue.put((4, self.controller.generation, TARGET, None, None, '표시 칸의 전체 경로를 읽지 못했습니다.'))
        with patch('standalone_ir.messagebox.showerror') as error, patch('standalone_ir.write_error_log') as persist:
            self.view._pump()
        error.assert_called_once()
        persist.assert_called_once()
        self.assertIn('전체 경로', self.view.slice_panel.binding_status.get())
        self.assertEqual('자동 경로 감시 중', self.view.slice_panel.auto_status.get())
        self.assertIs(self.binding, self.view._source_binding)
        watcher.rebind.assert_called_once_with(self.binding)

    def test_stale_closed_or_wrong_target_binding_error_does_not_show_dialog(self):
        from dataclasses import replace
        for serial, generation, target, closing in (
                (0, self.controller.generation, TARGET, False),
                (5, self.controller.generation-1, TARGET, False),
                (5, self.controller.generation, replace(TARGET, pid=999), False),
                (5, self.controller.generation, TARGET, True)):
            with self.subTest(serial=serial, generation=generation, closing=closing):
                self.view._binding_serial = 5
                self.view.closing = closing
                self.view._binding_queue.put((serial, generation, target, None, None, 'old error'))
                with patch('standalone_ir.messagebox.showerror') as error:
                    self.view._poll_source_binding()
                error.assert_not_called()
        self.view.closing = False

    def test_invalid_or_empty_binding_result_is_failure_not_preview(self):
        for failure in (None, '', '   '):
            with self.subTest(failure=failure):
                self.view._binding_serial = 6
                self.view._binding_queue.put((6, self.controller.generation, TARGET, None, None, failure))
                with patch('standalone_ir.messagebox.showerror') as error, patch('standalone_ir.messagebox.askyesno') as preview, patch('standalone_ir.write_error_log'):
                    self.view._poll_source_binding()
                error.assert_called_once()
                preview.assert_not_called()
                self.assertIsNone(self.view._source_binding)

    def test_missing_target_reports_reason_without_starting_capture(self):
        self.view.target_map.clear()
        with patch('standalone_ir.messagebox.showwarning') as warning, patch('standalone_ir.threading.Thread') as worker:
            self.view._bind_source()
        warning.assert_called_once()
        worker.assert_not_called()

    def test_context_changed_during_preview_cannot_accept_old_binding(self):
        for action in ('close', 'new_ticket', 'target'):
            with self.subTest(action=action):
                self.view.closing = False
                self.view._binding_serial = 7
                self.view._binding_queue.put((7, self.controller.generation, TARGET, self.binding, self.selection, None))
                def answer(*args, **kwargs):
                    if action == 'close':self.view.closing = True
                    elif action == 'new_ticket':self.view._binding_serial += 1
                    else:self.view.target_map.clear()
                    return True
                with patch('standalone_ir.messagebox.askyesno', side_effect=answer):
                    self.view._poll_source_binding()
                self.assertIsNone(self.view._source_binding)
                self.view.closing = False
                self.view._populate_hosts()


if __name__=='__main__':unittest.main()
