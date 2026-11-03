import threading
import unittest

from output_limit import LiveMaximumIrChanged, LiveMaximumIrSetting, limit_ir_recommendation


class LiveLimitSettingTests(unittest.TestCase):
    def test_bounds_and_status(self):
        setting = LiveMaximumIrSetting(30.5)
        self.assertFalse(setting.status()['ready'])
        with self.assertRaises(ValueError): setting.set(40)
        with self.assertRaises(ValueError): setting.bind_bounds(31, 80)
        setting.bind_bounds(10, 25, 2)
        stamp = setting.snapshot()
        self.assertEqual((0, 30.5), stamp)
        self.assertTrue(setting.activate(stamp))
        self.assertEqual(25, setting.status()['active_effective_cap'])
        with self.assertRaises(ValueError): setting.set(9.9)
        with self.assertRaises(ValueError): setting.bind_bounds(0, 100)

    def test_aba_and_admission(self):
        setting = LiveMaximumIrSetting(50)
        setting.bind_bounds(0, 100)
        stamp = setting.snapshot()
        self.assertTrue(setting.activate(stamp))
        setting.set(45)
        setting.set(50)
        self.assertFalse(setting.activate(stamp))
        with self.assertRaises(LiveMaximumIrChanged): setting.begin_admission(stamp, 45, 50)
        new = setting.snapshot()
        self.assertTrue(setting.activate(new))
        token = setting.begin_admission(new, 45, 50)
        self.assertTrue(setting.status()['in_flight'])
        with self.assertRaises(LiveMaximumIrChanged): setting.begin_admission(new, 45, 50)
        setting.set(40)
        setting.end_admission(token)
        with self.assertRaises(LiveMaximumIrChanged): setting.begin_admission(new, 45, 50)

    def test_no_lock_held_during_admission(self):
        setting = LiveMaximumIrSetting(50)
        setting.bind_bounds(0, 100)
        stamp = setting.snapshot()
        setting.activate(stamp)
        token = setting.begin_admission(stamp, 45, 50)
        done = threading.Event()
        thread = threading.Thread(target=lambda: (setting.set(40), done.set()), daemon=True)
        thread.start()
        self.assertTrue(done.wait(1))
        setting.end_admission(token)
        thread.join()

    def test_ramp_and_validation(self):
        self.assertEqual(45, limit_ir_recommendation(50, 0, 20))
        self.assertEqual(47.5, limit_ir_recommendation(50, 90, 47.5))
        self.assertEqual(0, limit_ir_recommendation(0, 0, 0))
        self.assertEqual(31, limit_ir_recommendation(33, 0, 10, 10, 80, 2))
        for raw in (-1, 101, float('nan'), True):
            with self.assertRaises(ValueError): limit_ir_recommendation(50, raw, 10)
        setting = LiveMaximumIrSetting(20)
        setting.bind_bounds(10, 80, 2)
        stamp = setting.snapshot(); setting.activate(stamp)
        with self.assertRaises(LiveMaximumIrChanged): setting.begin_admission(stamp, 20, 30)
        token = setting.begin_admission(stamp, 28, 30)
        setting.end_admission(token)
