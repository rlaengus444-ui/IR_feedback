import tempfile
from pathlib import Path
import unittest
from PIL import Image
from layer_charts import LayerPoint
from plot_images import _segments, write_plot


class PlotImagesTests(unittest.TestCase):
    def test_full_extent_png_metadata_and_resolution(self):
        points = tuple(LayerPoint(i, 40 + i / 100, 55, None, 60 if i == 2501 else None, None, True) for i in range(1, 2502))
        with tempfile.TemporaryDirectory() as d:
            for kind in ('temperature', 'ir'):
                path = Path(d) / (kind + '.png')
                write_plot(path, points, kind, 75)
                with Image.open(path) as image:
                    image.load()
                    self.assertEqual((1600, 900), image.size)
                    self.assertAlmostEqual(150, image.info['dpi'][0], delta=.1)
                    self.assertEqual(('1', '2501', '2501', '75'), tuple(image.info[k] for k in ('first_layer','last_layer','row_count','target_at_capture')))
                    if kind == 'ir':
                        self.assertGreater(sum(1 for color in image.getdata() if color == (239,139,38)), 0)

    def test_missing_values_and_layer_gaps_break_lines(self):
        points = (LayerPoint(1, 40, 10, None, None, None, False), LayerPoint(2, 41, None, None, None, None, False), LayerPoint(3, 42, 20, None, None, None, False), LayerPoint(5, 43, 30, None, None, None, False))
        self.assertEqual([[(1, 10)], [(3, 20)], [(5, 30)]], _segments(points, 'ir1_observed'))
        self.assertEqual([], _segments(points, 'ir2_observed'))
        self.assertEqual([[(1,40), (2,41), (3,42)], [(5,43)]], _segments(points, 'temperature'))
