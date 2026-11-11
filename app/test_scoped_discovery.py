import unittest
from adapter_contracts import ReadTransportError
from windows_ui_adapter import WindowsUiAdapter
from test_resilient_adapter import TARGET,NEW

class ScopedDiscoveryTests(unittest.TestCase):
    def test_rebind_passes_exact_selected_identity_to_native_inventory(self):
        requests=[]
        def runner(request,event):
            requests.append(request)
            return {'success':True,'complete':True,'windows':[dict(machine=NEW.machine,pid=NEW.pid,hwnd=NEW.hwnd,title=NEW.title,image_path=NEW.image_path)]}
        adapter=WindowsUiAdapter(runner=runner)
        self.assertEqual([NEW],adapter.discover_matching(TARGET))
        self.assertEqual(TARGET.image_path,requests[0]['expected_image_path'])
        self.assertEqual(TARGET.machine,requests[0]['expected_machine'])
    def test_incomplete_native_inventory_never_selects_partial_candidate(self):
        for complete in (False,None,0,'false'):
            adapter=WindowsUiAdapter(runner=lambda request,event:{'success':True,'complete':complete,'windows':[]})
            with self.assertRaises(ReadTransportError):adapter.discover_matching(TARGET)
        adapter=WindowsUiAdapter(runner=lambda request,event:{'success':True,'windows':[]})
        with self.assertRaises(ReadTransportError):adapter.discover_matching(TARGET)

if __name__=='__main__':unittest.main()
