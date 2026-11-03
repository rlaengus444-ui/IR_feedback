"""Hold diagnostics survive GUI queuing without changing control authority."""
from dataclasses import replace
import threading
import unittest
from unittest.mock import Mock

from adapter_contracts import UiAdapter
from session import SessionController, SessionOptions, run_ui_session
from test_session import TARGET, DeferredThread, snapshot
from test_excel_history_continuity import HistoryHarness


class HoldDiagnosticsTests(unittest.TestCase):
    def test_fault_transitions_are_logged_before_queue_even_during_close(self):
        def runner(options, stop, emit):
            emit('control_hold', '작업 로그 기록 중 · 재확인 대기')
            emit('history_reset', '초기화 원인 · 로그 읽기 재시도 초과')
            for i in range(300):
                emit('log', str(i))
        logger = Mock()
        controller = SessionController(runner, DeferredThread, error_logger=logger)
        controller.connect(SessionOptions(TARGET))
        controller.close()
        controller.thread.complete()
        self.assertEqual([], controller.poll())
        self.assertEqual(2, logger.call_count)
        self.assertIn('작업 로그 기록 중', logger.call_args_list[0].args[0])
        self.assertIn('초기화 원인', logger.call_args_list[1].args[0])

    def test_unknown_state_preserves_original_reason_and_logs_transition_once(self):
        reason = '원본 로그 기록 중 · 안정된 작업 정보를 다시 확인합니다.'
        frames = [snapshot(control_available=True), snapshot('next', 1, control_available=True)]
        held = replace(frames[-1], machine_state='unknown', control_available=False,
                       control_recoverable=True, control_unavailable_reason=reason)
        frames += [held, held, held]
        adapter = Mock(spec=UiAdapter)
        adapter.read_snapshot.side_effect = frames
        stop, events = threading.Event(), []
        def emit(kind, payload):
            events.append((kind, payload))
            if sum(k == 'snapshot' for k, _ in events) == len(frames):
                stop.set()
        run_ui_session(SessionOptions(TARGET, mode='control'), stop, emit, lambda: adapter)
        holds = [p for k, p in events if k == 'control_hold']
        self.assertEqual(1, len(holds))
        self.assertIn(reason, holds[0])
        self.assertIn(reason, [p for k, p in events if k == 'snapshot'][-1]['status'])
        adapter.apply_ir_power.assert_not_called()

    def test_excel_reset_records_specific_reason_only_once(self):
        h = HistoryHarness()
        self.addCleanup(h.close)
        h.start()
        h.wait_frame(h.row('accepted', 1))
        h.accepted('accepted')
        reason = '원본 로그 읽기 재시도 초과'
        held = replace(h.frame, control_available=False, control_recoverable=True,
                       control_unavailable_reason=reason)
        h.wait_frame(held)
        h.wait(h.reset_entered.is_set)
        h.wait(lambda: any(k == 'history_reset' for k, _ in h.events))
        reads = h.reads
        h.wait(lambda: h.reads >= reads + 2)
        records = [p for k, p in h.events if k == 'history_reset']
        self.assertEqual(1, len(records))
        self.assertIn(reason, records[0])
        self.assertIn('완료 이력=True', records[0])
        self.assertEqual(1, h.resets)
        h.adapter.apply_ir_power.assert_not_called()


if __name__ == '__main__':
    unittest.main()
