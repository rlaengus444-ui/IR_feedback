"""Compare exact analysis outputs and timings using the packaged Pillow runtime."""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import threading
import time
import types

SOURCE_ROOT = Path(__file__).resolve().parents[1]
BASELINE_SHA = 'df8381be1ec0b2b29a73ed18d39760f928bd2bcf07fc42eb5278de8fefcc1881'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package_root', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args()
    report = {'passed': False, 'no_equipment_writes': True, 'cases': {}}
    try:
        if not 1 <= args.rounds <= 5:
            raise ValueError('Use 1..5 paired rounds')
        app = (args.package_root / 'app').resolve()
        if os.name == 'nt' and Path(sys.executable).resolve() != (args.package_root / 'runtime/python.exe').resolve():
            raise ValueError('Use the actual packaged runtime')
        sys.path.insert(0, str(app))
        import slice_data as candidate
        if Path(candidate.__file__).resolve() != app / 'slice_data.py':
            raise ValueError('Loaded analysis outside selected package')
        source = (SOURCE_ROOT / 'validation/reference/slice_data_s3_1.py').read_bytes()
        if hashlib.sha256(source).hexdigest() != BASELINE_SHA:
            raise ValueError('Pinned baseline source differs')
        baseline = types.ModuleType('released_s3_1_slice_data')
        sys.modules[baseline.__name__] = baseline
        exec(compile(source, 'pinned-s3.1/slice_data.py', 'exec'), baseline.__dict__)
        import PIL
        report.update(python=sys.version, pillow=PIL.__version__, baseline_source_sha256=BASELINE_SHA,
                      candidate_source_sha256=hashlib.sha256((app/'slice_data.py').read_bytes()).hexdigest(),
                      timing_method='Paired interleaved runs; median, same ZIP bytes and configuration')
        config = candidate.SliceConfig(5, 400, 400)
        for name, layers, unique in (('duplicate', 96, 3), ('unique', 48, 48), ('palette', 24, 4)):
            path = SOURCE_ROOT / 'validation/slice-analysis-benchmark' / (name+'.zip')
            timings = {'baseline': [], 'candidate': []}
            reference = None
            for index in range(args.rounds):
                order = ('baseline', 'candidate') if index % 2 == 0 else ('candidate', 'baseline')
                for implementation in order:
                    module = baseline if implementation == 'baseline' else candidate
                    started = time.perf_counter()
                    result = module.analyze([path], config)
                    timings[implementation].append(time.perf_counter()-started)
                    value = {'fingerprint': result.fingerprint, 'records': [asdict(r) for r in result.records]}
                    if reference is None:
                        reference = value
                    if value != reference or len(value['records']) != layers:
                        raise ValueError('Record/fingerprint mismatch: '+name)
            medians = {k: statistics.median(v) for k, v in timings.items()}
            report['cases'][name] = dict(layers=layers, unique_bytes=unique, pixels=[4096, 4096],
                zip_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), exact_parity=True,
                fingerprint=reference['fingerprint'], seconds=timings, median_seconds=medians,
                speedup=medians['baseline']/medians['candidate'])

        # CPU scheduling check only: fake preview polling while real unique
        # images are analyzed. This is not a PLC or real sensor latency test.
        from adapter_contracts import MachineWindow, UiSnapshot
        from session import SessionOptions, run_ui_session
        target = MachineWindow('S3', os.getpid(), 1, 'synthetic scheduling check', '')
        stop = threading.Event()
        polls, errors = [], []
        class Adapter:
            def connect(self, *a): pass
            def prepare_mode(self, *a): pass
            def disconnect(self): pass
            def read_snapshot(self, *a):
                polls.append(time.perf_counter())
                return UiSnapshot(target, layer_index=len(polls), t_recoating_c=65.,
                    heater1_setting_percent=60., observation_key=str(len(polls)),
                    observation_is_new=True, machine_state='AUTO', minimum_poll_seconds=.1)
            def apply_ir_power(self, *a): raise AssertionError('Preview must not write')
        def work():
            try: run_ui_session(SessionOptions(target, mode='preview'), stop, lambda *a: None, Adapter)
            except Exception as error: errors.append(str(error))
        thread = threading.Thread(target=work)
        thread.start()
        try:
            candidate.analyze([SOURCE_ROOT/'validation/slice-analysis-benchmark/unique.zip'], config)
        finally:
            stop.set()
            thread.join(timeout=5)
        if thread.is_alive() or errors or len(polls) < 2:
            raise ValueError('Synthetic polling lifecycle failed: '+str(errors))
        gap = max(b-a for a, b in zip(polls, polls[1:]))
        report['synthetic_polling'] = dict(samples=len(polls), maximum_gap_seconds=gap,
                                         real_device_latency_tested=False)
        report['passed'] = (report['cases']['duplicate']['speedup'] >= 2 and
                            report['cases']['palette']['speedup'] >= 2 and
                            report['cases']['unique']['speedup'] >= 1/1.15 and gap < 1)
    except Exception as error:
        import traceback
        report.update(error=str(error), traceback=traceback.format_exc())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
