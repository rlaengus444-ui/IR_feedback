import csv
from datetime import datetime, timedelta
import io
import os
from pathlib import Path
import tempfile
import time
import unittest

from adapter_contracts import MachineWindow
from csv_source import CsvSourceError, JobContext, JobLog, MonitoringCsvReader, log_timestamp


HEADER = list(MonitoringCsvReader.REQUIRED) + ["PrintShiftFromCenterPixels", "MonitoringEvaluation"]


def encode_rows(rows):
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\r\n").writerows(rows)
    return stream.getvalue().encode("utf-8")


class FixedLog:
    def __init__(self, context): self.context = context
    def read(self): return self.context


class CsvSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime.now().replace(microsecond=0)
        self.started = self.now - timedelta(minutes=3)
        self.context = JobContext("job-1", 0, self.started, 1, "AUTO")
        self.log = FixedLog(self.context)
        self.folder = self.root / (self.started.strftime("%Y%m%d_%H%M%S") + "_1820-layer")
        self.folder.mkdir()
        self.path = self.folder / ("LayerDatasheet_" + self.started.strftime("%Y%m%d_%H%M%S") + ".csv")
        self.target = MachineWindow("M1", 1, 2, "fixture", str(self.root / "Operator.exe"))
        self.path.write_bytes(encode_rows([HEADER, self.row(1, 42, self.now - timedelta(minutes=2))]))
        self.reader = MonitoringCsvReader(self.target, roots=[self.root], log=self.log, clock=lambda:self.now.timestamp()+.5)

    def row(self, identity, temp, finish, *, note="Incomplete"):
        return [str(identity), self.started.strftime("%m/%d/%Y %H:%M:%S"),
                (finish-timedelta(seconds=24)).strftime("%m/%d/%Y %H:%M:%S"),
                finish.strftime("%m/%d/%Y %H:%M:%S"), str(temp), "-27,72", note]

    def append(self, rows):
        with self.path.open("ab") as stream: stream.write(encode_rows(rows))

    def test_initial_history_is_baseline_then_new_complete_row_is_received(self):
        first = self.reader.read()
        self.assertEqual((0,42,False), (first.sample.layer_index,first.sample.temperature,first.is_new))
        self.append([self.row(2,43.2,self.now)])
        second = self.reader.read()
        self.assertEqual((1,43.2,True,24),(second.sample.layer_index,second.sample.temperature,second.is_new,second.sample.delay_seconds))
        self.assertIsNotNone(second.token)
        self.assertFalse(self.reader.read().is_new)

    def test_partial_row_is_not_a_measurement_or_a_save_token(self):
        baseline = self.reader.read()
        raw = encode_rows([self.row(2,43,self.now)])
        with self.path.open("ab") as stream: stream.write(raw[:-4])
        partial = self.reader.read()
        self.assertEqual(baseline.sample.key, partial.sample.key)
        self.assertIsNone(partial.token)
        with self.path.open("ab") as stream: stream.write(raw[-4:])
        completed = self.reader.read()
        self.assertTrue(completed.is_new)
        self.assertEqual(43, completed.sample.temperature)

    def test_quoted_comma_newline_and_split_quote_are_not_extra_rows(self):
        self.reader.read()
        raw = encode_rows([self.row(2,44,self.now,note='note, "quoted"\r\nsecond line')])
        quote = raw.index(b'""') + 1
        with self.path.open("ab") as stream: stream.write(raw[:quote])
        self.assertFalse(self.reader.read().is_new)
        with self.path.open("ab") as stream: stream.write(raw[quote:])
        self.assertEqual(44, self.reader.read().sample.temperature)

    def test_stale_appended_record_is_not_new_control_input(self):
        self.reader.read()
        self.append([self.row(2,44,self.now-timedelta(seconds=30))])
        result = self.reader.read()
        self.assertFalse(result.is_new)
        self.assertEqual(44,result.sample.temperature)

    def test_multiple_new_rows_only_return_latest_without_replaying_backlog(self):
        self.reader.read()
        self.append([self.row(2,44,self.now-timedelta(seconds=60)),self.row(3,45,self.now)])
        result = self.reader.read()
        self.assertEqual(2,result.sample.layer_index)
        self.assertTrue(result.is_new)

    def test_future_default_rows_do_not_become_temperature_samples(self):
        future = ["2","01/01/0001 00:00:00","01/01/0001 00:00:00","01/01/0001 00:00:00","0","",""]
        self.append([future])
        self.assertEqual(0,self.reader.read().sample.layer_index)

    def test_exact_recoating_header_required_without_substituting_other_temperature(self):
        header = [name.replace("TemperaturePostRecoating","TemperaturePostHeating") for name in HEADER]
        self.path.write_bytes(encode_rows([header,self.row(1,42,self.now)]))
        with self.assertRaises(CsvSourceError): self.reader.read()

    def test_duplicate_header_is_ambiguous(self):
        self.path.write_bytes(encode_rows([HEADER+["Id"],self.row(1,42,self.now)+["1"]]))
        with self.assertRaises(CsvSourceError): self.reader.read()

    def test_invalid_temperature_and_non_finite_values_stop_reading(self):
        for temperature in ("NaN","inf","3000","not-a-number"):
            with self.subTest(temperature=temperature):
                self.path.write_bytes(encode_rows([HEADER,self.row(1,temperature,self.now)]))
                reader = MonitoringCsvReader(self.target,roots=[self.root],log=self.log)
                with self.assertRaises(CsvSourceError):reader.read()

    def test_same_layer_correction_and_reversed_order_reject_control(self):
        for row in (self.row(1,50,self.now),self.row(0,50,self.now)):
            with self.subTest(row=row):
                self.path.write_bytes(encode_rows([HEADER,self.row(1,42,self.now-timedelta(minutes=1))]))
                reader = MonitoringCsvReader(self.target,roots=[self.root],log=self.log)
                reader.read();self.append([row])
                with self.assertRaises(CsvSourceError):reader.read()

    def test_replacement_or_truncation_does_not_replay_old_samples(self):
        first = self.reader.read()
        self.path.write_bytes(encode_rows([HEADER,self.row(1,41,self.now-timedelta(seconds=60))]))
        result = self.reader.read()
        self.assertFalse(result.is_new)
        self.assertNotEqual(first.sample.key,result.sample.key)

    def test_job_change_clears_selected_path_and_old_observation(self):
        self.reader.read()
        self.log.context = JobContext("job-2",0,self.now,0,"AUTO")
        self.reader._last_scan = 0
        result = self.reader.read()
        self.assertIsNone(result.sample)
        self.assertIsNone(result.token)

    def test_new_file_created_after_connect_can_be_a_new_observation(self):
        self.path.unlink()
        self.assertIsNone(self.reader.read().sample)
        self.path.write_bytes(encode_rows([HEADER,self.row(1,42,self.now+timedelta(seconds=1))]))
        self.reader._last_scan=0
        self.assertTrue(self.reader.read().is_new)

    def test_ambiguous_matching_csv_is_not_chosen_by_newest_mtime(self):
        other = self.root/(self.now.strftime("%Y%m%d_%H%M%S")+"_1820-layer")
        other.mkdir();(other/self.path.name).write_bytes(self.path.read_bytes())
        with self.assertRaises(CsvSourceError):self.reader.read()

    def test_unrelated_newer_job_is_not_selected(self):
        other = self.root/(self.now.strftime("%Y%m%d_%H%M%S")+"_1820-layer")
        other.mkdir();(other/"LayerDatasheet_20000101_000000.csv").write_bytes(self.path.read_bytes())
        self.assertEqual(42,self.reader.read().sample.temperature)

    def test_csv_reader_works_without_any_ui_transport(self):
        self.assertEqual(42,self.reader.read().sample.temperature)
        self.assertFalse(any(name in __import__("csv_source").__dict__ for name in ("AutomationElement","SendMessage","WindowsUiAdapter")))


