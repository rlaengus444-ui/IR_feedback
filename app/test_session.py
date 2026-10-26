"""No BinderJet connection: fake adapter, fake worker, and hidden Tk view tests."""

from dataclasses import replace
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from adapter_contracts import AdapterError, AmbiguousWriteError, ControlUnavailable, MachineWindow, UiAdapter, UiSnapshot
from session import BoundedEvents, SessionController, SessionOptions, WorkerEvent, run_ui_session


TARGET = MachineWindow("M1", 123, 456, "BinderJet M1 test", "C:/test/BinderJet.exe")


def snapshot(key="initial", layer=0, **values):
    defaults = dict(layer_index=layer, t_recoating_c=80, observation_key=key,
        heater1_setting_percent=50, heater2_setting_percent=50,
        minimum_poll_seconds=0.1, machine_state="AUTO")
    defaults.update(values)
    return UiSnapshot(TARGET, **defaults)


class DeferredThread:
    def __init__(self, target, **kwargs):
        self.target = target
        self.alive = False

    def start(self):
        self.alive = True

    def complete(self):
        self.target()
        self.alive = False

    def is_alive(self):
        return self.alive


class WorkerTests(unittest.TestCase):
    def test_first_csv_row_created_after_connect_counts_as_first_new_observation(self):
        adapter=Mock(spec=UiAdapter)
        frames=[snapshot("new1",1,control_available=True,observation_is_new=True),
                snapshot("new2",2,control_available=True,observation_is_new=True),
                snapshot("new3",3,control_available=True,observation_is_new=True)]
        adapter.read_snapshot.side_effect=frames
        stop=threading.Event();reports=[]
        def emit(kind,payload):
            if kind=="snapshot":
                reports.append(payload)
                if len(reports)==len(frames):stop.set()
        run_ui_session(SessionOptions(TARGET,mode="control"),stop,emit,lambda:adapter)
        adapter.apply_ir_power.assert_called_once_with(1,45,"new3",stop)

    def test_monitor_never_writes_and_disconnect_releases_only_own_adapter(self):
        adapter = Mock(spec=UiAdapter)
        adapter.read_snapshot.return_value = snapshot()
        stop = threading.Event()
        def emit(kind, value):
            if kind == "snapshot":
                stop.set()
        run_ui_session(SessionOptions(TARGET), stop, emit, lambda: adapter)
        adapter.apply_ir_power.assert_not_called()
        adapter.prepare_mode.assert_called_once_with("monitor", stop)
        adapter.disconnect.assert_called_once()

    def test_control_unavailable_is_visible_and_never_silently_becomes_preview(self):
        adapter = Mock(spec=UiAdapter)
        adapter.read_snapshot.return_value = snapshot(control_available=False,
            control_unavailable_reason="Whole settings commit is not safe")
        stop = threading.Event()
        reports = []
        def emit(kind, value):
            if kind == "snapshot":
                reports.append(value)
                stop.set()
        run_ui_session(SessionOptions(TARGET, mode="control"), stop, emit, lambda: adapter)
        self.assertEqual("control-unavailable", reports[0]["effective_mode"])
        self.assertEqual({}, reports[0]["proposed"])
        self.assertIn("Whole settings", reports[0]["status"])
        adapter.apply_ir_power.assert_not_called()

    def test_preview_waits_for_new_t_recoating_and_never_relearns_repeated_rows(self):
        adapter = Mock(spec=UiAdapter)
        frames = [snapshot(), snapshot("layer2", 1), snapshot("layer2", 1),
                  snapshot("layer3", 2), snapshot("layer4", 3)]
        adapter.read_snapshot.side_effect = frames
        reports = []
        stop = threading.Event()
        def emit(kind, value):
            if kind == "snapshot":
                reports.append(value)
                if len(reports) == len(frames):
                    stop.set()
        run_ui_session(SessionOptions(TARGET, mode="preview"), stop, emit, lambda: adapter)
        self.assertEqual([{}, {1: 50}, {1: 50}, {1: 50}, {1: 45}], [row["proposed"] for row in reports])
        adapter.apply_ir_power.assert_not_called()

    def test_wrong_window_identity_and_read_failure_close_without_output_actions(self):
        for response in (snapshot(), AdapterError("timeout")):
            adapter = Mock(spec=UiAdapter)
            if isinstance(response, Exception):
                adapter.read_snapshot.side_effect = response
            else:
                adapter.read_snapshot.return_value = replace(response, target=replace(TARGET, pid=999))
            with self.assertRaises(AdapterError):
                run_ui_session(SessionOptions(TARGET), threading.Event(), lambda *args: None, lambda: adapter)
            adapter.disconnect.assert_called_once()
            adapter.apply_ir_power.assert_not_called()

    def test_cancelled_before_connect_performs_no_external_calls(self):
        factory = Mock()
        stop = threading.Event()
        stop.set()
        run_ui_session(SessionOptions(TARGET), stop, lambda *args: None, factory)
        factory.assert_not_called()

    def test_authorized_control_saves_once_for_changed_common_ir_only(self):
        adapter = Mock(spec=UiAdapter)
        frames = [snapshot(control_available=True),
                  snapshot("next1", 1, control_available=True),
                  snapshot("next2", 2, control_available=True),
                  snapshot("next3", 3, control_available=True)]
        adapter.read_snapshot.side_effect = frames
        stop = threading.Event()
        reports = []
        def emit(kind, value):
            if kind == "snapshot":
                reports.append(value)
                if len(reports) == len(frames):
                    stop.set()
        run_ui_session(SessionOptions(TARGET, mode="control"), stop, emit, lambda: adapter)
        adapter.prepare_mode.assert_called_once_with("control", stop)
        adapter.apply_ir_power.assert_called_once_with(1, 45, "next3", stop)
        self.assertIn("UI 자동 적용 완료", reports[-1]["status"])

    def test_ambiguous_save_stops_without_retry_restore_or_heater_off(self):
        adapter = Mock(spec=UiAdapter)
        adapter.read_snapshot.side_effect = [snapshot(control_available=True),
            snapshot("next1", 1, control_available=True), snapshot("next2", 2, control_available=True),
            snapshot("next3", 3, control_available=True)]
        adapter.apply_ir_power.side_effect = AmbiguousWriteError("Save result unknown")
        with self.assertRaises(AmbiguousWriteError):
            run_ui_session(SessionOptions(TARGET, mode="control"), threading.Event(), lambda *args: None, lambda: adapter)
        self.assertEqual(1, adapter.apply_ir_power.call_count)
        adapter.disconnect.assert_called_once()

    def test_loss_of_ui_control_admission_stops_until_explicit_reconnect(self):
        adapter = Mock(spec=UiAdapter)
        adapter.read_snapshot.side_effect = [snapshot(control_available=True), snapshot("next", 1,
            control_available=False, control_unavailable_reason="operator changed page")]
        with self.assertRaises(ControlUnavailable):
            run_ui_session(SessionOptions(TARGET, mode="control"), threading.Event(), lambda *args: None, lambda: adapter)
        adapter.apply_ir_power.assert_not_called()
        adapter.disconnect.assert_called_once()

    def test_equal_layer_new_job_or_row_correction_clears_history_and_waits(self):
        adapter = Mock(spec=UiAdapter)
        frames = [snapshot(), snapshot("a", 1), snapshot("b", 2), snapshot("c", 3),
                  snapshot("same-layer-new-job", 3), snapshot("same-layer-correction", 3),
                  snapshot("d", 4), snapshot("e", 5), snapshot("f", 6)]
        adapter.read_snapshot.side_effect = frames
        stop = threading.Event()
        reports = []
        def emit(kind, value):
            if kind == "snapshot":
                reports.append(value)
                if len(reports) == len(frames):
                    stop.set()
        run_ui_session(SessionOptions(TARGET, mode="preview"), stop, emit, lambda: adapter)
        self.assertEqual([{}, {1: 50}, {1: 50}, {1: 45}, {}, {}, {1: 50}, {1: 50}, {1: 45}],
                         [row["proposed"] for row in reports])
        adapter.apply_ir_power.assert_not_called()

    def test_run_identity_change_resets_even_when_layer_number_increases(self):
        adapter = Mock(spec=UiAdapter)
        frames = [replace(snapshot(), run_key="job-a"), replace(snapshot("a", 1), run_key="job-a"),
            replace(snapshot("b", 2), run_key="job-a"), replace(snapshot("c", 3), run_key="job-a"),
            replace(snapshot("new-job", 4), run_key="job-b"), replace(snapshot("next", 5), run_key="job-b")]
        adapter.read_snapshot.side_effect = frames
        stop = threading.Event()
        reports = []
        def emit(kind, value):
            if kind == "snapshot":
                reports.append(value)
                if len(reports) == len(frames):
                    stop.set()
        run_ui_session(SessionOptions(TARGET, mode="preview"), stop, emit, lambda: adapter)
        self.assertEqual({}, reports[-2]["proposed"])
        self.assertEqual({1: 50}, reports[-1]["proposed"])

    def test_external_ir_change_stops_ui_ownership_without_overwriting_it(self):
        adapter = Mock(spec=UiAdapter)
        adapter.read_snapshot.side_effect = [snapshot(control_available=True),
            replace(snapshot("manual", 1, control_available=True), heater1_setting_percent=55, heater2_setting_percent=55)]
        with self.assertRaises(ControlUnavailable):
            run_ui_session(SessionOptions(TARGET, mode="control"), threading.Event(), lambda *args: None, lambda: adapter)
        adapter.apply_ir_power.assert_not_called()
        adapter.disconnect.assert_called_once()

    def test_delayed_visible_historical_row_is_baseline_and_never_a_new_input(self):
        adapter = Mock(spec=UiAdapter)
        frames = [replace(snapshot(control_available=True), layer_index=None,
            t_recoating_c=None, observation_key=None), snapshot("historical", 0, control_available=True),
            snapshot("fresh1", 1, control_available=True), snapshot("fresh2", 2, control_available=True),
            snapshot("fresh3", 3, control_available=True)]
        adapter.read_snapshot.side_effect = frames
        stop = threading.Event()
        reports = []
        def emit(kind, value):
            if kind == "snapshot":
                reports.append(value)
                if len(reports) == len(frames):
                    stop.set()
        run_ui_session(SessionOptions(TARGET, mode="control"), stop, emit, lambda: adapter)
        self.assertEqual([{}, {}, {1: 50}, {1: 50}, {1: 45}], [row["proposed"] for row in reports])
        adapter.apply_ir_power.assert_called_once_with(1, 45, "fresh3", stop)

    def test_fractional_actual_setting_is_held_then_saved_without_truncation(self):
        for temperature, expected in ((80, 57.5), (50, 67.5)):
            with self.subTest(temperature=temperature):
                adapter = Mock(spec=UiAdapter)
                frames = [replace(snapshot(key, layer, control_available=True),
                    t_recoating_c=temperature, heater1_setting_percent=62.5, heater2_setting_percent=62.5)
                    for key, layer in (("historical", 0), ("fresh1", 1), ("fresh2", 2), ("fresh3", 3))]
                adapter.read_snapshot.side_effect = frames
                stop = threading.Event()
                reports = []
                def emit(kind, value):
                    if kind == "snapshot":
                        reports.append(value)
                        if len(reports) == len(frames):
                            stop.set()
                run_ui_session(SessionOptions(TARGET, mode="control"), stop, emit, lambda: adapter)
                self.assertEqual([{}, {1: 62.5}, {1: 62.5}, {1: expected}], [row["proposed"] for row in reports])
                adapter.apply_ir_power.assert_called_once_with(1, expected, "fresh3", stop)

    def test_dotnet_canonical_float_roundoff_does_not_look_like_external_edit(self):
        adapter = Mock(spec=UiAdapter)
        actual = 62.12345678901235
        canonical = 67.1234567890124
        frames = [replace(snapshot(key, layer, control_available=True), t_recoating_c=50,
            heater1_setting_percent=actual, heater2_setting_percent=actual)
            for key, layer in (("historical", 0), ("fresh1", 1), ("fresh2", 2), ("fresh3", 3))]
        frames.append(replace(frames[-1], heater1_setting_percent=canonical, heater2_setting_percent=canonical))
        adapter.read_snapshot.side_effect = frames
        stop = threading.Event()
        reports = []
        def emit(kind, value):
            if kind == "snapshot":
                reports.append(value)
                if len(reports) == len(frames):
                    stop.set()
        run_ui_session(SessionOptions(TARGET, mode="control"), stop, emit, lambda: adapter)
        self.assertEqual(1, adapter.apply_ir_power.call_count)
        self.assertAlmostEqual(actual + 5, adapter.apply_ir_power.call_args.args[1])
        self.assertEqual(5, len(reports))

    def test_pause_resume_clears_preview_history_and_rebaselines_existing_row(self):
        adapter = Mock(spec=UiAdapter)
        frames = [snapshot(), snapshot("a", 1), snapshot("b", 2), snapshot("c", 3),
            snapshot("c", 3, machine_state="IDLE"), snapshot("c", 3),
            snapshot("d", 4), snapshot("e", 5), snapshot("f", 6)]
        adapter.read_snapshot.side_effect = frames
        stop = threading.Event()
        reports = []
        def emit(kind, value):
            if kind == "snapshot":
                reports.append(value)
                if len(reports) == len(frames):
                    stop.set()
        run_ui_session(SessionOptions(TARGET, mode="preview"), stop, emit, lambda: adapter)
        self.assertEqual([{}, {1: 50}, {1: 50}, {1: 45}, {}, {}, {1: 50}, {1: 50}, {1: 45}],
                         [row["proposed"] for row in reports])
        self.assertEqual("waiting", reports[4]["effective_mode"])
        adapter.apply_ir_power.assert_not_called()

    def test_idle_unknown_rows_never_save_and_auto_resume_waits_for_fresh_rows(self):
        adapter = Mock(spec=UiAdapter)
        frames = [snapshot(control_available=True), snapshot("a", 1, control_available=True),
            snapshot("idle-row", 2, control_available=True, machine_state="IDLE"),
            snapshot("unknown-row", 3, control_available=True, machine_state="UNKNOWN"),
            snapshot("resume-baseline", 4, control_available=True, machine_state="RUNNING"),
            snapshot("fresh1", 5, control_available=True, machine_state="RUNNING"),
            snapshot("fresh2", 6, control_available=True, machine_state="RUNNING"),
            snapshot("fresh3", 7, control_available=True, machine_state="RUNNING")]
        adapter.read_snapshot.side_effect = frames
        stop = threading.Event()
        reports = []
        def emit(kind, value):
            if kind == "snapshot":
                reports.append(value)
                if len(reports) == len(frames):
                    stop.set()
        run_ui_session(SessionOptions(TARGET, mode="control"), stop, emit, lambda: adapter)
        self.assertEqual([{}, {1: 50}, {}, {}, {}, {1: 50}, {1: 50}, {1: 45}],
                         [row["proposed"] for row in reports])
        adapter.apply_ir_power.assert_called_once_with(1, 45, "fresh3", stop)

    def test_unknown_machine_cannot_enter_control_mode(self):
        target = replace(TARGET, machine="unknown")
        SessionOptions(target, mode="monitor")
        with self.assertRaises(ValueError):
            SessionOptions(target, mode="control")


