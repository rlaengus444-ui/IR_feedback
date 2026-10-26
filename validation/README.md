# Synthetic UI Automation validation

`full_route_probe.py` launches only `fullroutebin/FullRouteFixture.exe` and uses
the PID/HWND that this fixture writes to its own marker file. It never discovers
or selects a running BinderJet process. All settings and temperatures are fake.
The non-activating fixture is placed behind other windows at 1% opacity.

Run on Windows using the module's Python runtime:

```powershell
..\runtime\python.exe -E -s -B full_route_probe.py
..\runtime\python.exe -E -s -B full_route_probe.py --late
..\runtime\python.exe -E -s -B full_route_probe.py --bootstrap
..\runtime\python.exe -E -s -B background_route_probe.py
..\runtime\python.exe -E -s -B background_session_probe.py
```

The script finds only the known workspace `../standalone-ir/app` or packaged
`../app` / `../../app` directory. It generates complete M1/S3 field snapshots
and fake owner files in the fixture's `fullroutebin/Machines/*/Configs` folder.
No real machine configuration is copied. Each run produces a JSON result file.
The script calls the supplied product `WindowsUiAdapter` unchanged; it contains
no experimental helper replacements. The fixture uses the original application's
v4.7.2 runtime compatibility profile, a virtual grid with 104-pixel rows, and a
timer snapshot that exposes only the viewport's ±128-row cache.

Cases cover successful M1/S3 Sequence IR edits and one Save, unchanged non-IR
owner parameters, acknowledgement timeout, a commit that finishes after the
helper timeout, an interrupted textbox edit, and refusal of a pre-existing
unsaved non-IR edit while preserving it, and AUTO→IDLE immediately after the IR
textbox changes. `--late` starts at viewport 0 and tests layer 858 of 1000.

The default eight-case result is `full-route-results.json`; the late-layer result
is written separately to `late-layer-results.json`. Prototype evidence is kept
separately and is not substituted for the product-adapter results.
`--bootstrap` verifies initial WebCam pages and already-open Sequences pages with
unsaved edits; its independent report is `bootstrap-results.json`.

The fixture executable is supplied. Its C# source and explicit-item project
are also included for independent rebuilding with .NET Framework 4.8.
These tests validate the external UI route, not thermal or electrical hardware.

`background_route_probe.py` adds 15 cases. Its fixture stops snapshot refresh
when hidden or minimized, while independently changing the source temperature.
It verifies same-row temperature updates, subsequent layer receipt, background
Save, page/minimization preservation, late-layer following, pending-edit refusal,
layer advance between verification and edit, and fail-closed behavior when the
table cannot scroll. Save uses a FlatStyle.Flat button, matching the original
button's WinForms style. `background_session_probe.py` runs the actual session,
Excel controller and native adapter through three new layers while changing
tabs/minimization. It checks two initial holds, one Save on the third input,
duplicate polling without another Save, and disconnect completion for M1/S3.
