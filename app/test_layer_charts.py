"""Read-only graph histories and plotting, without a device or Tk display."""

from dataclasses import replace
import unittest

from adapter_contracts import MachineWindow, UiSnapshot
from layer_charts import LayerCharts, LayerHistory


TARGET = MachineWindow("M1", 123, 456, "fixture", "C:/fixture.exe")


def frame(key, layer=0, **values):
    defaults = dict(layer_index=layer, observation_key=key, run_key="run-a",
                    t_recoating_c=64, heater1_setting_percent=52,
                    heater2_setting_percent=52, coupled_output=True)
    defaults.update(values)
    return UiSnapshot(TARGET, **defaults)


class FakeCanvas:
    def __init__(self):
        self.calls = []

    def winfo_width(self):
        return 600

    def winfo_height(self):
        return 160

    def delete(self, *args, **kwargs):
        self.calls.append(("delete", args, kwargs))

    def create_text(self, *args, **kwargs):
        self.calls.append(("text", args, kwargs))

    def create_line(self, *args, **kwargs):
        self.calls.append(("line", args, kwargs))

    def create_oval(self, *args, **kwargs):
        self.calls.append(("oval", args, kwargs))


class HistoryTests(unittest.TestCase):
    def test_record_first_value_once_per_observation_and_never_infer_save(self):
        history = LayerHistory()
        first = frame("a")
        self.assertTrue(history.record(dict(snapshot=first, proposed={1: 70})))
        self.assertFalse(history.record(dict(snapshot=replace(first, heater1_setting_percent=70), proposed={1: 70})))
        point = history.points[0]
        self.assertEqual((1, 64, 52, None), (point.layer, point.temperature, point.ir1_observed, point.ir1_saved))
        self.assertIsNone(point.ir2_observed)

    def test_only_explicit_successful_save_is_a_marker(self):
        history = LayerHistory()
        history.record(dict(snapshot=frame("a", coupled_output=False, heater2_setting_percent=40),
                            applied={1: 55, 2: 45}, proposed={1: 60}))
        point = history.points[0]
        self.assertEqual((52, 40, 55, 45), (point.ir1_observed, point.ir2_observed, point.ir1_saved, point.ir2_saved))

    def test_new_run_and_reset_clear_prior_run_data(self):
        history = LayerHistory()
        history.record(dict(snapshot=frame("a", 7)))
        history.record(dict(snapshot=frame("b", 0, run_key="run-b")))
        self.assertEqual([1], [point.layer for point in history.points])
        history.reset()
        self.assertEqual((), history.points)

    def test_bounded_history_and_sorted_layers_with_correction(self):
        history = LayerHistory(capacity=2)
        history.record(dict(snapshot=frame("a", 1)))
        history.record(dict(snapshot=frame("b", 2)))
        history.record(dict(snapshot=frame("corrected", 1, t_recoating_c=62)))
        self.assertEqual([(2, 62), (3, 64)], [(point.layer, point.temperature) for point in history.points])
        history.record(dict(snapshot=frame("c", 3)))
        self.assertEqual([2, 4], [point.layer for point in history.points])

    def test_invalid_inputs_and_unavailable_ir_are_not_misrepresented(self):
        history = LayerHistory()
        for snapshot in (frame(None), frame("a", -1), frame("a", t_recoating_c=float("nan")),
                         frame("a", t_recoating_c=1e300)):
            self.assertFalse(history.record(dict(snapshot=snapshot)))
        self.assertEqual((), history.points)
        history.record(dict(snapshot=frame("b", heater1_setting_percent=101), applied={1: float("inf")}))
        self.assertIsNone(history.points[0].ir1_observed)
        self.assertIsNone(history.points[0].ir1_saved)


class PlotTests(unittest.TestCase):
    def test_empty_and_populated_temperature_plot(self):
        canvas = FakeCanvas()
        LayerCharts._draw(canvas, (), False)
        self.assertTrue(any(call[0] == "text" and "데이터가 없습니다" in call[2].get("text", "") for call in canvas.calls))
        history = LayerHistory()
        history.record(dict(snapshot=frame("a", 0)))
        history.record(dict(snapshot=frame("b", 1, t_recoating_c=65)))
        canvas = FakeCanvas()
        LayerCharts._draw(canvas, history.points, False)
        self.assertTrue(any(call[0] == "line" and call[2].get("fill") == "#df6854" for call in canvas.calls))

    def test_power_plot_only_marks_explicit_save(self):
        history = LayerHistory()
        history.record(dict(snapshot=frame("a", 0), proposed={1: 70}))
        history.record(dict(snapshot=frame("b", 1), applied={1: 57}))
        canvas = FakeCanvas()
        LayerCharts._draw(canvas, history.points, True)
        markers = [call for call in canvas.calls if call[0] == "oval" and call[2].get("fill") == "#ef8b26"]
        self.assertEqual(1, len(markers))


if __name__ == "__main__":
    unittest.main()
