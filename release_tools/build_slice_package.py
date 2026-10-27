"""Build a new slice package using an existing portable runtime and Windows Pillow.

Usage: python release_tools/build_slice_package.py BASE.zip OUTPUT.zip VERSION PILLOW_SOURCE
PILLOW_SOURCE is a cp314-win_amd64 Pillow wheel or a Windows Python 3.14
site-packages directory containing PIL and, if present, pillow.libs. No installation occurs.
"""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

from verify_package import MANIFEST, ROOT, SOURCES, verify

def build(base, output, version, site_packages):
    base, output, site = Path(base), Path(output), Path(site_packages)
    if output.exists():
        raise FileExistsError(f'Refusing to overwrite {output}')
    entries = {}
    with zipfile.ZipFile(base) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())) or archive.testzip() is not None:
            raise ValueError('Invalid base ZIP')
        for info in archive.infolist():
            if not info.is_dir() and (info.filename.startswith('runtime/') or info.filename == 'IRFeedback.exe'):
                entries[info.filename] = archive.read(info)
    # Replace any previously bundled Pillow and preserve its adjacent DLL directory.
    entries = {k: v for k, v in entries.items() if not (k.lower().startswith(('runtime/lib/site-packages/pil/', 'runtime/lib/site-packages/pillow.libs/')) or
               (k.lower().startswith('runtime/lib/site-packages/pillow-') and '.dist-info/' in k.lower()))}
    if site.is_file():
        if not site.name.lower().endswith('-cp314-cp314-win_amd64.whl'):
            raise ValueError('Expected a cp314-cp314-win_amd64 Pillow wheel')
        with zipfile.ZipFile(site) as wheel:
            if len(wheel.namelist()) != len(set(wheel.namelist())) or wheel.testzip() is not None:
                raise ValueError('Invalid Pillow wheel')
            for info in wheel.infolist():
                if not info.is_dir() and (info.filename.startswith(('PIL/', 'pillow.libs/')) or
                        (info.filename.lower().startswith('pillow-') and '.dist-info/' in info.filename.lower())):
                    entries['runtime/Lib/site-packages/' + info.filename] = wheel.read(info)
    else:
        directories = [site / folder for folder in ('PIL', 'pillow.libs')]
        directories.extend(site.glob('pillow-*.dist-info'))
        for directory in directories:
            if directory.exists():
                for file in directory.rglob('*'):
                    if file.is_file() and '__pycache__' not in file.parts and file.suffix.lower() != '.pyc':
                        entries['runtime/Lib/site-packages/' + file.relative_to(site).as_posix()] = file.read_bytes()
    for entry, source in SOURCES.items():
        entries[entry] = (ROOT / source).read_bytes()
    manifest = {'version': version, 'sha256': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(entries.items())}}
    entries[MANIFEST] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    # Exclusive creation prevents replacing any existing release artifact.
    created = False
    try:
        with output.open('xb') as stream:
            created = True
            with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for name, data in sorted(entries.items()):
                    archive.writestr(name, data)
        verify(output, version)
    except Exception:
        if created:
            output.unlink()
        raise
    return output

if __name__ == '__main__':
    if len(sys.argv) != 5:
        raise SystemExit(__doc__)
    print('Built and verified:', build(*sys.argv[1:]))
