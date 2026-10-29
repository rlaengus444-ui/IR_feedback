"""Offline product flow: explicit source choice, configuration and late events."""
import tkinter as tk
import unittest
from unittest.mock import patch

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


if __name__=='__main__':unittest.main()
