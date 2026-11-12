"""No elapsed expiry; invalidated observations still cancel owned Excel work."""
import queue
import threading
import time
import unittest
from unittest.mock import patch
from excel_calculation import ExcelCalculation
from excel_workbook import WorkbookTimeout
from test_excel_transport import Process,controller
from test_excel_calculation import Group,row
import test_excel_async
from test_excel_async import FakeController,wait_for


class WorkbookStepValidityTests(unittest.TestCase):
    def test_live_step_waits_past_old_budget_and_keeps_one_request(self):
        for delay in (10,30,60,120):
            with self.subTest(delay=delay):
                p=Process();c=controller(p);clock=[100.0]
                class Response:
                    calls=0
                    def get(self,timeout):
                        self.calls+=1
                        if self.calls==1:clock[0]+=delay;raise queue.Empty
                        return {'value':42}
                c._queue=Response()
                with patch('excel_workbook.time.monotonic',side_effect=lambda:clock[0]):
                    self.assertEqual({'value':42},c._call({'op':'step'}))
                self.assertEqual(p.kills,0)
                self.assertEqual(1,p.stdin.getvalue().count('"op": "step"'))

    def test_stop_still_interrupts_an_unbounded_step(self):
        p=Process();c=controller(p)
        class Response:
            def get(self,timeout):c.stop_event.set();raise queue.Empty
        c._queue=Response()
        with self.assertRaises(InterruptedError):c._call({'op':'step'})
        self.assertEqual(1,p.kills)


class OwnerStepValidityTests(unittest.TestCase):
    def test_busy_owner_has_no_step_watchdog_after_120_seconds(self):
        fixture=test_excel_async.AsyncTests('test_both_heaters_owned_and_result_atomic');fixture.setUp()
        self.addCleanup(fixture.doCleanups);fixture.start()
        FakeController.block='process'
        fixture.group.submit('ticket',2,65,{'A':50,'B':50},65)
        self.assertTrue(FakeController.entered.wait(1))
        self.assertIsNone(fixture.group._deadline)
        future=time.monotonic()+120
        with patch('excel_async.time.monotonic',return_value=future):
            self.assertIsNone(fixture.group.poll())
            self.assertEqual('busy',fixture.group.state)
        FakeController.gate.set()
        self.assertEqual('result',wait_for(fixture.group.poll)['kind'])


class ReplacementCalculationTests(unittest.TestCase):
    def setUp(self):
        self.groups=[];self.now=0;test=self
        class Retirable(Group):
            def __init__(self,*a,**kw):
                super().__init__(*a,**kw);self.prepared_workbook=kw.get('prepared_workbook',object());test.groups.append(self)
            def close(self):super().close();self.state='closed'
            def retirement_status(self):return {'verified':self.closed}
        self.calc=ExcelCalculation(None,100,100,threading.Event(),group_factory=Retirable,clock=lambda:self.now)
        self.kw=dict(eligible=True,powers={1:50},heaters=(1,),target_stamp=(1,100),image_epoch=None)
        self.watch('1');self.groups[0].events.append({'kind':'ready'});self.watch('1');self.calculate('2')
    def watch(self,key,**changes):self.calc.watch(row(key),**(self.kw|changes))
    def calculate(self,key,**changes):
        kw=self.kw|changes
        return self.calc.calculate(row(key),kw['powers'],kw['heaters'],kw['target_stamp'],None)

    def test_newer_row_retires_old_work_and_uses_prepared_copy(self):
        old=self.groups[0];ticket=old.jobs[-1][0]
        self.watch('3');self.assertTrue(old.closed)
        old.events.append({'kind':'result','ticket':ticket,'results':{1:99}})
        self.watch('3');new=self.groups[-1];self.assertIsNot(new,old)
        self.assertIs(new.prepared_workbook,old.prepared_workbook)
        new.events.append({'kind':'ready'});self.watch('3');self.calculate('3')
        self.assertEqual(1,len(new.jobs));self.assertEqual(4,new.jobs[0][1])
        self.assertIsNone(self.calc.accepted_stamp)

    def test_target_aba_never_recalculates_same_row_after_replacement(self):
        self.watch('2',target_stamp=(2,110));self.watch('2',target_stamp=(3,100))
        new=self.groups[-1];new.events.append({'kind':'ready'});self.watch('2',target_stamp=(3,100))
        self.assertIsNone(self.calculate('2',target_stamp=(3,100)))
        self.assertEqual([],new.jobs)
        self.watch('3',target_stamp=(3,100));self.calculate('3',target_stamp=(3,100))
        self.assertEqual(1,len(new.jobs))

    def test_real_replacement_init_fault_keeps_backoff_and_fresh_baseline(self):
        self.watch('3');self.watch('3');failed=self.groups[-1]
        failed.events.append({'kind':'timeout','error':WorkbookTimeout('init',operation='init',elapsed=90,deadline=90,phase='load',process_state='exited')})
        failed.close();self.watch('3')
        self.assertFalse(self.calc._context_restart)
        self.assertIsNone(self.calc._resume_context)
        self.watch('3');self.assertEqual(2,self.calc._retry_at);self.assertEqual(2,len(self.groups))
        self.now=2;self.watch('3');new=self.groups[-1]
        new.events.append({'kind':'ready'});self.watch('3')
        self.assertIsNone(self.calculate('3'))


