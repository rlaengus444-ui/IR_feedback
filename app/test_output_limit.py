"""Offline maximum IR limit and session safety tests."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from adapter_contracts import UiAdapter
from layer_algorithm import LayerAdaptiveController, LayerRecommendation
from output_limit import parse_maximum_ir, validate_maximum_ir
from session import SessionOptions, run_ui_session
from test_session import TARGET, snapshot


class MaximumIrValidationTests(unittest.TestCase):
    def test_valid_numeric_and_user_text(self):
        for value in (0, 0.5, 62.5, 100):
            with self.subTest(value=value):
                self.assertEqual(value, validate_maximum_ir(value))
                self.assertEqual(value, parse_maximum_ir(str(value)))

    def test_invalid_values(self):
        for value in (True, False, None, '10', -0.01, 100.01, float('nan'), float('inf'), -float('inf')):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                validate_maximum_ir(value)
        for value in ('', ' ', 'nan', 'inf', '-1', '100.01', 'abc'):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                parse_maximum_ir(value)

    def test_session_option_rejects_invalid_limit(self):
        for value in (True, -1, 101, float('nan')):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                SessionOptions(TARGET, maximum_ir_percent=value)


class SessionMaximumIrTests(unittest.TestCase):
    def run_frames(self, frames, **options):
        adapter = Mock(spec=UiAdapter)
        adapter.read_snapshot.side_effect = frames
        stop = threading.Event()
        reports = []
        def emit(kind, payload):
            if kind == 'snapshot':
                reports.append(payload)
                if len(reports) == len(frames):
                    stop.set()
        run_ui_session(SessionOptions(TARGET, **options), stop, emit, lambda: adapter)
        return adapter, reports

    def frames(self, actual, count=4):
        return [snapshot(str(i), i, control_available=True, coupled_output=False,
                         observation_is_new=i > 0, t_recoating_c=50,
                         heater1_setting_percent=actual, heater2_setting_percent=actual)
                for i in range(count)]

    def test_zero_and_decimal_limits_in_preview_and_control_for_both_heaters(self):
        for mode in ('preview', 'control'):
            for cap, actual in ((0, 0), (62.5, 60)):
                with self.subTest(mode=mode, cap=cap):
                    adapter, reports = self.run_frames(self.frames(actual), mode=mode, maximum_ir_percent=cap)
                    proposed = reports[-1]['proposed']
                    self.assertEqual({1, 2}, set(proposed))
                    self.assertTrue(all(0 <= power <= cap and abs(power - actual) <= 5 for power in proposed.values()))
                    if mode == 'preview':
                        adapter.apply_ir_power.assert_not_called()
                    for call in adapter.apply_ir_power.call_args_list:
                        self.assertIn(call.args[0], (1, 2))
                        self.assertLessEqual(call.args[1], cap)
                        self.assertLessEqual(abs(call.args[1] - actual), 5)

    def test_above_user_limit_ramps_on_each_fresh_layer(self):
        for mode in ('preview', 'control'):
            with self.subTest(mode=mode):
                actuals = (80, 80, 75, 70, 65, 60)
                frames = [snapshot(str(i), i, control_available=True, coupled_output=False,
                                   observation_is_new=i > 0, t_recoating_c=50,
                                   heater1_setting_percent=actual, heater2_setting_percent=actual)
                          for i, actual in enumerate(actuals)]
                adapter, reports = self.run_frames(frames, mode=mode, maximum_ir_percent=60)
                self.assertEqual({}, reports[0]['proposed'])
                self.assertEqual([{1: value, 2: value} for value in (75, 70, 65, 60, 60)],
                                 [report['proposed'] for report in reports[1:]])
                if mode == 'control':
                    self.assertEqual([75, 75, 70, 70, 65, 65, 60, 60],
                                     [call.args[1] for call in adapter.apply_ir_power.call_args_list])
                else:
                    adapter.apply_ir_power.assert_not_called()

    def test_above_json_hard_bound_holds_until_baseline_and_fresh_row(self):
        document = json.loads(Path(__file__).with_name('workbook-replay.settings.json').read_text())
        document['parameters']['maximum_power_percent'] = 58
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            path.write_text(json.dumps(document))
            for mode in ('preview', 'control'):
                with self.subTest(mode=mode):
                    actuals = (60, 60, 58, 58, 58, 58)
                    frames = [snapshot(str(i), i, control_available=True, coupled_output=False,
                                       observation_is_new=i > 0, t_recoating_c=50,
                                       heater1_setting_percent=actual, heater2_setting_percent=actual)
                              for i, actual in enumerate(actuals)]
                    adapter, reports = self.run_frames(frames, mode=mode, settings_path=path)
                    self.assertTrue(all(report['effective_mode'] == 'power-limit-waiting'
                                        and not report['proposed'] for report in reports[:2]))
                    self.assertEqual({}, reports[2]['proposed'])
                    self.assertTrue(reports[-1]['proposed'])
                    adapter.apply_ir_power.assert_not_called()

    def test_json_minimum_above_user_cap_is_rejected(self):
        document = json.loads(Path(__file__).with_name('workbook-replay.settings.json').read_text())
        document['parameters']['minimum_power_percent'] = 50
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            path.write_text(json.dumps(document))
            adapter = Mock(spec=UiAdapter)
            with self.assertRaises(ValueError):
                run_ui_session(SessionOptions(TARGET, mode='control', settings_path=path,
                                              maximum_ir_percent=49), threading.Event(), lambda *_: None,
                               lambda: adapter)
            adapter.apply_ir_power.assert_not_called()

    def test_builtin_history_uses_effective_cap_for_saturation(self):
        controllers = []
        def make_controller(settings):
            controller = LayerAdaptiveController(settings)
            controllers.append(controller)
            return controller
        with patch('session.LayerAdaptiveController', side_effect=make_controller):
            self.run_frames(self.frames(60), mode='preview', maximum_ir_percent=62.5)
        self.assertEqual(2, len(controllers))
        for controller in controllers:
            self.assertEqual(100, controller.settings.maximum_power_percent)
            self.assertEqual(62.5, controller._history[-1]['D'])
            # Seed an eligible beta-observation block to exercise saturation.
            controller._history[-1].update(AR=3, AW=1, AY=10, AZ=0, BA=0,
                BB=0, BC=0, BD=1, BE=0, BF=0, AU=0, D=62.5, V=0)
            row = controller.process(5, 50, 62.5, maximum_power_percent=62.5).diagnostics
            self.assertEqual(1, row['AV'])
            self.assertEqual(1, row['BF'])
            self.assertEqual(62.5, row['D'])

    def test_lower_json_maximum_is_effective_controller_bound(self):
        document = json.loads(Path(__file__).with_name('workbook-replay.settings.json').read_text())
        document['parameters']['maximum_power_percent'] = 58
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            path.write_text(json.dumps(document))
            controllers = []
            def make_controller(settings):
                controller = LayerAdaptiveController(settings)
                controllers.append(controller)
                return controller
            with patch('session.LayerAdaptiveController', side_effect=make_controller):
                _, reports = self.run_frames(self.frames(55), mode='preview',
                    maximum_ir_percent=62.5, settings_path=path)
            self.assertTrue(all(controller.settings.maximum_power_percent == 58 for controller in controllers))
            self.assertEqual({1: 58, 2: 58}, reports[-1]['proposed'])

    def test_adversarial_controller_cannot_save_outside_limit_or_step(self):
        for cap, unsafe in ((62.5, 62.6), (100, 70)):
            with self.subTest(cap=cap, unsafe=unsafe):
                adapter = Mock(spec=UiAdapter)
                adapter.read_snapshot.side_effect = self.frames(60, 4)
                stop = threading.Event()
                reports = []
                def emit(kind, payload):
                    if kind == 'snapshot':
                        reports.append(payload)
                        if len(reports) == 4: stop.set()
                controller = Mock()
                controller.process.return_value = LayerRecommendation(unsafe, 'fixture', {})
                with patch('session.LayerAdaptiveController', return_value=controller):
                    try:
                        run_ui_session(SessionOptions(TARGET, mode='control', maximum_ir_percent=cap),
                                       stop, emit, lambda: adapter)
                    except (ValueError, RuntimeError):
                        pass
                adapter.apply_ir_power.assert_not_called()


if __name__ == '__main__':
    unittest.main()
