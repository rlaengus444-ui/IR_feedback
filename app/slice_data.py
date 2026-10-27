"""Precomputed slicing-image advisory data. No device/temperature inference."""
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
import csv
import hashlib
import io
import json
import math
import re
import zipfile

EXTENSIONS = frozenset(('.png', '.bmp', '.tif', '.tiff', '.jpg', '.jpeg'))
FEATURE_NAMES = ('image_valid', 'center_coverage_pct', 'center_intensity_pct', 'whole_coverage_pct',
                 'previous_image_valid', 'previous_center_coverage_pct', 'previous_center_intensity_pct',
                 'previous_whole_coverage_pct', 'previous_powder_layers', 'radius_mm', 'dpi_x', 'dpi_y')
MAX_IMAGE_BYTES = 256 * 1024 * 1024
MAX_IMAGE_PIXELS = 120_000_000
MAX_FILES = 100_000
MAX_ARCHIVE_BYTES = 16 * 1024**3

def validate_features(features):
    """Normalize the 12 workbook inputs and validate before advancing state."""
    values = dict.fromkeys(FEATURE_NAMES)
    values.update(image_valid=0, previous_image_valid=0, previous_powder_layers=0)
    if features is not None:
        if not isinstance(features, dict) or set(features) != set(FEATURE_NAMES):
            raise ValueError("슬라이싱 입력 항목이 일치하지 않습니다.")
        values.update(features)
    for name, value in values.items():
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError("슬라이싱 입력값이 올바르지 않습니다: " + name)
        if value is None:
            continue
        if name.endswith('_pct') and not 0 <= value <= 100:
            raise ValueError("슬라이싱 비율은 0~100이어야 합니다.")
        if name in ('image_valid', 'previous_image_valid') and value not in (0, 1):
            raise ValueError("이미지 유효성은 0 또는 1이어야 합니다.")
        if name == 'previous_powder_layers' and (type(value) is not int or value < 0):
            raise ValueError("무분사 참고 층 수가 올바르지 않습니다.")
        if name in ('radius_mm', 'dpi_x', 'dpi_y') and value <= 0:
            raise ValueError("반경과 DPI는 양수여야 합니다.")
    for prefix in ('', 'previous_'):
        present = [values[prefix + name] is not None for name in ('center_coverage_pct', 'center_intensity_pct', 'whole_coverage_pct')]
        if values[prefix + 'image_valid'] not in (0, 1) or all(present) != bool(values[prefix + 'image_valid']) or (any(present) and not all(present)):
            raise ValueError("누락 이미지와 수치의 유효성이 일치하지 않습니다.")
    return values

@dataclass(frozen=True)
class SliceConfig:
    radius_mm: float
    dpi_x: float = 400.
    dpi_y: float = 400.
    white_ink: bool = True
    threshold: int = 0
    first_layer: int = 1
    numbering: str = 'filename'

    def __post_init__(self):
        for name in ('radius_mm', 'dpi_x', 'dpi_y'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(name + '는 양의 유한 숫자여야 합니다.')
        if max(self.dpi_x, self.dpi_y) > 10000:
            raise ValueError('DPI는 10000 이하여야 합니다.')
        if type(self.white_ink) is not bool or type(self.threshold) is not int or not 0 <= self.threshold <= 254:
            raise ValueError('분사 색상과 0~254의 판정 기준을 확인하세요.')
        if type(self.first_layer) is not int or not 1 <= self.first_layer <= 1_000_001:
            raise ValueError('첫 이미지 대응 층은 1 이상의 정수여야 합니다.')
        if self.numbering not in ('filename', 'sequence'):
            raise ValueError('층 번호 배정 방식을 확인하세요.')

@dataclass(frozen=True)
class SliceRecord:
    layer: int
    name: str
    center_coverage_pct: float
    center_intensity_pct: float
    whole_coverage_pct: float
    roi_pixels: int
    width: int
    height: int
    sha256: str
    prior_powder_run: int = 0

@dataclass(frozen=True)
class SliceDataset:
    config: SliceConfig
    records: tuple
    fingerprint: str

    def __post_init__(self):
        mapping = {r.layer: r for r in self.records}
        if len(mapping) != len(self.records) or not self.records:
            raise ValueError('중복되거나 비어 있는 이미지 층 정보입니다.')
        object.__setattr__(self, '_by_layer', MappingProxyType(mapping))

    def features(self, layer):
        current, previous = self._by_layer.get(layer), self._by_layer.get(layer - 1)
        c = self.config
        return dict(zip(FEATURE_NAMES, (int(current is not None),
            current.center_coverage_pct if current else None,
            current.center_intensity_pct if current else None,
            current.whole_coverage_pct if current else None,
            int(previous is not None), previous.center_coverage_pct if previous else None,
            previous.center_intensity_pct if previous else None,
            previous.whole_coverage_pct if previous else None,
            previous.prior_powder_run if previous else 0, c.radius_mm, c.dpi_x, c.dpi_y)))

    def export_csv(self, path):
        # Metadata in every row prevents losing mm/pixel assumptions on export.
        with open(path, 'w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(('층', '파일', '중앙 도포율 (%)', '중앙 평균 인쇄강도 (%)', '전체 도포율 (%)',
                             '중앙 픽셀 수', '가로 px', '세로 px', '반경 mm', 'DPI X', 'DPI Y',
                             '흰색 분사', '판정 기준', '전체 무분사 연속층', '데이터 SHA256'))
            for r in self.records:
                writer.writerow((r.layer, r.name, r.center_coverage_pct, r.center_intensity_pct,
                    r.whole_coverage_pct, r.roi_pixels, r.width, r.height, self.config.radius_mm,
                    self.config.dpi_x, self.config.dpi_y, int(self.config.white_ink), self.config.threshold,
                    r.prior_powder_run, self.fingerprint))


