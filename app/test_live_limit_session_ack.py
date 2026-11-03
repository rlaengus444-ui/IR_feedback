"""Offline checks for cap acknowledgement at the accepted-decision boundary."""
import unittest
from unittest.mock import patch
from test_live_limit_runtime import LiveHarness
from session import SessionController, SessionOptions, WorkerEvent
from test_session import TARGET, DeferredThread, snapshot


class CapAcknowledgementTests(unittest.TestCase):
    def test_stale_queued_decision_is_hidden_but_confirmed_save_remains(self):
        controller = SessionController(lambda *args: None, DeferredThread)
        options = SessionOptions(TARGET, mode='control', maximum_ir_percent=80)
        controller.connect(options)
        setting = controller.maximum_ir_setting
        setting.bind_bounds(0, 100, 5)
        old = setting.snapshot()
        self.assertTrue(setting.activate(old))
        controller.events.put(WorkerEvent(controller.generation, 'snapshot', {
            'snapshot': snapshot('one', 1), 'proposed': {1: 75}, 'reasons': {1: 'Excel'},
            'applied': {1: 75}, 'status': '저장 확인', 'decision_cap_stamp': old,
            # The cap can change during an admitted Save; the payload's
            # status may already contain the newer request.
            'limit_status': {'requested_stamp': (1, 60.0)},
        }))
        setting.set(60)
        payload = controller.poll()[0].payload
        self.assertEqual({}, payload['proposed'])
        self.assertEqual({}, payload['reasons'])
        self.assertEqual({1: 75}, payload['applied'])
        self.assertIn('이전 권장값', payload['status'])
        self.assertEqual(setting.snapshot(), payload['limit_status']['requested_stamp'])

    def test_legacy_snapshot_without_decision_tag_is_not_filtered(self):
        controller = SessionController(lambda *args: None, DeferredThread)
        options = SessionOptions(TARGET, mode='preview')
        controller.connect(options)
        controller.maximum_ir_setting.bind_bounds(0, 100)
        controller.events.put(WorkerEvent(controller.generation, 'snapshot', {
            'snapshot': snapshot('one', 1), 'proposed': {1: 50}, 'reasons': {1: 'fixture'},
            'applied': {}, 'status': 'fixture',
        }))
        self.assertEqual({1: 50}, controller.poll()[0].payload['proposed'])

    def test_initial_and_changed_cap_remain_pending_during_old_calculation(self):
        h = LiveHarness(initial=80, actual=80, block_process=True)
        self.addCleanup(h.close)
        h.start()
        setting = h.options._maximum_ir_setting
        self.assertTrue(setting.status()['pending'])
        self.assertIsNone(setting.status()['active_stamp'])
        h.row(1)
        self.assertTrue(h.process_entered.wait(1))
        setting.set(60)
        self.assertTrue(setting.status()['pending'])
        self.assertIsNone(setting.status()['active_stamp'])
        h.process_release.set()
        h.wait(lambda: h.resets >= 1 and h.group.state == 'ready')
        h.wait(lambda: h.reads >= 5)
        self.assertTrue(setting.status()['pending'])
        self.assertIsNone(setting.status()['active_stamp'])
        self.assertEqual(0, h.adapter.apply_ir_power.call_count)
        h.row(2)
        h.wait(lambda: h.adapter.apply_ir_power.call_count == 1)
        self.assertFalse(setting.status()['pending'])
        self.assertEqual(setting.snapshot(), setting.status()['active_stamp'])
        self.assertEqual(75, h.adapter.apply_ir_power.call_args.args[1])

    def test_cap_change_at_activation_discards_decision_and_waits_fresh(self):
        h = LiveHarness(initial=80, actual=80)
        self.addCleanup(h.close)
        h.start()
        setting = h.options._maximum_ir_setting
        activate = setting.activate
        def changed(stamp):
            setting.set(60)
            return activate(stamp)
        with patch.object(setting, 'activate', side_effect=changed):
            h.row(1)
            h.wait(lambda: h.resets >= 1)
        self.assertEqual(0, h.adapter.apply_ir_power.call_count)
        self.assertIsNone(setting.status()['active_stamp'])
        self.assertTrue(setting.status()['pending'])
        h.wait(lambda: h.group.state == 'ready')
        reads = h.reads
        h.wait(lambda: h.reads >= reads + 2)
        h.row(2)
        h.wait(lambda: h.adapter.apply_ir_power.call_count == 1)
        self.assertEqual(75, h.adapter.apply_ir_power.call_args.args[1])
        self.assertEqual(setting.snapshot(), setting.status()['active_stamp'])


if __name__ == '__main__':
    unittest.main()
