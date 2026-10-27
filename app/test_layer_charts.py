"""Read-only graph histories and plotting, without a device or Tk display."""

from dataclasses import replace
import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from adapter_contracts import MachineWindow, UiSnapshot
from layer_charts import LayerCharts, LayerHistory, _HoverIndex, _write_csv


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

    def test_current_target_is_visible_when_empty_and_outside_temperature_range(self):
        canvas = FakeCanvas()
        LayerCharts._draw(canvas, (), False, target=120)
        lines = [call for call in canvas.calls if call[0] == "line" and call[2].get("fill") == "#27845b"]
        self.assertEqual(1, len(lines))
        self.assertTrue(any("현재 목표 120" in call[2].get("text", "") for call in canvas.calls if call[0] == "text"))
        self.assertTrue(any("데이터가 없습니다" in call[2].get("text", "") for call in canvas.calls if call[0] == "text"))
        history = LayerHistory()
        history.record(dict(snapshot=frame("a", t_recoating_c=50)))
        canvas = FakeCanvas()
        LayerCharts._draw(canvas, history.points, False, target=120)
        line = next(call for call in canvas.calls if call[0] == "line" and call[2].get("fill") == "#27845b")
        self.assertGreaterEqual(line[1][1], 12)
        self.assertLessEqual(line[1][1], 132)

    def test_changing_effective_target_redraws_even_without_a_new_observation(self):
        view = Mock()
        view._target_temperature = None
        LayerCharts.set_target_temperature(view, 65)
        self.assertEqual(65.0, view._target_temperature)
        view._schedule_redraw.assert_called_once()
        LayerCharts.set_target_temperature(view, 65)
        view._schedule_redraw.assert_called_once()
        LayerCharts.set_target_temperature(view, 70)
        self.assertEqual(2, view._schedule_redraw.call_count)
        for invalid in (float("nan"), float("inf"), "70", 2001):
            with self.assertRaises(ValueError):
                LayerCharts.set_target_temperature(view, invalid)
        self.assertEqual(70.0, view._target_temperature)

    def test_hover_returns_exact_layer_value_for_temperature_and_saved_power(self):
        history = LayerHistory()
        history.record(dict(snapshot=frame("a", 0, t_recoating_c=64.25), applied={1: 57}))
        history.record(dict(snapshot=frame("b", 1, t_recoating_c=65.5, heater1_setting_percent=57)))
        temperature = LayerCharts._draw(FakeCanvas(), history.points, False, target=65)
        temp_point = next(point for point in temperature.points if "64.25" in point[2])
        self.assertEqual("1층\nT_recoating: 64.25 °C", temperature.nearest(temp_point[0], temp_point[1])[2])
        power = LayerCharts._draw(FakeCanvas(), history.points, True)
        saved = next(point for point in power.points if "저장값" in point[2])
        self.assertEqual("1층\nIR 저장값: 57.0%", power.nearest(saved[0], saved[1])[2])
        self.assertIsNone(power.nearest(-100, -100))

    def test_missing_power_is_not_a_hover_target_and_many_points_are_bounded(self):
        history = LayerHistory()
        history.record(dict(snapshot=frame("a", 0, heater1_setting_percent=None)))
        index = LayerCharts._draw(FakeCanvas(), history.points, True)
        self.assertEqual((), index.points)
        for layer in range(1, 2000):
            history.record(dict(snapshot=frame(str(layer), layer)))
        canvas = FakeCanvas()
        index = LayerCharts._draw(canvas, history.points, False)
        self.assertEqual(2000, len(index.points))
        markers = [call for call in canvas.calls if call[0] == "oval"]
        self.assertLessEqual(len(markers), 301)
        self.assertIsNotNone(index.nearest(index.points[1000][0], index.points[1000][1]))

    def test_overlapping_observed_and_saved_values_are_both_shown(self):
        history = LayerHistory()
        history.record(dict(snapshot=frame("a", 0, heater1_setting_percent=57), applied={1: 57}))
        index = LayerCharts._draw(FakeCanvas(), history.points, True)
        x, y, _ = index.points[0]
        label = index.nearest(x, y)[2]
        self.assertIn("1층", label)
        self.assertIn("IR 설정: 57.0%", label)
        self.assertIn("IR 저장값: 57.0%", label)


class TkHoverTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.geometry("940x300-10000-10000")
        self.graph = LayerCharts(self.root)
        self.graph.pack(fill="both", expand=True)
        self.graph.set_target_temperature(65)
        self.graph.record(dict(snapshot=frame("first", 0, t_recoating_c=64.25,
                                                   heater1_setting_percent=57), applied={1: 57}))
        self.graph.record(dict(snapshot=frame("second", 1, t_recoating_c=66,
                                                    heater1_setting_percent=58)))
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def test_recent_window_boundary(self):
        for layer in range(2, 100):
            self.graph.record(dict(snapshot=frame(str(layer), layer)))
        self.root.update()
        self.assertEqual(100, len(self.graph._hover_indexes[self.graph.temperature_canvas].points))
        self.assertTrue(self.graph._hover_indexes[self.graph.temperature_canvas].points[0][2].startswith("1층"))
        self.graph.record(dict(snapshot=frame("100", 100)))
        self.root.update()
        self.assertEqual(101, len(self.graph.history.points))
        self.assertEqual(100, len(self.graph._hover_indexes[self.graph.temperature_canvas].points))
        self.assertTrue(self.graph._hover_indexes[self.graph.temperature_canvas].points[0][2].startswith("2층"))

    def test_recent_100_and_live_full_window_preserve_all_received_layers(self):
        for layer in range(2, 2105):
            self.graph.record(dict(snapshot=frame(str(layer), layer,
                                                  t_recoating_c=60 + layer % 5),
                                   applied={1: 58}))
        self.root.update()
        self.assertEqual(2105, len(self.graph.history.points))
        recent = self.graph._hover_indexes[self.graph.temperature_canvas].points
        self.assertEqual(100, len(recent))
        self.assertTrue(recent[0][2].startswith("2006층"))
        self.assertTrue(recent[-1][2].startswith("2105층"))
        self.graph.show_all()
        self.root.update()
        window = self.graph._full_window
        full_temperature, full_power = self.graph._full_canvases
        full = self.graph._hover_indexes[full_temperature].points
        self.assertEqual(2105, len(full))
        self.assertTrue(full[0][2].startswith("1층"))
        self.assertTrue(full[-1][2].startswith("2105층"))
        self.assertTrue(any(item[2].startswith("1층") for item in self.graph._hover_indexes[full_power].points))
        self.graph.show_all()
        self.assertIs(window, self.graph._full_window)
        self.graph.record(dict(snapshot=frame("next", 2105)))
        self.root.update()
        self.assertEqual(2106, len(self.graph._hover_indexes[full_temperature].points))
        self.assertTrue(self.graph._hover_indexes[self.graph.temperature_canvas].points[0][2].startswith("2007층"))
        self.graph._close_all()
        self.root.update()
        self.assertEqual(2106, len(self.graph.history.points))
        self.graph.show_all()
        self.root.update()
        self.assertEqual(2106, len(self.graph._hover_indexes[self.graph._full_canvases[0]].points))

    def test_csv_saves_all_layers_and_distinguishes_settings_and_successful_saves(self):
        for layer in range(2, 205):
            self.graph.record(dict(snapshot=frame(str(layer), layer, coupled_output=False,
                                                  heater1_setting_percent=0,
                                                  heater2_setting_percent=44), applied={1: 12, 2: 13}))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "층별 결과.csv"
            with patch("layer_charts.filedialog.asksaveasfilename", return_value=str(path)), \
                 patch("layer_charts.messagebox.showinfo") as success:
                self.graph.export_csv()
            success.assert_called_once()
            self.assertEqual(b"\xef\xbb\xbf", path.read_bytes()[:3])
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(206, len(rows))
            self.assertEqual(["1", "64.25", "57.0", "", "57.0", "", "예"], rows[1])
            self.assertEqual(["205", "64.0", "0.0", "44.0", "12.0", "13.0", "아니오"], rows[-1])
            self.assertEqual(205, len(self.graph.history.points))

    def test_cancel_and_save_failure_do_not_delete_history_or_existing_file(self):
        with patch("layer_charts.filedialog.asksaveasfilename", return_value=""), \
             patch("layer_charts.messagebox.showinfo") as success:
            self.graph.export_csv()
            success.assert_not_called()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "existing.csv"
            path.write_bytes(b"original")
            with patch("layer_charts.os.replace", side_effect=OSError("blocked")):
                with self.assertRaises(OSError):
                    _write_csv(path, self.graph.history.points)
            self.assertEqual(b"original", path.read_bytes())
            self.assertEqual([path], list(Path(folder).iterdir()))
            with patch("layer_charts.filedialog.asksaveasfilename", return_value=str(path)), \
                 patch("layer_charts._write_csv", side_effect=OSError("blocked")), \
                 patch("layer_charts.messagebox.showerror") as error:
                self.graph.export_csv()
                error.assert_called_once()
            self.assertEqual(2, len(self.graph.history.points))

    def test_motion_and_leave_show_correct_temperature_and_overlapping_ir(self):
        for canvas, needle, expected in (
            (self.graph.temperature_canvas, "64.25", ("1층", "64.25 °C")),
            (self.graph.power_canvas, "IR 저장값", ("1층", "IR 설정: 57.0%", "IR 저장값: 57.0%")),
        ):
            index = self.graph._hover_indexes[canvas]
            x, y, _ = next(point for point in index.points if needle in point[2])
            canvas.event_generate("<Motion>", x=int(round(x)), y=int(round(y)))
            self.root.update()
            items = canvas.find_withtag("hover-text")
            self.assertEqual(1, len(items))
            tooltip = canvas.itemcget(items[0], "text")
            for value in expected:
                self.assertIn(value, tooltip)
            canvas.event_generate("<Leave>")
            self.root.update()
            self.assertEqual((), canvas.find_withtag("hover-tooltip"))

    def test_resize_rebuilds_hover_coordinates(self):
        canvas = self.graph.temperature_canvas
        x_before = self.graph._hover_indexes[canvas].points[-1][0]
        self.root.geometry("1040x300-10000-10000")
        self.root.update()
        x_after, y_after, _ = self.graph._hover_indexes[canvas].points[-1]
        self.assertNotEqual(x_before, x_after)
        canvas.event_generate("<Motion>", x=int(round(x_after)), y=int(round(y_after)))
        self.root.update()
        items = canvas.find_withtag("hover-text")
        self.assertEqual(1, len(items))
        self.assertIn("2층", canvas.itemcget(items[0], "text"))


if __name__ == "__main__":
    unittest.main()
