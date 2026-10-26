# CSV transport validation

This fixture never launches or selects a real BinderJet process. It generates
fake M1/S3 settings, fake machine logs and fake Monitoring CSV rows, and uses
only the PID/HWND written by its own executable.

Run from this directory with the bundled runtime:

```powershell
..\runtime\python.exe -E -s -B csv_route_probe.py
```

The actual session, Excel algorithm, CSV reader and native Sequence adapter are
used unchanged. The source factory only points the reader at the fixture's
own Monitoring directory. A 1,820-row virtual Process table rejects and records
WM_GETOBJECT, scrolling, and Process menu navigation. All 18 scenarios require
zero Process table accesses.

Cases include M1/S3 WebCam/Sequences while normal/minimized, a partial CSV row,
connection while minimized, delayed/ambiguous Save, interrupted edit, AUTO to
IDLE after edit, dirty settings, a late layer, first CSV creation after connect,
and a UI stall while the CSV reader keeps receiving data and IR is suspended.

Every normal case checks two initial holds, one Save on the third new CSV row,
no repeated Save for duplicate polling, unchanged non-IR parameters and
disconnect completion. These are synthetic transport tests, not hardware or
thermal-response validation.
