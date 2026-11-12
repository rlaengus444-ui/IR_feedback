"""Threaded history continuity across the raw CSV freshness window."""
from dataclasses import replace
import threading
import unittest
from types import SimpleNamespace

from test_excel_session_async import Harness


class HistoryHarness(Harness):
    def __init__(self, *, block=False):
        super().__init__()
        self.initializations = 0
        self.resets = 0
        self.block = block
        h = self

        class Controller:
            def __init__(self, spec, target, **kwargs):
                self.closed = False
                self.history = []
                h.initializations += 1
            def set_target_temperature(self, target):
                pass
            def reset(self):
                self.history.clear()
                h.resets += 1
                h.reset_entered.set()
            def process(self, layer, temperature, power, *, slice_features=None):
                self.history.append(layer)
                with h.condition:
                    h.calls.append((layer, tuple(self.history)))
                    h.condition.notify_all()
                h.entered.set()
                if h.block and not h.release.wait(4):
                    raise RuntimeError('test did not release calculation')
                return SimpleNamespace(power_percent=power + (1 if len(self.history) >= 3 else 0), reason='history')
            def close(self, force=False):
                self.closed = True
            def retirement_status(self):
                return {'verified': self.closed}
        self.factory = Controller

    def set_frame(self, frame):
        with self.condition:
            self.frame = frame
            self.condition.notify_all()

    def wait_frame(self, frame):
        self.set_frame(frame)
        self.wait(lambda: any(k == 'snapshot' and p['snapshot'] is frame for k, p in self.events))

    def accepted(self, key):
        self.wait(lambda: any(k == 'snapshot' and p['snapshot'].observation_key == key
                              and p['proposed'] for k, p in self.events))

    def expired(self, key, layer, **changes):
        return replace(self.row(None, layer), observation_is_new=False,
                       observation_fresh=False, expired_observation_key=key, **changes)


class HistoryContinuityTests(unittest.TestCase):
    def harness(self, **kwargs):
        h = HistoryHarness(**kwargs)
        self.addCleanup(h.close)
        return h.start()

    def test_expired_accepted_rows_preserve_consecutive_history_and_third_save(self):
        h = self.harness()
        for layer in (1, 2, 3):
            key = f'fresh-{layer}'
            h.wait_frame(h.row(key, layer))
            h.accepted(key)
            if layer < 3:
                h.wait_frame(h.expired(key, layer))
                # Let another polling cycle confirm the expired state remains idle.
                reads = h.reads
                h.wait(lambda: h.reads >= reads + 2)
                self.assertEqual(0, h.resets)
                h.adapter.apply_ir_power.assert_not_called()
        h.wait(lambda: h.adapter.apply_ir_power.call_count == 1)
        self.assertEqual([2, 3, 4], [c[0] for c in h.calls])
        self.assertEqual((2, 3, 4), h.calls[-1][1])
        self.assertEqual(1, h.initializations)
        self.assertEqual(0, h.resets)
        self.assertEqual((1, 51, 'fresh-3'), h.adapter.apply_ir_power.call_args.args[:3])

    def test_invalidated_pending_observation_retires_owner_and_discards_result(self):
        h = self.harness(block=True)
        h.wait_frame(h.row('pending', 1))
        h.wait(h.entered.is_set)
        previous=h.group
        h.wait_frame(h.expired('pending',1))
        h.release.set()
        h.wait(lambda:previous.retirement_status().get('verified'))
        reads = h.reads
        h.wait(lambda: h.reads >= reads + 2)
        h.adapter.apply_ir_power.assert_not_called()
        self.assertEqual(0,h.resets)
        self.assertEqual('closed',previous.state)

    def test_unknown_invalid_and_readiness_loss_reset_accepted_history(self):
        for kind in ('unknown', 'invalid', 'readiness'):
            with self.subTest(kind=kind):
                h = self.harness()
                h.wait_frame(h.row('accepted', 1))
                h.accepted('accepted')
                if kind == 'unknown':
                    frame = replace(h.row(None, 1), observation_is_new=False)
                elif kind == 'invalid':
                    frame = h.expired('accepted', 1, measurement_valid=False)
                else:
                    frame = h.expired('accepted', 1, control_available=False,
                                      control_recoverable=True)
                h.wait_frame(frame)
                h.wait(h.reset_entered.is_set)
                h.adapter.apply_ir_power.assert_not_called()
                self.assertEqual(1, h.resets)
                h.close()


