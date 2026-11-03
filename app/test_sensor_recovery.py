"""CSV invalid observations remain nonphysical and keep integrity checks."""
from datetime import timedelta
from unittest.mock import patch

from csv_source import CsvReadUnavailable, CsvSourceError
from test_csv_source import CsvSourceTests, encode_rows, HEADER


class SensorRecoveryTests(CsvSourceTests):
    def test_invalid_then_fresh_higher_layer(self):
        self.reader.read()
        self.append([self.row(2, -1, self.now)])
        invalid = self.reader.read()
        self.assertIsNone(invalid.sample.temperature)
        self.assertFalse(invalid.sample.measurement_valid)
        self.assertFalse(invalid.is_new)
        self.assertIn('-1', invalid.details)
        self.assertEqual(invalid.sample, self.reader.read().sample)
        self.append([self.row(3, 44, self.now)])
        resumed = self.reader.read()
        self.assertEqual(44, resumed.sample.temperature)
        self.assertTrue(resumed.sample.measurement_valid)
        self.assertTrue(resumed.is_new)

    def test_invalid_is_exposed_before_later_row_in_same_read(self):
        self.reader.read()
        self.append([self.row(2, -1, self.now), self.row(3, 44, self.now)])
        self.assertFalse(self.reader.read().sample.measurement_valid)
        self.assertEqual(44, self.reader.read().sample.temperature)

    def test_invalid_rows_still_validate_time_and_job(self):
        for kind in ('reversed', 'before_job', 'descending', 'same_layer'):
            with self.subTest(kind=kind):
                self.path.write_bytes(encode_rows([HEADER, self.row(1, 42, self.now)]))
                from csv_source import MonitoringCsvReader
                reader = MonitoringCsvReader(self.target, roots=[self.root], log=self.log)
                reader.read()
                row = self.row(2, -1, self.now)
                if kind == 'reversed':
                    row[3] = (self.started - timedelta(seconds=1)).strftime('%m/%d/%Y %H:%M:%S')
                elif kind == 'before_job':
                    row[1] = (self.started - timedelta(seconds=10)).strftime('%m/%d/%Y %H:%M:%S')
                elif kind == 'descending':
                    row[0] = '0'
                else:
                    row[0] = '1'
                self.append([row])
                with self.assertRaises(CsvSourceError):
                    reader.read()

    def test_os_error_is_typed_and_does_not_reset_reader(self):
        before = self.reader.read().sample
        with patch('csv_source.shared_binary', side_effect=OSError('offline')):
            with self.assertRaises(CsvReadUnavailable):
                self.reader.read()
        self.assertEqual(before, self.reader.read().sample)

    def test_incomplete_sentinel_is_not_a_measurement(self):
        baseline = self.reader.read()
        row = self.row(2, -1, self.now)
        row[3] = ''
        self.append([row])
        observed = self.reader.read()
        self.assertNotEqual(baseline.sample, observed.sample)
        self.assertFalse(observed.sample.measurement_valid)
        self.assertIsNone(observed.sample.temperature)
        self.assertIsNone(observed.sample.finished)
        self.assertIsNone(observed.token)
        self.assertFalse(observed.is_new)