class ArrivalAuthorityTests(unittest.TestCase):
    def test_preconnection_history_is_never_arrival_authorized(self):
        import tempfile
        import test_windows_ui_adapter as fixtures
        with tempfile.TemporaryDirectory() as directory:
            raw,state,calls,path,values,append,baseline,source,audit=fixtures.AdapterTests.fixture(self,directory)
            self.assertFalse(baseline.observation_fresh)
            self.assertFalse(baseline.observation_is_new)
            with self.assertRaises(Exception):raw._verify_csv_observation(baseline.observation_key)
            self.assertEqual(0,state['saves'])

    def test_layer_advance_invalidates_even_without_a_new_csv_row(self):
        import tempfile
        from datetime import datetime
        import test_windows_ui_adapter as fixtures
        from adapter_contracts import ControlUnavailable
        with tempfile.TemporaryDirectory() as directory:
            raw,state,calls,path,values,append,baseline,source,audit=fixtures.AdapterTests.fixture(self,directory)
            snap=append();log=source.parents[2]/'Logs'/'20261027_M1.log'
            with log.open('a') as f:f.write(datetime.now().strftime('%Y-%m-%d%p%I%M%S')+'...[BJT - AUTO]: Layer: 5 -> Current Z = 1\n')
            with self.assertRaises(ControlUnavailable):raw._verify_csv_observation(snap.observation_key)
            raw._last_read=0;current=raw.read_snapshot(threading.Event())
            self.assertIsNone(current.observation_key)
            self.assertFalse(current.observation_fresh)
            self.assertEqual(0,state['saves'])


class OfflineDisplayTests(unittest.TestCase):
    def test_offline_csv_display_grants_no_control_or_arrival_authority(self):
        from datetime import datetime,timedelta
        from types import SimpleNamespace
        from unittest.mock import Mock
        from csv_source import CsvSnapshot,JobContext
        from resilient_adapter import ResilientUiAdapter
        from test_resilient_adapter import TARGET
        for token in (None,{'sha256':'display-only'}):
            for value,age,expected in ((42,120,42),(-1,0,None),(float('nan'),0,None),(42,-5,None)):
                with self.subTest(token=token,value=value,age=age):
                    sample=SimpleNamespace(layer_index=1,temperature=value,finished=datetime.now()-timedelta(seconds=age),key='row',measurement_valid=value!=-1)
                    adapter=ResilientUiAdapter();adapter._target=TARGET
                    adapter._reader=Mock(read=Mock(return_value=CsvSnapshot(sample,JobContext(run_key='job'),token,True,'csv')))
                    result=adapter._offline()
                    self.assertEqual(expected,result.t_recoating_c)
                    self.assertEqual('row' if expected is not None and token else None,result.observation_key)
                    self.assertEqual('unknown',result.machine_state)
                    self.assertFalse(result.control_available)
                    self.assertFalse(result.observation_fresh)
                    self.assertFalse(result.observation_is_new)
                    self.assertIsNone(adapter._raw)