class JobLogTests(unittest.TestCase):
    def test_korean_am_pm_timestamp_is_twelve_hour_clock(self):
        self.assertEqual(18,log_timestamp("2026-10-27오후060139...[BJT - AUTO]: text").hour)
        self.assertEqual(0,log_timestamp("2026-10-27오전120139...[BJT - AUTO]: text").hour)
        self.assertEqual(12,log_timestamp("2026-10-27오후120139...[BJT - AUTO]: text").hour)

    def test_log_binds_first_layer_and_retains_context_when_tail_advances(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);logs=base/"Logs";logs.mkdir()
            path=logs/"20261027_M1.log"
            path.write_text("2026-10-27오전060139...[BJT - AUTO]: AutoPrint started!\n2026-10-27오전060143...[BJT - AUTO]: Layer: 1058 -> Current Z = 1.23\n",encoding="utf-8")
            reader=JobLog(MachineWindow("M1",1,2,"fixture",str(base/"Operator.exe")))
            first=reader.read()
            self.assertEqual((1058,1058,datetime(2026,10,27,6,1,43)),(first.first_layer,first.current_layer,first.first_started))
            with path.open("a",encoding="utf-8") as stream:stream.write("unrelated\n"*30000+"2026-10-27오전060250...[BJT - AUTO]: Layer: 1059 -> Current Z = 1.2\n")
            second=reader.read()
            self.assertEqual(first.run_key,second.run_key)
            self.assertEqual(1059,second.current_layer)
            with path.open("a",encoding="utf-8") as stream:stream.write("2026-10-27오전060300...[BJT - AUTO]: AutoPrint finished!\n")
            self.assertEqual("IDLE",reader.read().state)

    def test_other_machine_log_is_not_used(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);(base/"Logs").mkdir()
            (base/"Logs/20261027_S3.log").write_text("2026-10-27오전060139...[BJT - AUTO]: AutoPrint started!\n",encoding="utf-8")
            self.assertIsNone(JobLog(MachineWindow("M1",1,2,"fixture",str(base/"Operator.exe"))).read().run_key)


if __name__=="__main__":unittest.main()