class ControllerTests(unittest.TestCase):
    def test_single_worker_disconnect_and_generation_filters(self):
        emitted = []
        def runner(options, stop, emit):
            emitted.append(emit)
            emit("connected", {})
        controller = SessionController(runner, DeferredThread)
        self.assertTrue(controller.connect(SessionOptions(TARGET)))
        self.assertFalse(controller.connect(SessionOptions(TARGET)))
        first = controller.thread
        controller.disconnect()
        self.assertTrue(controller.stop_event.is_set())
        self.assertEqual("disconnecting", controller.state)
        first.complete()
        controller.poll()
        self.assertEqual("disconnected", controller.state)
        self.assertTrue(controller.connect(SessionOptions(TARGET)))
        emitted[0]("snapshot", {"late": True})
        self.assertEqual([], controller.poll())

    def test_disconnect_and_close_do_not_join_or_wait_on_blocked_worker(self):
        controller = SessionController(lambda *args: None, DeferredThread)
        controller.connect(SessionOptions(TARGET))
        started = time.monotonic()
        controller.disconnect()
        controller.close()
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertTrue(controller.worker_alive)
        controller.events.put(WorkerEvent(controller.generation, "snapshot", {}))
        self.assertEqual([], controller.poll())
        self.assertFalse(controller.connect(SessionOptions(TARGET)))

    def test_event_backlog_is_bounded(self):
        events = BoundedEvents(capacity=12)
        for number in range(1000):
            events.put(WorkerEvent(1, "snapshot", number))
        values = events.drain(1000)
        self.assertEqual(12, len(values))
        self.assertEqual(999, values[-1].payload)

    def test_ambiguous_write_is_reported_even_if_disconnect_was_requested(self):
        def runner(options, stop, emit):
            stop.set()
            raise AmbiguousWriteError("저장 결과 확인 불가")
        controller = SessionController(runner, DeferredThread)
        controller.connect(SessionOptions(TARGET))
        controller.thread.complete()
        events = controller.poll()
        self.assertIn("error", [event.kind for event in events])
        self.assertEqual("error", controller.state)

    def test_ambiguous_write_is_persisted_if_window_is_already_closing(self):
        def runner(options, stop, emit):
            raise AmbiguousWriteError("저장 확인 전에 창 닫힘")
        logger = Mock()
        controller = SessionController(runner, DeferredThread, error_logger=logger)
        controller.connect(SessionOptions(TARGET))
        controller.close()
        controller.thread.complete()
        logger.assert_called_once_with("저장 확인 전에 창 닫힘")
        self.assertEqual([], controller.poll())


