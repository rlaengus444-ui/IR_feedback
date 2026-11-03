from dataclasses import replace
import unittest

from layer_algorithm import LayerAdaptiveController
from test_layer_algorithm import source_settings


class LiveLimitAlgorithmTests(unittest.TestCase):
    def test_ramp_history_and_fractional_cap(self):
        controller = LayerAdaptiveController(source_settings())
        first = controller.process(1, 65, 50, maximum_power_percent=42.5)
        self.assertEqual(45, first.power_percent)
        self.assertEqual(-5, first.diagnostics['V'])
        self.assertEqual(-5, first.diagnostics['E'])
        second = controller.process(2, 65, 45, maximum_power_percent=42.5)
        self.assertEqual(42.5, second.power_percent)
        self.assertEqual(-2.5, second.diagnostics['V'])
        self.assertEqual(-5, second.diagnostics['P'])
        third = controller.process(3, 65, 42.5, maximum_power_percent=60)
        self.assertEqual(3, third.diagnostics['O'])
        self.assertEqual(-2.5, third.diagnostics['P'])

    def test_bounds_zero_and_smaller_step(self):
        settings = replace(source_settings(), minimum_power_percent=10, maximum_power_percent=80, maximum_step_percent=2)
        controller = LayerAdaptiveController(settings)
        self.assertEqual(78, controller.process(1, 60, 80, maximum_power_percent=10).power_percent)
        with self.assertRaises(ValueError): controller.process(2, 60, 78, maximum_power_percent=9)
        self.assertEqual(2, controller.process(2, 60, 78, maximum_power_percent=10).diagnostics['O'])
        zero = LayerAdaptiveController(source_settings())
        self.assertEqual(0, zero.process(1, 65, 0, maximum_power_percent=0).power_percent)

    def test_beta_saturation_survives_raising_or_lowering_cap(self):
        for prior_cap, current_cap in ((62.5, 80), (80, 62.5)):
            with self.subTest(prior_cap=prior_cap, current_cap=current_cap):
                controller = LayerAdaptiveController(source_settings())
                for layer in (1, 2, 3):
                    controller.process(layer, 60, 62.5, maximum_power_percent=prior_cap)
                previous = controller._history[-1]
                previous.update(D=62.5, V=1, AR=3, AW=7, AY=10, AZ=8,
                                BA=2, BF=0, BE=0, BC=0, BB=0, BD=0, AU=0)
                result = controller.process(4, 60, 62.5, maximum_power_percent=current_cap)
                self.assertEqual(1, result.diagnostics['AV'])
                self.assertEqual(1, result.diagnostics['BF'])
                self.assertEqual(0, result.diagnostics['BG'])
                self.assertEqual(0.5, result.diagnostics['K'])

    def test_cap_state_is_transactional_and_reset(self):
        controller = LayerAdaptiveController(source_settings())
        controller.process(1, 60, 50, maximum_power_percent=45)
        self.assertEqual(45, controller._last_effective_cap)
        with self.assertRaises(ValueError):
            controller.process(2, 60, 50, maximum_power_percent=-1)
        self.assertEqual(45, controller._last_effective_cap)
        controller.reset()
        self.assertIsNone(controller._last_effective_cap)
        result = controller.process(2, 60, 50, maximum_power_percent=80)
        self.assertEqual(1, result.diagnostics['O'])
        self.assertEqual(80, controller._last_effective_cap)

    def test_original_actual_bounds_remain_enforced(self):
        settings = replace(source_settings(), minimum_power_percent=10, maximum_power_percent=80)
        controller = LayerAdaptiveController(settings)
        with self.assertRaises(ValueError): controller.process(1, 65, 81, maximum_power_percent=100)
        self.assertEqual(80, controller.process(1, 65, 80, maximum_power_percent=100).power_percent)