class SyntheticHistoryReplayTests(unittest.TestCase):
    def test_synthetic_history_only_300_layers_expire_between_rows(self):
        """Exercise frozen-row bookkeeping, not Excel formulas or equipment behavior."""
        from excel_calculation import ExcelCalculation
        from excel_workbook import WorkbookController
        clock = [0.0]
        created = []
        reset_calls = []

        class ImmediateGroup:
            def __init__(self, *args, **kwargs):
                self.state = 'new'
                self.event = None
                self.controllers = {}
                created.append(self)
            def start(self, heaters):
                for heater in heaters:
                    controller = object.__new__(WorkbookController)
                    controller.metadata = {'first_row': 13, 'last_row': 312}
                    controller.target = 65
                    controller.maximum_ir_percent = 100
                    controller._count = 0
                    controller._last_layer = None
                    controller.requests = []
                    def fake_call(request, timeout=None, controller=controller):
                        controller.requests.append(dict(request))
                        if request['op'] == 'step':
                            return {'value': request['actual'] + .1}
                        return {'ok': True}
                    controller._call = fake_call
                    self.controllers[heater] = controller
                self.state = 'ready'
                self.event = {'kind': 'ready'}
                return True
            def poll(self):
                result, self.event = self.event, None
                return result
            def submit(self, ticket, layer, temperature, powers, target, features):
                results = {}
                for heater, controller in self.controllers.items():
                    controller.set_target_temperature(target)
                    results[heater] = controller.process(layer, temperature, powers[heater], slice_features=features)
                self.event = {'kind': 'result', 'ticket': ticket, 'results': results}
                return True
            def reset(self, ticket=None):
                reset_calls.append(ticket)
                for controller in self.controllers.values():
                    controller.reset()
                self.event = {'kind': 'ready', 'ticket': ticket}
                return True
            def close(self):
                pass
            def describe(self):
                return {'state': self.state}

        calc = ExcelCalculation(None, 65, 100, threading.Event(), group_factory=ImmediateGroup,
                                clock=lambda: clock[0])
        powers = {1: 40.0, 2: 50.0}
        target = (0, 65)
        def observation(layer, key, *, expired=None):
            return SimpleNamespace(layer_index=layer, observation_key=key,
                expired_observation_key=expired, t_recoating_c=70, run_key='run',
                csv_generation='source', control_epoch=1, observation_is_new=key is not None,
                observation_fresh=key is not None, measurement_valid=True)
        def watch(row, eligible):
            calc.watch(row, eligible=eligible, powers=powers, heaters=(1, 2),
                       target_stamp=target, image_epoch=None, allow_idle_history=True)
        baseline = observation(0, 'baseline')
        watch(baseline, True)
        for layer in range(1, 301):
            clock[0] += 60
            key = f'row-{layer}'
            row = observation(layer, key)
            watch(row, True)
            result = calc.calculate(row, powers, (1, 2), target, None)
            self.assertIsNotNone(result, f'layer {layer} failed to produce a result')
            powers = {heater: result[heater].power_percent for heater in (1, 2)}
            clock[0] += 6
            expired = observation(layer, None, expired=key)
            watch(expired, False)
            if layer == 150:
                target = (1, 72)
                watch(expired, False)
            self.assertEqual([], reset_calls, f'history reset at expired layer {layer}')
        self.assertEqual(1, len(created))
        for controller in created[0].controllers.values():
            requests = controller.requests
            self.assertEqual(['step'] * 300, [r['op'] for r in requests])
            self.assertEqual(list(range(300)), [r['index'] for r in requests])
            self.assertEqual(list(range(2, 302)), [r['layer'] for r in requests])
            self.assertEqual([65] * 150 + [72] * 150, [r['target'] for r in requests])
            self.assertEqual(list(range(13, 313)), [13 + r['index'] for r in requests])
        self.assertAlmostEqual(70, powers[1])
        self.assertAlmostEqual(80, powers[2])


if __name__ == '__main__':
    unittest.main()
