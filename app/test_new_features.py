"""Feature integration tests use only fake adapters and an invisible local view."""

import threading
import unittest
from unittest.mock import Mock, patch

from adapter_contracts import ControlUnavailable, UiAdapter
from excel_workbook import WorkbookSpec
from layer_algorithm import LayerRecommendation
from session import SessionController, SessionOptions, run_ui_session
from test_session import DeferredThread, TARGET, snapshot


SPEC = WorkbookSpec("sample.xlsx", "", "B13", "C13", "D13", "B5", "A13")


class ExcelSessionTests(unittest.TestCase):
    def test_excel_controller_is_used_once_for_each_new_row_and_closed(self):
        adapter = Mock(spec=UiAdapter)
        adapter.read_snapshot.side_effect = [snapshot("old", 0, control_available=True),
            snapshot("new", 1, control_available=True),
            snapshot("new", 1, control_available=True, heater1_setting_percent=55, heater2_setting_percent=55)]
        controller = Mock()
        controller.process.return_value = LayerRecommendation(55, "Excel test", {})
        stop = threading.Event()
        rows = []
        options = SessionOptions(TARGET, mode="control", workbook_spec=SPEC)
        def emit(kind, payload):
            if kind == "snapshot":
                rows.append(payload)
                if len(rows) == 1:
                    options._target_temperature_setting.set(70)
                if len(rows) == 3:
                    stop.set()
        with patch("session.WorkbookController", return_value=controller) as factory:
            run_ui_session(options, stop, emit, lambda: adapter)
        factory.assert_called_once_with(SPEC, 65.0, stop_event=stop)
        controller.set_target_temperature.assert_called_once_with(70)
        controller.process.assert_called_once_with(2, 80, 50)
        adapter.apply_ir_power.assert_called_once_with(1, 55, "new", stop)
        self.assertEqual([{}, {1: 55}, {}], [row["applied"] for row in rows])
        controller.close.assert_called_once()

    def test_invalid_workbook_recommendation_is_never_saved(self):
        adapter = Mock(spec=UiAdapter)
        adapter.read_snapshot.return_value = snapshot("new", 1, control_available=True, observation_is_new=True)
        controller = Mock()
        controller.process.return_value = LayerRecommendation(101, "bad", {})
        with patch("session.WorkbookController", return_value=controller):
            with self.assertRaises(ControlUnavailable):
                run_ui_session(SessionOptions(TARGET, mode="control", workbook_spec=SPEC),
                               threading.Event(), lambda *args: None, lambda: adapter)
        adapter.apply_ir_power.assert_not_called()
        controller.close.assert_called_once()

    def test_invalid_excel_stops_before_connecting_to_target(self):
        factory = Mock()
        with patch("session.WorkbookController", side_effect=ValueError("invalid workbook")):
            with self.assertRaisesRegex(ValueError, "invalid workbook"):
                run_ui_session(SessionOptions(TARGET, mode="control", workbook_spec=SPEC),
                               threading.Event(), lambda *args: None, factory)
        factory.assert_not_called()


class FeatureViewTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from standalone_ir import ThermalFeedbackWindow
        self.root = tk.Tk()
        self.root.withdraw()
        self.controller = SessionController(lambda *args: None, DeferredThread)
        self.view = ThermalFeedbackWindow(self.root, controller=self.controller,
                                          discoverer=lambda: [], auto_discover=False)
        self.view.targets = [TARGET]
        self.view._populate_hosts()

    def tearDown(self):
        self.root.destroy()

    def test_excel_cells_pass_to_session_and_target_can_change_while_connected(self):
        self.view.algorithm_type.set("Excel 수식 파일")
        self.view._algorithm_changed()
        self.view.settings_path.set("sample.xlsx")
        self.view.mode.set("계산 미리보기")
        self.view.workbook_result_cell.set("D13")
        self.view.connect()
        self.assertEqual("normal", str(self.view.target_apply_button["state"]))
        self.view.target_temperature.set("72.5")
        self.view.apply_target_temperature()
        self.assertEqual(72.5, self.controller.target_temperature_setting.get())
        self.assertIn("다음 새 층", self.view.log.get("1.0", "end"))

    def test_default_session_uses_existing_algorithm_without_excel(self):
        self.assertEqual("기본 알고리즘", self.view.algorithm_type.get())
        self.view.mode.set("계산 미리보기")
        runner = Mock()
        self.controller.runner = runner
        self.view.connect()
        self.controller.thread.complete()
        self.assertIsNone(runner.call_args.args[0].workbook_spec)

    def test_view_records_completed_layer_graph_without_applying(self):
        self.view._show_snapshot(dict(snapshot=snapshot("layer", 4), effective_mode="preview",
                                      proposed={1: 55}, reasons={}, applied={}, status="계산"))
        point = self.view.charts.history.points[0]
        self.assertEqual((5, 80, 50, None), (point.layer, point.temperature, point.ir1_observed, point.ir1_saved))

    def test_invalid_target_does_not_update_worker(self):
        self.view.connect()
        self.view.target_temperature.set("NaN")
        self.view.apply_target_temperature()
        self.assertEqual(65, self.controller.target_temperature_setting.get())
        self.assertIn("유효한 숫자", self.view.log.get("1.0", "end"))

    def test_reference_line_follows_applied_target_not_just_requested_target(self):
        self.view.mode.set("계산 미리보기")
        self.view.connect()
        self.assertEqual(65, self.view.charts._target_temperature)
        self.view.target_temperature.set("72")
        self.view.apply_target_temperature()
        self.assertEqual(65, self.view.charts._target_temperature)
        payload = dict(snapshot=snapshot("same", 4), effective_mode="preview",
                       proposed={}, reasons={}, applied={}, status="계산", display_target_c=65)
        self.view._show_snapshot(payload)
        self.assertEqual(65, self.view.charts._target_temperature)
        payload["display_target_c"] = 72
        self.view._show_snapshot(payload)
        self.assertEqual(72, self.view.charts._target_temperature)


if __name__ == "__main__":
    unittest.main()
