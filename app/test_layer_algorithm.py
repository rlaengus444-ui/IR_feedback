"""Replay every numeric source-workbook cell; no Excel or hardware required."""

from dataclasses import replace
import json
from pathlib import Path
import unittest

from layer_algorithm import LayerAdaptiveController, LayerSettings, WORKBOOK_SHA256


HERE = Path(__file__).resolve().parent


def source_settings():
    return LayerSettings.from_dict(json.loads((HERE / "workbook-replay.settings.json").read_text(encoding="utf-8")))


class LayerAlgorithmTests(unittest.TestCase):
    def test_all_80_source_rows_and_every_numeric_helper_column(self):
        fixture = json.loads((HERE / "random80_validation.json").read_text(encoding="utf-8"))
        self.assertEqual(WORKBOOK_SHA256, fixture["sha256"])
        self.assertEqual(80, len(fixture["rows"]))
        controller = LayerAdaptiveController(source_settings())
        checks = 0
        for expected in fixture["rows"]:
            actual = controller.process(expected["A"], expected["B"], expected["C"]).diagnostics
            for column, value in expected.items():
                with self.subTest(layer=expected["A"], column=column):
                    if value is None:
                        self.assertIsNone(actual[column])
                    else:
                        self.assertAlmostEqual(value, actual[column], delta=max(1e-9, abs(value) * 1e-10))
                checks += 1
        self.assertEqual(80 * len(fixture["columns"]), checks)

    def test_first_two_layers_and_explicit_reset_hold_actual_setting(self):
        controller = LayerAdaptiveController(source_settings())
        self.assertEqual(50, controller.process(1, 80, 50).power_percent)
        self.assertEqual(50, controller.process(2, 80, 50).power_percent)
        self.assertEqual(45, controller.process(3, 80, 50).power_percent)
        reset = controller.process(4, 80, 45, restart=True)
        self.assertEqual(45, reset.power_percent)
        self.assertEqual(1, reset.diagnostics["O"])
        self.assertEqual(0.5, reset.diagnostics["L"])

    def test_67_input_examples_cover_alpha_switch_and_source_resets(self):
        fixture = json.loads((HERE / "input_example_validation.json").read_text(encoding="utf-8"))
        controller = LayerAdaptiveController(source_settings())
        self.assertEqual(67, len(fixture["rows"]))
        alphas = set()
        resets = 0
        for expected in fixture["rows"]:
            result = controller.process(expected["A"], expected["B"], expected["C"], restart=expected["reset"])
            alphas.add(result.diagnostics["J"])
            resets += expected["reset"]
            for column, value in expected.items():
                if column == "reset":
                    continue
                with self.subTest(layer=expected["A"], column=column):
                    if value is None:
                        self.assertIsNone(result.diagnostics[column])
                    else:
                        self.assertAlmostEqual(value, result.diagnostics[column], delta=max(1e-9, abs(value) * 1e-10))
        self.assertEqual({0.2, 0.3}, alphas)
        self.assertEqual(2, resets)

    def test_missing_layer_resets_and_actual_applied_power_drives_wait(self):
        controller = LayerAdaptiveController(source_settings())
        for layer in (1, 2, 3):
            controller.process(layer, 80, 50)
        # Recommendation was 45 but the user actually held 50: no invented change.
        held = controller.process(4, 80, 50)
        self.assertEqual(0, held.diagnostics["P"])
        self.assertEqual(0, held.diagnostics["Q"])
        changed = controller.process(5, 79, 48)
        self.assertEqual(-2, changed.diagnostics["P"])
        self.assertEqual(2, changed.diagnostics["Q"])
        self.assertEqual(48, changed.power_percent)
        missing = controller.process(7, 80, 48)
        self.assertEqual(1, missing.diagnostics["O"])

    def test_settings_and_nonfinite_inputs_fail_before_advancing_history(self):
        settings = source_settings()
        for parameters in ({"maximum_step_percent": 6}, {"initial_g": 0},
                           {"target_temperature_c": float("nan")}, {"auto_alpha": "true"}):
            with self.assertRaises(ValueError):
                replace(settings, **parameters)
        controller = LayerAdaptiveController(settings)
        for temperature, power in ((float("nan"), 50), (65, 101)):
            with self.assertRaises(ValueError):
                controller.process(1, temperature, power)
        self.assertEqual(1, controller.process(1, 65, 50).diagnostics["O"])

    def test_beta_completed_block_crossing_reduces_and_slow_approach_increases(self):
        # Golden workbooks never change beta. Seed the boundary state of an
        # already eligible seven-observation block to exercise both decisions.
        for temperature, direction in ((66, -1), (62, 1)):
            for initial_beta in (0.2, 0.5, 0.8):
                settings = replace(source_settings(), initial_beta=initial_beta)
                controller = LayerAdaptiveController(settings)
                for layer in (1, 2, 3):
                    controller.process(layer, 65, 70)
                controller._history[-1].update(AR=3, AW=7, AY=10, AZ=8, BA=2,
                    BB=1, BC=0, BD=1, BE=0, BF=0, AU=0, D=70, V=0)
                row = controller.process(4, temperature, 70).diagnostics
                with self.subTest(temperature=temperature, beta=initial_beta):
                    self.assertEqual(1, row["BJ"])
                    self.assertEqual(direction, row["BG"])
                    self.assertAlmostEqual(max(0.2, min(0.8, initial_beta + direction * 0.05)), row["K"])
                    self.assertEqual(3 if row["K"] != initial_beta else 0, row["AU"])


if __name__ == "__main__":
    unittest.main()
