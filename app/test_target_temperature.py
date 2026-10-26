"""Target setting validation and its effect on feedback, without hardware."""

import threading
import unittest
from unittest.mock import Mock

from adapter_contracts import MachineWindow, UiAdapter, UiSnapshot
from session import SessionOptions, run_ui_session
from target_temperature import parse_target_temperature, validate_target_temperature


TARGET = MachineWindow("M1", 123, 456, "test", "C:/test/BinderJet.exe")


class TargetTemperatureTests(unittest.TestCase):
    def test_parse_valid_targets_with_decimal_and_scientific_notation(self):
        for text, expected in (("65", 65.0), (" 67.5 ", 67.5),
                               ("1e2", 100.0), ("-273.15", -273.15),
                               ("2000", 2000.0)):
            with self.subTest(text=text):
                self.assertEqual(expected, parse_target_temperature(text))

    def test_invalid_targets_never_reach_session(self):
        for invalid in ("", "abc", "NaN", "Infinity", "-Infinity", "1e999",
                        "-273.16", "2000.01", "50 °C"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    parse_target_temperature(invalid)
        for invalid in (True, None, float("nan"), float("inf"), -274, 2001, 10**1000):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    validate_target_temperature(invalid)
                with self.assertRaises(ValueError):
                    SessionOptions(TARGET, target_temperature_c=invalid)

    def test_session_uses_selected_target_for_opposite_recommendations(self):
        for target_temperature, expected in ((70, 45), (90, 55)):
            with self.subTest(target_temperature=target_temperature):
                adapter = Mock(spec=UiAdapter)
                adapter.read_snapshot.side_effect = [
                    UiSnapshot(TARGET, layer_index=layer, t_recoating_c=80,
                        observation_key=f"layer-{layer}", heater1_setting_percent=50,
                        heater2_setting_percent=50, machine_state="AUTO",
                        minimum_poll_seconds=0.1, observation_is_new=True)
                    for layer in range(3)
                ]
                stop = threading.Event()
                proposals = []

                def emit(kind, payload):
                    if kind == "snapshot":
                        proposals.append(payload["proposed"])
                        if len(proposals) == 3:
                            stop.set()

                options = SessionOptions(TARGET, mode="preview",
                    target_temperature_c=parse_target_temperature(str(target_temperature)))
                run_ui_session(options, stop, emit, lambda: adapter)
                self.assertEqual([{1: 50}, {1: 50}, {1: expected}], proposals)
                adapter.apply_ir_power.assert_not_called()
                adapter.disconnect.assert_called_once()


if __name__ == "__main__":
    unittest.main()
