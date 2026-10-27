import csv,io,math,tempfile,threading,unittest,zipfile
from pathlib import Path
from PIL import Image
from slice_data import SliceConfig, analyze, _circle

class SliceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
    def image(self,name,color=0,size=(10,10),mode='L'):
        path=self.root/name;Image.new(mode,size,color).save(path);return str(path)
    def test_exact_physical_circle_with_anisotropic_dpi(self):
        for w,h,dx,dy,r in [(11,9,25.4,50.8,2),(10,10,25.4,25.4,2),(21,31,50.8,76.2,4)]:
            box,mask,count=_circle(w,h,SliceConfig(r,dx,dy))
            expected={(x,y) for y in range(h) for x in range(w) if ((x+.5-w/2)*25.4/dx)**2+((y+.5-h/2)*25.4/dy)**2<=r*r+1e-9}
            actual={(x+box[0],y+box[1]) for y in range(mask.height) for x in range(mask.width) if mask.getpixel((x,y))}
            self.assertEqual(expected,actual);self.assertEqual(len(expected),count)
    def test_zip_and_multiselection_are_equivalent_and_holes_stay_unknown(self):
        paths=[self.image('층001.png'),self.image('층003.png',255)]
        z=self.root/'layers.zip'
        with zipfile.ZipFile(z,'w') as f:
            for p in paths:f.write(p,'한글/'+Path(p).name)
            f.writestr('__MACOSX/._001.png',b'not an image')
        a=analyze(paths,SliceConfig(2,25.4,25.4));b=analyze([z],SliceConfig(2,25.4,25.4))
        self.assertEqual(a.fingerprint,b.fingerprint)
        self.assertEqual([1,3],[r.layer for r in a.records]);self.assertEqual(0,a.features(2)['image_valid'])
        self.assertIsNone(a.features(2)['whole_coverage_pct'])
        self.assertEqual(1,a.features(2)['previous_powder_layers'])
        self.assertEqual(0,a.features(3)['previous_image_valid'])
        self.assertEqual(100,a.features(3)['center_coverage_pct'])
    def test_palette_is_rendered_and_transparent_pixels_are_blank(self):
        p=self.root/'001.tif';im=Image.new('P',(10,10),0);im.putpalette([255,255,255,0,0,0]+[0]*762);im.save(p,compression='tiff_lzw')
        d=analyze([p],SliceConfig(2,25.4,25.4));self.assertEqual(100,d.records[0].center_coverage_pct)
        p=self.root/'002.png';Image.new('RGBA',(10,10),(255,255,255,0)).save(p)
        d=analyze([p],SliceConfig(2,25.4,25.4));self.assertEqual(0,d.records[0].whole_coverage_pct)
    def test_polarity_threshold_and_mean_are_independent(self):
        p=self.image('001.png',128)
        d=analyze([p],SliceConfig(2,25.4,25.4,True,128));r=d.records[0]
        self.assertEqual(0,r.center_coverage_pct);self.assertAlmostEqual(128/255*100,r.center_intensity_pct)
        d=analyze([p],SliceConfig(2,25.4,25.4,False,0));self.assertAlmostEqual(127/255*100,d.records[0].center_intensity_pct)
    def test_high_bit_depth_and_float_images_are_not_silently_misclassified(self):
        for name,mode,value in [('001.png','I;16',32768),('002.tif','F',0.5),('003.tif','I',255)]:
            with self.subTest(mode=mode):
                path=self.image(name,value,mode=mode)
                with self.assertRaisesRegex(ValueError,'16비트 정수·실수형'):
                    analyze([path],SliceConfig(2,25.4,25.4))
    def test_layer_start_offset_and_consecutive_prior_whole_empty_layers(self):
        paths=[self.image(f'{n:03}.png',255 if n==4 else 0) for n in range(1,5)]
        d=analyze(paths,SliceConfig(2,25.4,25.4,first_layer=1189))
        self.assertEqual(3,d.features(1192)['previous_powder_layers'])
        self.assertEqual(0,d.features(1193)['previous_powder_layers'])
        self.assertEqual(1189,d.records[0].layer)
    def test_duplicate_invalid_geometry_sizes_and_multiframe_rejected(self):
        p=self.image('001.png');q=self.image('same001.png')
        for paths,c in [([p,q],SliceConfig(2,25.4,25.4)),([p],SliceConfig(50,25.4,25.4))]:
            with self.assertRaises(ValueError):analyze(paths,c)
        q=self.image('002.png',size=(12,10))
        with self.assertRaises(ValueError):analyze([p,q],SliceConfig(2,25.4,25.4))
        p=self.root/'001.tif';Image.new('L',(10,10)).save(p,save_all=True,append_images=[Image.new('L',(10,10))])
        with self.assertRaises(ValueError):analyze([p],SliceConfig(2,25.4,25.4))
    def test_cancellation_never_publishes_partial_dataset_and_csv_has_units(self):
        p=self.image('001.png');event=threading.Event();event.set()
        with self.assertRaises(InterruptedError):analyze([p],SliceConfig(2,25.4,25.4),cancel=event)
        d=analyze([p],SliceConfig(2,25.4,25.4));out=self.root/'data.csv';d.export_csv(out)
        self.assertTrue(out.read_bytes().startswith(b'\xef\xbb\xbf'))
        with out.open(encoding='utf-8-sig',newline='') as f:rows=list(csv.reader(f))
        self.assertIn('반경 mm',rows[0]);self.assertEqual('2',rows[1][8])
    def test_missing_filename_number_requires_explicit_sequence_and_bad_config_rejected(self):
        p=self.image('no_number.png')
        with self.assertRaises(ValueError):analyze([p],SliceConfig(2,25.4,25.4))
        self.assertEqual(1,analyze([p],SliceConfig(2,25.4,25.4,numbering='sequence')).records[0].layer)
        q=self.image('also_no_number.png')
        with self.assertRaisesRegex(ValueError,'층 범위'):
            analyze([p,q],SliceConfig(2,25.4,25.4,first_layer=1_000_001,numbering='sequence'))
        for kw in [dict(radius_mm=0),dict(radius_mm=1,dpi_x=float('nan')),dict(radius_mm=1,first_layer=True)]:
            with self.assertRaises(ValueError):SliceConfig(**kw)
if __name__=='__main__':unittest.main()
