import unittest
import json
from pathlib import Path
from dataclasses import replace

from image_guard import ImageGuardSettings
from layer_algorithm import LayerAdaptiveController
from test_layer_algorithm import source_settings


def feature(current=1, previous=1, center=1):
    return {'image_valid': int(current is not None), 'whole_coverage_pct': current,
            'center_coverage_pct': center if current is not None else None,
            'center_intensity_pct': center if current is not None else None,
            'previous_image_valid': int(previous is not None), 'previous_whole_coverage_pct': previous,
            'previous_center_coverage_pct': previous, 'previous_center_intensity_pct': previous,
            'previous_powder_layers': 0, 'radius_mm': 1, 'dpi_x': 400, 'dpi_y': 400}


class ImageGuardTests(unittest.TestCase):
    def controller(self):
        return LayerAdaptiveController(replace(source_settings(), target_temperature_c=100, wait_layers=0), ImageGuardSettings(absolute_recovery=False))

    def shock(self, controller, previous=1, current=1):
        actual = 50
        for layer in range(1, 4):
            actual = controller.process(layer, 90, actual, slice_features=feature()).power_percent
        return controller.process(4, 85, actual, slice_features=feature(current, previous))

    def test_previous_ink_is_required_and_outer_print_counts(self):
        for previous in (0, None):
            c = self.controller()
            r = self.shock(c, previous)
            self.assertEqual(0, r.diagnostics['CH'])
            self.assertEqual(r.diagnostics['CQ'], r.power_percent)
        c = self.controller()
        r = self.shock(c)
        self.assertEqual((1, 1, 0), (r.diagnostics['CH'], r.diagnostics['CI'], r.diagnostics['E']))
        self.assertGreater(r.diagnostics['CQ'], r.power_percent)
        c = self.controller()
        actual = 50
        for layer in range(1, 4):
            actual = c.process(layer, 90, actual, slice_features=feature(center=0)).power_percent
        r = c.process(4, 85, actual, slice_features=feature(center=0))
        self.assertEqual(1, r.diagnostics['CH'])

    def test_inclusive_window_timeout_missing_and_no_retrigger(self):
        c = self.controller(); r = self.shock(c); actual = r.power_percent
        for age in range(2, 25):
            r = c.process(age + 3, 85, actual, slice_features=feature(None, None) if age == 5 else feature())
            actual = r.power_percent
            self.assertEqual(age, r.diagnostics['CI'])
            self.assertEqual(1 if age == 2 else 2, r.diagnostics['CL'])
            self.assertLessEqual(r.diagnostics['E'], 0 if age == 2 else 1)
        r = c.process(28, 85, actual, slice_features=feature())
        self.assertEqual((0, 1, 0), (r.diagnostics['CI'], r.diagnostics['CM'], r.diagnostics['CJ']))
        self.assertGreater(r.diagnostics['E'], 1)

    def test_negative_corrections_and_saturation(self):
        c = self.controller(); r = self.shock(c)
        r = c.process(5, 120, r.power_percent, slice_features=feature())
        self.assertEqual(r.diagnostics['CQ'], r.power_percent)
        self.assertLessEqual(r.diagnostics['E'], 0)
        c = self.controller()
        r = c.process(1, 80, 100, slice_features=feature())
        self.assertEqual(100, r.power_percent)

    def test_invalid_does_not_advance_and_resets(self):
        c = self.controller(); r = self.shock(c); actual = r.power_percent
        with self.assertRaises(ValueError): c.process(5, -1, actual, slice_features=feature())
        with self.assertRaises(ValueError): c.process(5, 85, actual, slice_features=feature(101))
        self.assertEqual(2, c.process(5, 85, actual, slice_features=feature()).diagnostics['CI'])
        r = c.process(7, 85, actual, slice_features=feature())
        self.assertEqual((1, 0), (r.diagnostics['O'], r.diagnostics['CI']))
        c.set_target_temperature(101)
        r = c.process(8, 85, r.power_percent, slice_features=feature())
        self.assertEqual((2, 1), (r.diagnostics['O'], r.diagnostics['DA']))
        r = c.process(9, 85, r.power_percent + 1, slice_features=feature())
        self.assertEqual((3, 1), (r.diagnostics['O'], r.diagnostics['DA']))

    def test_learning_is_cancelled_then_resumes_after_clean_tail(self):
        c = self.controller(); r = self.shock(c); actual = r.power_percent
        self.assertEqual((1, None, 0, 0), (r.diagnostics['CP'], r.diagnostics['X'], r.diagnostics['AM'], r.diagnostics['AV']))
        initial = tuple(r.diagnostics[k] for k in ('L', 'J', 'K'))
        for layer in range(5, 29):
            r = c.process(layer, 85, actual, slice_features=feature()); actual = r.power_percent
            self.assertEqual(initial, tuple(r.diagnostics[k] for k in ('L', 'J', 'K')))
        for layer in range(29, 33):
            r = c.process(layer, 85, actual, slice_features=feature()); actual = r.power_percent
        self.assertEqual(0, r.diagnostics['CP'])

    def test_missing_payload_keeps_episode_and_recovery_rearms(self):
        c = self.controller(); r = self.shock(c)
        r = c.process(5, 85, r.power_percent, slice_features=None)
        self.assertEqual((1, -1, -1, 2), tuple(r.diagnostics[k] for k in ('CD', 'CE', 'CF', 'CI')))
        self.assertTrue(r.reason.startswith('증출력 보류'))
        actual = r.power_percent
        for layer in (6, 7, 8, 9):
            r = c.process(layer, 100, actual, slice_features=feature()); actual = r.power_percent
        self.assertEqual((0, 1, 0), tuple(r.diagnostics[k] for k in ('CI', 'CJ', 'CL')))

    def test_malformed_features_rejected_without_advancing(self):
        c = self.controller(); r = self.shock(c)
        bad = feature(); bad['center_intensity_pct'] = float('nan')
        with self.assertRaises(ValueError): c.process(5, 85, r.power_percent, slice_features=bad)
        self.assertEqual(2, c.process(5, 85, r.power_percent, slice_features=feature()).diagnostics['CI'])

    def test_unknown_from_start_preserves_all_legacy_fixture_fields(self):
        fixture = json.loads((Path(__file__).parent / 'random80_validation.json').read_text())
        baseline, unknown = LayerAdaptiveController(source_settings()), LayerAdaptiveController(source_settings())
        for item in fixture['rows']:
            args = item['A'], item['B'], item['C']
            a = baseline.process(*args).diagnostics
            b = unknown.process(*args, slice_features=feature(None, None)).diagnostics
            self.assertEqual(0, b['CD']); self.assertEqual(0, b['CP'])
            for col in fixture['columns']:
                self.assertEqual(a[col], b[col], col)
        baseline.set_target_temperature(66); unknown.set_target_temperature(66)
        a = baseline.process(81, 64, 22).diagnostics
        b = unknown.process(81, 64, 22, slice_features=feature(None, None)).diagnostics
        for col in fixture['columns']: self.assertEqual(a[col], b[col], col)

    def test_reference_uses_maximum_and_rejects_unstable_or_outlier(self):
        c = self.controller(); actual = 50
        for layer, temp in enumerate((89.827, 90.0, 90.173), 1):
            actual = c.process(layer, temp, actual, slice_features=feature()).power_percent
        r = c.process(4, 86.048, actual, slice_features=feature())
        self.assertAlmostEqual(4.125, r.diagnostics['CV']); self.assertEqual(1, r.diagnostics['CH'])
        c = self.controller(); actual = 50
        for layer, temp in enumerate((90, 92, 90), 1):
            actual = c.process(layer, temp, actual, slice_features=feature()).power_percent
        r = c.process(4, 84, actual, slice_features=feature())
        self.assertEqual((0, 3), (r.diagnostics['CH'], r.diagnostics['CX']))
        c = self.controller(); actual = 50
        for layer, temp in enumerate((90, 90.1, 90), 1):
            actual = c.process(layer, temp, actual, slice_features=feature()).power_percent
        r = c.process(4, 89.9, actual, slice_features=feature())
        self.assertEqual((0, 4), (r.diagnostics['CH'], r.diagnostics['CX']))

    def test_missing_availability_quarantines_boundary_then_clean_tail(self):
        c = self.controller(); r = self.shock(c); actual = r.power_percent
        for layer in range(5, 28):
            r = c.process(layer, 85, actual, slice_features=feature()); actual = r.power_percent
        r = c.process(28, 85, actual, slice_features=None)
        self.assertEqual((1, 1, 1), (r.diagnostics['CD'], r.diagnostics['CN'], r.diagnostics['CY']))
        actual = r.power_percent
        for layer in range(29, 32):
            r = c.process(layer, 85, actual, slice_features=None); actual = r.power_percent
        self.assertEqual((1, 0, 0, 1), (r.diagnostics['CD'], r.diagnostics['CN'], r.diagnostics['CP'], r.diagnostics['CY']))

    def test_valid_printing_before_trigger_matches_legacy(self):
        for filename in ('random80_validation.json', 'input_example_validation.json'):
            fixture = json.loads((Path(__file__).parent / filename).read_text())
            baseline, observed = LayerAdaptiveController(source_settings()), LayerAdaptiveController(source_settings())
            for item in fixture['rows']:
                args = item['A'], item['B'], item['C']
                restart = item.get('reset', False)
                a = baseline.process(*args, restart=restart).diagnostics
                b = observed.process(*args, restart=restart, slice_features=feature()).diagnostics
                for col in item.keys() - {'reset'}: self.assertEqual(a[col], b[col], (filename, item['A'], col))
                self.assertEqual((0, 0, 0), (b['CH'], b['CY'], b['CP']))
            baseline.set_target_temperature(66); observed.set_target_temperature(66)
            args = (fixture['rows'][-1]['A'] + 1, 65, 22)
            a = baseline.process(*args).diagnostics
            b = observed.process(*args, slice_features=feature()).diagnostics
            for col in fixture['rows'][-1].keys() - {'reset'}: self.assertEqual(a[col], b[col], col)
            self.assertEqual(1, b['DA'])

    def test_optional_print_learning_exclusion_only_after_event(self):
        g = ImageGuardSettings(exclude_print_learning_after_event=True, absolute_recovery=False)
        c = LayerAdaptiveController(replace(source_settings(), target_temperature_c=100, wait_layers=0), g)
        r = self.shock(c); self.assertEqual((1, 1, 1), (r.diagnostics['CY'], r.diagnostics['CZ'], r.diagnostics['CP']))
        actual = r.power_percent; learned = tuple(r.diagnostics[k] for k in ('L', 'J', 'K'))
        for layer in range(5, 36):
            r = c.process(layer, 85, actual, slice_features=feature()); actual = r.power_percent
        self.assertEqual((0, 1, 1, 1), tuple(r.diagnostics[k] for k in ('CL', 'CY', 'CZ', 'CP')))
        self.assertEqual(learned, tuple(r.diagnostics[k] for k in ('L', 'J', 'K')))
        self.assertEqual(r.diagnostics['CQ'], r.power_percent)
        for layer in range(36, 40):
            r = c.process(layer, 85, actual, slice_features=feature(0, 0)); actual = r.power_percent
        self.assertEqual((0, 0), (r.diagnostics['CZ'], r.diagnostics['CP']))

    def test_seen_survives_manual_reset_but_not_gap(self):
        c = self.controller(); r = self.shock(c)
        r = c.process(5, 85, r.power_percent + 1, slice_features=feature())
        self.assertEqual((1, 1, 1), (r.diagnostics['O'], r.diagnostics['CY'], r.diagnostics['DA']))
        r = c.process(7, 85, r.power_percent, slice_features=feature())
        self.assertEqual((1, 0, 1), (r.diagnostics['O'], r.diagnostics['CY'], r.diagnostics['DA']))

    def test_disabled_matches_legacy_with_valid_images(self):
        disabled = LayerAdaptiveController(source_settings(), ImageGuardSettings(enabled=False))
        plain = LayerAdaptiveController(source_settings())
        for layer, temp in enumerate((65, 65, 65, 60, 60), 1):
            a = plain.process(layer, temp, 50).diagnostics
            b = disabled.process(layer, temp, 50, slice_features=feature()).diagnostics
            for key in a: self.assertEqual(a[key], b[key], key)

    def test_absolute_fixed_reference_and_entry_gain(self):
        c = LayerAdaptiveController(replace(source_settings(), target_temperature_c=100, wait_layers=0))
        r = self.shock(c)
        self.assertEqual(1, r.diagnostics['CH'])
        reference, gain = r.diagnostics['DC'], r.diagnostics['DD']
        self.assertEqual(50, reference)
        self.assertAlmostEqual(min(2, c._history[-2]['K'] / c._history[-2]['L']), gain)
        actual = r.power_percent
        for layer in range(5, 16):
            r = c.process(layer, 85, actual, slice_features=feature()); actual = r.power_percent
            self.assertEqual((reference, gain), (r.diagnostics['DC'], r.diagnostics['DD']))
            self.assertLessEqual(r.diagnostics['E'], 0 if layer == 5 else 1)
            self.assertGreaterEqual(r.diagnostics['E'], -2)

    def test_absolute_integral_antiwindup_and_hot_unwind(self):
        c = LayerAdaptiveController(replace(source_settings(), target_temperature_c=100, wait_layers=0))
        r = self.shock(c); actual = r.power_percent
        for layer in range(5, 15):
            r = c.process(layer, 70, actual, slice_features=feature()); actual = r.power_percent
        self.assertEqual(0, r.diagnostics['DR'])
        self.assertEqual(r.diagnostics['DI'], r.diagnostics['DJ'])
        c._history[-1]['DJ'] = 10
        r = c.process(15, 110, actual, slice_features=feature())
        self.assertLessEqual(r.diagnostics['E'], 0)
        self.assertLess(r.diagnostics['DJ'], 10)
        self.assertEqual(0, r.diagnostics['DR'])

    def test_absolute_integrates_after_sustained_unsaturated_deficit(self):
        c = LayerAdaptiveController(replace(source_settings(), target_temperature_c=100, wait_layers=0))
        r = self.shock(c); actual = r.power_percent
        for layer in range(5, 16):
            r = c.process(layer, 85, actual, slice_features=feature()); actual = r.power_percent
        self.assertEqual((12, 1, 0), (r.diagnostics['CI'], r.diagnostics['DR'], r.diagnostics['DI']))
        self.assertGreater(r.diagnostics['DJ'], 0)
        r = c.process(16, 85, actual, slice_features=feature())
        self.assertGreater(r.diagnostics['E'], 0)
        self.assertLess(r.diagnostics['E'], 1)
        self.assertAlmostEqual(r.diagnostics['DI'], c._history[-2]['DJ'])

    def test_absolute_full_timeout_handoff_and_missing(self):
        c = LayerAdaptiveController(replace(source_settings(), target_temperature_c=100, wait_layers=0))
        r = self.shock(c); actual = r.power_percent
        for age in range(2, 133):
            r = c.process(age + 3, 85, actual, slice_features=None); actual = r.power_percent
            self.assertEqual(age, r.diagnostics['CI'])
            self.assertEqual(1 if age <= 2 else 2 if age <= 120 else 3, r.diagnostics['CL'])
            if age == 132:
                self.assertEqual(1, r.diagnostics['DL'])
                self.assertAlmostEqual(r.diagnostics['CQ'], r.power_percent)
        saved = r.diagnostics['DJ']
        r = c.process(136, 85, actual, slice_features=None)
        self.assertEqual((0, 1), (r.diagnostics['CI'], r.diagnostics['CM']))
        self.assertEqual(r.diagnostics['CQ'], r.power_percent)
        self.assertEqual(saved, r.diagnostics['DJ'])
        actual = r.power_percent
        for layer in range(137, 170):
            r = c.process(layer, 100, actual, slice_features=None); actual = r.power_percent
            if r.diagnostics['CJ'] == 1: break
        self.assertEqual(1, r.diagnostics['CJ'])

    def test_absolute_stable_exit_requires_power_and_temperature(self):
        c = LayerAdaptiveController(replace(source_settings(), target_temperature_c=100, wait_layers=0))
        r = self.shock(c); actual = r.power_percent
        for layer in range(5, 18):
            r = c.process(layer, 100, actual, slice_features=feature()); actual = r.power_percent
            if r.diagnostics['CL'] == 0: break
        self.assertEqual((0, 1, 5), (r.diagnostics['CL'], r.diagnostics['CJ'], r.diagnostics['DP']))

    def test_absolute_obeys_configured_maximum_and_handoff(self):
        settings = replace(source_settings(), target_temperature_c=100, wait_layers=0, maximum_step_percent=1)
        c = LayerAdaptiveController(settings, ImageGuardSettings(positive_limit_pp=5, negative_limit_pp=2))
        r = self.shock(c); actual = r.power_percent
        for age in range(2, 133):
            r = c.process(age + 3, 85 if age != 5 else 120, actual, slice_features=feature())
            self.assertLessEqual(abs(r.power_percent - actual), 1 + 1e-9)
            actual = r.power_percent
        self.assertEqual(1, r.diagnostics['DL'])
        self.assertAlmostEqual(r.diagnostics['CQ'], r.power_percent)
        self.assertIn('절대값 회복', r.reason)
        self.assertIn(f"{r.power_percent:.2f}", r.reason)

    def test_absolute_invalid_entry_gain_does_not_advance(self):
        for key, value in (('K', 0), ('L', 0), ('K', float('nan')), ('L', float('inf'))):
            c = LayerAdaptiveController(replace(source_settings(), target_temperature_c=100, wait_layers=0))
            actual = 50
            for layer in range(1, 4):
                actual = c.process(layer, 90, actual, slice_features=feature()).power_percent
            c._history[-1][key] = value
            with self.assertRaises(ValueError): c.process(4, 85, actual, slice_features=feature())
            self.assertEqual(3, c._history[-1]['A'])

    def test_settings_validation(self):
        for kwargs in ({'hold_observations': 25}, {'positive_limit_pp': 6}, {'drop_c': float('nan')}, {'absolute_recovery': 1}, {'negative_limit_pp': 6}, {'integral_time_layers': 0}, {'residual_after_observations': 121}):
            with self.assertRaises(ValueError): ImageGuardSettings(**kwargs)

if __name__ == '__main__': unittest.main()
