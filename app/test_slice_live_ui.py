import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from slice_panel import SlicePanel
from standalone_ir import ThermalFeedbackWindow


class SliceLiveUITests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.published = []
        self.token = 7
        self.panel = SlicePanel(self.root, publish_dataset=lambda value, token: self.published.append((value, token)),
                                context_token=lambda: self.token)
        self.a = SimpleNamespace(fingerprint='a' * 64)
        self.record = SimpleNamespace(layer=1, name='001.png', center_coverage_pct=1,
                                      center_intensity_pct=2, whole_coverage_pct=3, roi_pixels=4)
        self.b = SimpleNamespace(fingerprint='b' * 64, records=[self.record])
        self.panel.mark_session_active(self.a, 0)

    def pump_event(self, generation, token, value=None, error=None):
        self.panel.events.put((generation, token, value, error))
        self.panel._pump()

    def test_error_cancel_stale_and_clear_preserve_active(self):
        self.panel.generation = 3
        self.pump_event(2, 7, self.b)
        self.pump_event(3, 7, error='failed')
        self.panel.cancel()
        self.assertIs(self.panel.active_dataset, self.a)
        self.assertEqual([], self.published)
        self.panel.clear()
        self.assertEqual([(None, 7)], self.published)
        self.assertIs(self.panel.active_dataset, self.a)

    def test_success_publishes_with_start_context_and_waits_for_ack(self):
        self.panel.generation = 4
        self.token = 8
        self.pump_event(4, 7, self.b)
        self.assertEqual([(self.b, 7)], self.published)
        self.panel.mark_pending(1)
        self.assertIs(self.panel.active_dataset, self.a)
        self.panel.mark_session_active(self.b, 1)
        self.assertIs(self.panel.active_dataset, self.b)
        self.assertIsNone(self.panel.pending_epoch)

    def test_session_callback_rejects_old_context_and_inactive_session(self):
        calls = []
        controller = SimpleNamespace(generation=8, thread=object(), state='connected',
                                     slice_dataset_setting=SimpleNamespace(active=lambda: (0, self.a)),
                                     update_slice_dataset=lambda value: calls.append(value) or 2)
        window = SimpleNamespace(controller=controller, closing=False, slice_panel=self.panel,
                                 _append_log=lambda message: None)
        ThermalFeedbackWindow._publish_slice_dataset(window, self.b, 7)
        self.assertEqual([], calls)
        ThermalFeedbackWindow._publish_slice_dataset(window, self.b, 8)
        self.assertEqual([self.b], calls)
        self.assertIs(self.panel.active_dataset, self.a)
        self.assertEqual(2, self.panel.pending_epoch)
        controller.state = 'disconnecting'
        ThermalFeedbackWindow._publish_slice_dataset(window, None, 8)
        self.assertEqual([self.b], calls)

    def test_connected_controls_and_workbook_lock(self):
        self.panel.workbook_busy = lambda: True
        self.panel.set_locked(False)
        self.assertEqual('normal', str(self.panel.configuration_widgets[0][0]['state']))
        self.assertEqual('disabled', str(self.panel.workbook_button['state']))
        self.panel.set_locked(True)
        self.assertEqual('disabled', str(self.panel.configuration_widgets[0][0]['state']))

    def test_window_locks_workbook_and_acknowledges_only_latest_activation(self):
        class Setting:
            current = None
            def active(self): return self.current
            def desired(self): return (2, self_dataset)
        self_dataset = self.a
        setting = Setting()
        setting.current = (0, self.a)
        controller = SimpleNamespace(thread=object(), state='connected', generation=7,
                                     slice_dataset_setting=setting, error=None,
                                     poll=lambda: [], worker_alive=True)
        window = ThermalFeedbackWindow(self.root, controller=controller, auto_discover=False)
        window._refresh_buttons()
        self.assertEqual('disabled', str(window.algorithm_box['state']))
        self.assertTrue(all(str(widget['state']) == 'disabled' for widget, _ in window.configuration_widgets))
        self.assertEqual('normal', str(window.slice_panel.configuration_widgets[0][0]['state']))
        self.assertEqual('disabled', str(window.slice_panel.workbook_button['state']))
        window.slice_panel.mark_session_active(self.a, 0)
        window.slice_panel.mark_pending(2)
        events = [SimpleNamespace(kind='slice_activated', payload={'epoch': 1, 'dataset': self.b}),
                  SimpleNamespace(kind='slice_activated', payload={'epoch': 2, 'dataset': self.b})]
        def poll():
            if not events:return []
            event=events.pop(0)
            if event.payload['epoch']==2:setting.current=(2,self.b)
            return [event]
        controller.poll = poll
        window._pump()
        self.assertIs(window.slice_panel.active_dataset, self.a)
        window._pump()
        self.assertIs(window.slice_panel.active_dataset, self.b)
        self.assertIsNone(window.slice_panel.pending_epoch)

    def test_lost_activation_event_reconciles_from_authoritative_binding(self):
        from session import SessionController, SessionOptions, WorkerEvent
        from test_session import TARGET, DeferredThread, snapshot
        from test_slice_handoff import A, B, SPEC
        controller=SessionController(lambda *a: None, DeferredThread)
        controller.connect(SessionOptions(TARGET,mode='preview',workbook_spec=SPEC,slice_dataset=A))
        controller.state='connected'
        window=ThermalFeedbackWindow(self.root,controller=controller,auto_discover=False)
        window.slice_panel.mark_session_active(A,0)
        epoch=controller.update_slice_dataset(B)
        window.slice_panel.mark_pending(epoch)
        controller.slice_dataset_setting.activate(epoch)
        controller.events.put(WorkerEvent(controller.generation,'slice_activated',dict(epoch=epoch,dataset=B)))
        for i in range(129):
            controller.events.put(WorkerEvent(controller.generation,'snapshot',dict(
                snapshot=snapshot(str(i),i),effective_mode='preview',proposed={},reasons={},applied={},
                status='fixture',slice_epoch=epoch)))
        window._pump()
        self.assertIs(B,window.slice_panel.active_dataset)
        self.assertEqual(epoch,window.slice_panel.active_epoch)
        self.assertIsNone(window.slice_panel.pending_epoch)

    def test_same_active_result_does_not_leave_pending_badge(self):
        controller=SimpleNamespace(generation=7,thread=object(),state='connected',
            update_slice_dataset=lambda value:0,
            slice_dataset_setting=SimpleNamespace(active=lambda:(0,self.a)))
        window=SimpleNamespace(controller=controller,closing=False,slice_panel=self.panel,
                               _append_log=lambda value:None)
        ThermalFeedbackWindow._publish_slice_dataset(window,self.a,7)
        self.assertIsNone(self.panel.pending_epoch)


if __name__ == '__main__':
    unittest.main()