def _cancel(event):
    if event is not None and event.is_set():
        raise InterruptedError('슬라이싱 이미지 분석을 취소했습니다.')


def _natural(name):
    return tuple((1, int(x)) if x.isdigit() else (0, x.casefold()) for x in re.split(r'(\d+)', name))


def _mapping(names, config):
    if len(names) > MAX_FILES or not names:
        raise ValueError('분석할 이미지가 없거나 100000개를 초과했습니다.')
    names = sorted(names, key=_natural)
    if config.numbering == 'sequence':
        if config.first_layer + len(names) - 1 > 1_000_001:
            raise ValueError('이미지 대응 층 범위를 초과했습니다.')
        return [(config.first_layer + i, n) for i, n in enumerate(names)]
    indices = []
    for name in names:
        match = re.search(r'(\d+)$', PurePosixPath(name.replace('\\', '/')).stem)
        if not match:
            raise ValueError('파일명 끝의 층 번호가 없습니다. 정렬순 배정 또는 번호 파일명을 사용하세요: ' + name)
        indices.append((int(match.group()), name))
    if len({i for i, _ in indices}) != len(indices):
        raise ValueError('파일명 층 번호가 중복됩니다. 다른 작업/미리보기 이미지가 섞였는지 확인하세요.')
    low = min(i for i, _ in indices)
    mapped = sorted((config.first_layer + i - low, n) for i, n in indices)
    if mapped[-1][0] > 1_000_001:
        raise ValueError('이미지 대응 층 범위를 초과했습니다.')
    return mapped


def _circle(width, height, config):
    from PIL import Image, ImageDraw
    # Exact pixel-center test for a physical circle, even when DPI X != DPI Y.
    rx, ry = config.radius_mm * config.dpi_x / 25.4, config.radius_mm * config.dpi_y / 25.4
    if rx > width / 2 or ry > height / 2:
        raise ValueError('중앙 반경이 이미지의 실제 출력 영역을 벗어납니다.')
    x0, x1 = max(0, math.ceil(width / 2 - rx - .5)), min(width - 1, math.floor(width / 2 + rx - .5))
    y0, y1 = max(0, math.ceil(height / 2 - ry - .5)), min(height - 1, math.floor(height / 2 + ry - .5))
    mask = Image.new('L', (x1 - x0 + 1, y1 - y0 + 1), 0)
    draw = ImageDraw.Draw(mask)
    for y in range(y0, y1 + 1):
        dy = (y + .5 - height / 2) * 25.4 / config.dpi_y
        half = math.sqrt(max(0., config.radius_mm**2 - dy**2)) * config.dpi_x / 25.4
        left, right = math.ceil(width / 2 - half - .5 - 1e-10), math.floor(width / 2 + half - .5 + 1e-10)
        left, right = max(x0, left), min(x1, right)
        if left <= right:
            draw.line((left - x0, y - y0, right - x0, y - y0), fill=255)
    pixels = sum(mask.histogram()[1:])
    if not pixels:
        raise ValueError('이 반경에는 픽셀 중심이 포함되지 않습니다. 반경을 늘리세요.')
    return (x0, y0, x1 + 1, y1 + 1), mask, pixels


