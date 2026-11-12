"""Excel history and versioned cap admission, using offline groups."""
import threading
import unittest

from excel_calculation import ExcelCalculation
from excel_workbook import WorkbookController
from test_excel_calculation import Group, row


class LimitExcelTests(unittest.TestCase):
    def setUp(self):
        self.calc = ExcelCalculation(None, 100, 100, threading.Event(), group_factory=Group)
        self.powers = {1: 20, 2: 30}
        self.watch(row('1'), (0, 40))
        self.group = self.calc.group
        self.group.events.append({'kind': 'ready'})
        self.watch(row('1'), (0, 40))

    def watch(self, snapshot, cap):
        self.calc.watch(snapshot, eligible=True, powers=self.powers, heaters=(1, 2),
                        target_stamp=(0, 100), image_epoch=None, cap_stamp=cap)

    def calculate(self, snapshot, cap):
        return self.calc.calculate(snapshot, self.powers, (1, 2), (0, 100), None, cap_stamp=cap)

    def test_pending_cap_aba_discards_result_and_resets(self):
        self.assertIsNone(self.calculate(row('2'), (0, 40)))
        old_ticket = self.group.jobs[-1][0]
        self.watch(row('2'), (1, 60))
        self.watch(row('2'), (2, 40))
        self.assertEqual(self.group.resets,[])
        self.assertTrue(self.group.closed)
        self.assertFalse(self.calc.pending)
        self.group.events.append({'kind': 'result', 'ticket': old_ticket, 'results': {1: 9, 2: 10}})
        self.watch(row('2'), (2, 40))
        self.assertIsNone(self.calculate(row('2'), (2, 40)))

    def test_unconsumed_cap_change_discards_completed_result(self):
        self.calculate(row('2'), (0, 40))
        ticket = self.group.jobs[-1][0]
        self.group.events.append({'kind': 'result', 'ticket': ticket, 'results': {1: 9, 2: 10}})
        self.watch(row('2'), (0, 40))
        self.watch(row('2'), (1, 60))
        self.assertEqual(len(self.group.resets), 1)
        self.assertIsNone(self.calculate(row('2'), (1, 60)))

    def test_idle_cap_change_keeps_accepted_history(self):
        self.calculate(row('2'), (0, 40))
        ticket = self.group.jobs[-1][0]
        self.group.events.append({'kind': 'result', 'ticket': ticket, 'results': {1: 9, 2: 10}})
        self.watch(row('2'), (0, 40))
        self.assertEqual(self.calculate(row('2'), (0, 40)), {1: 9, 2: 10})
        self.assertEqual(self.calc.accepted_cap_stamp, (0, 40))
        self.watch(row('3'), (1, 80))
        self.assertEqual(self.group.resets, [])
        self.calculate(row('3'), (1, 80))
        self.assertEqual(len(self.group.jobs), 2)

    def test_immediate_result_retains_cap_stamp_through_recursive_poll(self):
        class Immediate(Group):
            def submit(self, *args):
                super().submit(*args)
                self.events.append({'kind': 'result', 'ticket': args[0], 'results': {1: 5, 2: 6}})
                return True
        calc = ExcelCalculation(None, 100, 100, threading.Event(), group_factory=Immediate)
        kwargs = dict(eligible=True, powers=self.powers, heaters=(1, 2), target_stamp=(0, 100),
                      image_epoch=None, cap_stamp=(3, 80))
        calc.watch(row('1'), **kwargs)
        calc.group.events.append({'kind': 'ready'})
        calc.watch(row('1'), **kwargs)
        self.assertEqual(calc.calculate(row('2'), self.powers, (1, 2), (0, 100), None,
                                        cap_stamp=(3, 80)), {1: 5, 2: 6})
        self.assertEqual(calc.accepted_cap_stamp, (3, 80))

    def test_physical_ceiling_allows_later_increase_above_initial_user_cap(self):
        controller = WorkbookController.__new__(WorkbookController)
        controller.maximum_ir_percent = 100.0
        controller.target = 100
        controller._count = 0
        controller._last_layer = None
        controller.metadata = {'first_row': 1, 'last_row': 10}
        controller.spec = None
        controller._call = lambda request: {'value': 85.0}
        self.assertEqual(controller.process(1, 80, 80).power_percent, 85)


if __name__ == '__main__':
    unittest.main()
