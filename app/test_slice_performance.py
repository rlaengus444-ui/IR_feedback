"""Behavioral checks for bounded per-analysis content reuse."""
import io
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image, UnidentifiedImageError
import slice_data
from slice_data import SliceConfig, analyze


class SliceContentCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = SliceConfig(2, 25.4, 25.4)

    def image(self, color=0, size=(10, 10)):
        out = io.BytesIO()
        Image.new('L', size, color).save(out, format='PNG')
        return out.getvalue()

    def archive(self, entries):
        path = self.root / 'layers.zip'
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as z:
            for name, data in entries:
                z.writestr(name, data)
        return path

    def test_duplicate_contents_reuse_measurement_but_preserve_records(self):
        blank, white = self.image(), self.image(255)
        archive = self.archive([('001.png', blank), ('002.png', blank),
                                ('003.png', white), ('005.png', blank)])
        original = slice_data._measure
        with patch.object(slice_data, '_measure', wraps=original) as measure:
            result = analyze([archive], self.config)
            self.assertEqual(2, measure.call_count)
        self.assertEqual([1, 2, 3, 5], [r.layer for r in result.records])
        self.assertEqual([1, 2, 0, 1], [r.prior_powder_run for r in result.records])
        self.assertEqual(['001.png', '002.png', '003.png', '005.png'], [r.name for r in result.records])
        # A fresh analysis must never reuse another analysis's configuration or cache.
        with patch.object(slice_data, '_measure', wraps=original) as measure:
            inverted = analyze([archive], SliceConfig(2, 25.4, 25.4, False))
            self.assertEqual(2, measure.call_count)
        self.assertEqual(100, inverted.records[0].whole_coverage_pct)
        self.assertNotEqual(result.fingerprint, inverted.fingerprint)

    def test_eviction_and_invalid_images_still_fail(self):
        archive = self.archive([('001.png', self.image()), ('002.png', self.image(255)),
                                ('003.png', self.image())])
        with patch.object(slice_data, 'MAX_MEASUREMENT_CACHE_ENTRIES', 1), \
             patch.object(slice_data, '_measure', wraps=slice_data._measure) as measure:
            analyze([archive], self.config)
            self.assertEqual(3, measure.call_count)
        for bad, error in ((self.image(size=(12, 10)), ValueError),
                           (b'not an image', UnidentifiedImageError)):
            archive = self.archive([('001.png', self.image()), ('002.png', self.image()), ('003.png', bad)])
            with self.assertRaises(error):
                analyze([archive], self.config)

    def test_corrupt_duplicate_zip_member_is_fully_read(self):
        data = self.image()
        archive = self.archive([('001.png', data), ('002.png', data)])
        raw = bytearray(archive.read_bytes())
        # Alter the second local member's payload without changing its CRC.
        first = raw.find(data)
        second = raw.find(data, first + len(data))
        self.assertGreater(second, first)
        raw[second + len(data) // 2] ^= 1
        archive.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, '손상된 ZIP'):
            analyze([archive], self.config)

    def test_cancel_after_last_progress_and_byte_limit(self):
        archive = self.archive([('001.png', self.image()), ('002.png', self.image())])
        event = threading.Event()
        with self.assertRaises(InterruptedError):
            analyze([archive], self.config, cancel=event,
                    progress=lambda i, total: event.set() if i == total else None)
        with patch.object(slice_data, 'MAX_IMAGE_BYTES', len(self.image()) - 1):
            with self.assertRaises(ValueError):
                analyze([archive], self.config)

    def test_grayscale_polarity_thresholds_and_anisotropic_roi(self):
        path = self.root / '001.png'
        image = Image.new('L', (10, 10))
        image.putdata([(i * 37) % 256 for i in range(100)])
        image.save(path)
        for white in (True, False):
            for threshold in (0, 1, 127, 254):
                config = SliceConfig(2, 25.4, 50.8, white, threshold)
                box, mask, pixels = slice_data._circle(10, 10, config)
                # Reproduce the original conversion and histogram pipeline.
                from PIL import ImageOps
                signal = image.convert('L')
                if not white:
                    signal = ImageOps.invert(signal)
                full = signal.histogram()
                hist = signal.crop(box).histogram(mask)
                expected = (100. * sum(hist[threshold + 1:]) / pixels,
                            100. * sum(i * n for i, n in enumerate(hist)) / (255. * pixels),
                            100. * sum(full[threshold + 1:]) / 100)
                record = analyze([path], config).records[0]
                self.assertEqual(expected, (record.center_coverage_pct,
                                            record.center_intensity_pct,
                                            record.whole_coverage_pct))


if __name__ == '__main__':
    unittest.main()