def _measure(data, config, geometry=None):
    from PIL import Image, ImageChops, ImageOps
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError('개별 이미지가 256 MiB를 초과했습니다.')
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        if image.mode in ('I', 'F') or image.mode.startswith('I;16'):
            raise ValueError('16비트 정수·실수형 이미지는 지원하지 않습니다. 8비트 회색조/RGB 이미지로 변환하세요.')
        if width * height > MAX_IMAGE_PIXELS or min(width, height) < 1:
            raise ValueError('이미지 픽셀 수가 허용 범위를 벗어났습니다.')
        if getattr(image, 'n_frames', 1) != 1:
            raise ValueError('여러 페이지 이미지는 층별 단일 이미지로 분리해서 넣으세요.')
        if geometry is None:
            geometry = ((width, height), *_circle(width, height, config))
        elif geometry[0] != (width, height):
            raise ValueError('슬라이싱 이미지의 픽셀 크기가 서로 다릅니다.')
        alpha = None
        if 'A' in image.getbands() or 'transparency' in image.info:
            rgba = image.convert('RGBA')
            signal, alpha = rgba.convert('L'), rgba.getchannel('A')
        else:
            signal = image.convert('L')
        if not config.white_ink:
            signal = ImageOps.invert(signal)
        if alpha is not None:
            signal = ImageChops.multiply(signal, alpha)
        full = signal.histogram()
        _, box, mask, pixels = geometry
        hist = signal.crop(box).histogram(mask)
        center = 100. * sum(hist[config.threshold + 1:]) / pixels
        mean = 100. * sum(i * n for i, n in enumerate(hist)) / (255. * pixels)
        total = 100. * sum(full[config.threshold + 1:]) / (width * height)
        return (center, mean, total, pixels, width, height), geometry


def analyze(paths, config, *, cancel=None, progress=None):
    """ZIP or explicit file list. One image in memory; publish only a full dataset."""
    paths = tuple(map(str, paths))
    if not paths:
        raise ValueError('ZIP 또는 이미지 파일을 선택하세요.')
    archive = None
    try:
        if len(paths) == 1 and Path(paths[0]).suffix.lower() == '.zip':
            archive = zipfile.ZipFile(paths[0])
            infos = archive.infolist()
            if len(infos) > MAX_FILES * 2 or sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES:
                raise ValueError('ZIP의 파일 수 또는 전체 해제 크기가 너무 큽니다.')
            selected = {}
            for info in infos:
                name = info.filename.replace('\\', '/')
                parts = PurePosixPath(name).parts
                if info.is_dir() or '__MACOSX' in parts or PurePosixPath(name).name.startswith('._'):
                    continue
                if PurePosixPath(name).suffix.lower() not in EXTENSIONS:
                    continue
                if name in selected or '..' in parts or name.startswith('/') or info.flag_bits & 1:
                    raise ValueError('중복·잘못된 경로·암호화 이미지가 있는 ZIP입니다.')
                if info.file_size > MAX_IMAGE_BYTES:
                    raise ValueError('ZIP 안의 이미지가 256 MiB를 초과했습니다.')
                selected[name] = info
            names = list(selected)
            def read(name):
                with archive.open(selected[name]) as stream:
                    return stream.read(MAX_IMAGE_BYTES + 1)
        else:
            if any(Path(p).suffix.lower() not in EXTENSIONS for p in paths):
                raise ValueError('ZIP 하나 또는 PNG/BMP/TIF/TIFF/JPEG 파일을 선택하세요.')
            resolved = [str(Path(p).resolve()) for p in paths]
            if len(set(resolved)) != len(resolved):
                raise ValueError('같은 이미지 파일이 중복 선택되었습니다.')
            names = resolved
            def read(name):
                with open(name, 'rb') as stream:
                    return stream.read(MAX_IMAGE_BYTES + 1)
        mapped = _mapping(names, config)
        records = []; geometry = None; previous_layer = None; powder_run = 0
        digest = hashlib.sha256(json.dumps(vars(config), sort_keys=True).encode())
        for index, (layer, name) in enumerate(mapped):
            _cancel(cancel)
            data = read(name); values, geometry = _measure(data, config, geometry)
            _cancel(cancel)
            if layer != (previous_layer or 0) + 1:
                powder_run = 0
            powder_run = powder_run + 1 if values[2] == 0 else 0
            file_hash = hashlib.sha256(data).hexdigest()
            digest.update(json.dumps([layer, PurePosixPath(name.replace('\\', '/')).name, file_hash]).encode())
            records.append(SliceRecord(layer, PurePosixPath(name.replace('\\', '/')).name, *values, file_hash, powder_run))
            previous_layer = layer
            if progress:
                progress(index + 1, len(mapped))
        return SliceDataset(config, tuple(records), digest.hexdigest())
    except zipfile.BadZipFile as exc:
        raise ValueError('손상된 ZIP 파일입니다.') from exc
    finally:
        if archive is not None:
            archive.close()