class ViewTests(unittest.TestCase):
    def setUp(self):
        from standalone_ir import ThermalFeedbackWindow
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.controller = SessionController(lambda *args: None, DeferredThread)
        self.view = ThermalFeedbackWindow(self.root, controller=self.controller,
            discoverer=lambda: [], auto_discover=False)
        self.view.targets = [TARGET, replace(TARGET, machine="S3", pid=124, hwnd=457)]
        self.view._populate_hosts()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def test_default_monitor_machine_selection_and_explicit_buttons(self):
        self.assertEqual("조회", self.view.mode.get())
        self.assertEqual(("M1", "S3"), self.view.machine_box["values"])
        self.assertEqual("normal", str(self.view.connect_button["state"]))
        self.view.machine.set("S3")
        self.view._populate_hosts()
        self.assertEqual("S3", self.view.target_map[self.view.host.get()].machine)
        self.view.connect()
        self.assertEqual("disabled", str(self.view.connect_button["state"]))
        self.assertEqual("normal", str(self.view.disconnect_button["state"]))
        self.view.disconnect()
        self.assertTrue(self.controller.stop_event.is_set())

    def test_control_requires_explicit_selection_and_connect(self):
        self.assertIsNone(self.controller.thread)
        self.view.mode.set("UI 자동 적용")
        self.assertIsNone(self.controller.thread)
        runner = Mock()
        self.controller.runner = runner
        self.view.connect()
        self.assertIsNotNone(self.controller.thread)
        self.assertIn("수동", self.view.capability.get())
        runner.assert_not_called()
        self.controller.thread.complete()
        self.assertEqual("control", runner.call_args.args[0].mode)

    def test_log_soft_limit_and_close_deadline(self):
        for number in range(300):
            self.view._append_log(str(number))
        self.assertLessEqual(len(self.view.log.get("1.0", "end").splitlines()), 102)
        self.view.connect()
        started = time.monotonic()
        self.view.close()
        self.assertLess(time.monotonic() - started, 0.05)
        self.view.close_deadline = time.monotonic() - 1
        self.view._pump()  # Deadline destroys the hidden test view without a join.


class SingleInstanceTests(unittest.TestCase):
    def test_actual_private_mutex_prevents_a_second_owner_and_can_be_reacquired(self):
        from single_instance import SingleInstance
        first = SingleInstance(r"Local\MADDE.IRFeedback.Test." + uuid.uuid4().hex)
        second = SingleInstance(first.name)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.close()
            self.assertTrue(second.acquire())
        finally:
            first.close()
            second.close()

    def test_second_main_entry_does_not_create_a_window_or_adapter(self):
        import standalone_ir
        gate = Mock()
        gate.acquire.return_value = False
        with patch.object(standalone_ir, "SingleInstance", return_value=gate), \
                patch.object(standalone_ir.tk, "Tk") as tk_root, \
                patch.object(standalone_ir.messagebox, "showinfo") as message, \
                patch.object(standalone_ir.sys, "excepthook"):
            standalone_ir.main()
        message.assert_called_once()
        tk_root.assert_not_called()


if __name__ == "__main__":
    unittest.main()
