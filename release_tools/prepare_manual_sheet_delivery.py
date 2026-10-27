"""Package two existing XLSX and plot pinned offline trajectories; never drive equipment."""
import argparse
import csv
import hashlib
import json
import shutil
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager

ROOT = Path(__file__).resolve().parents[1]
SHEETS = (
    ('IR_슬라이싱_수동계산.xlsx', '01_IR_이미지기반_회복제어_2026.10.28.xlsx',
     'eb34cd39d364011015bee1a03cc1c024a1bb41911df35ed5aeac41b299941c20'),
    ('IR_파우더층제어_분사층고정.xlsx', '02_IR_파우더층제어_분사층출력고정_2026.10.28.xlsx',
     '46fe77b0eaf57352ffffaa3bcc06d2e3e9a45dd3c339885961ca7922aaf7d7db'),
)


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    out = args.evidence / 'delivery'
    out.mkdir(exist_ok=True)
    manifest = {'evaluation_only': True, 'new_exe_required': False,
                'actual_excel_com_tested': False, 'equipment_tested': False,
                'final_algorithm_approval': 'on_hold', 'files': []}
    for source, name, expected in SHEETS:
        data = (ROOT / 'app/templates' / source).read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise ValueError(f'Unexpected template bytes: {source}')
        (out / name).write_bytes(data)
        manifest['files'].append(dict(name=name, sha256=actual, bytes=len(data)))

    base = read_csv(args.evidence / 'simulation-v4/simulation_300.csv')
    hold = read_csv(out / 'powder_hold_closed_loop_300.csv')
    if len(base) != 300 or len(hold) != 300:
        raise ValueError('Expected exactly 300 causal observations')
    numeric = ('sensor', 'physical', 'previous_ir', 'ir', 'G', 'alpha', 'beta')
    fields = ['layer', 'binder'] + [f'{name}_{key}' for name in ('original', 'recovery', 'hold') for key in numeric]
    combined = []
    for b, h in zip(base, hold):
        if (b['layer'], b['binder']) != (h['layer'], h['binder']):
            raise ValueError('Different layer plans')
        if h['binder'] == '1' and float(h['ir']) != float(h['previous_ir']):
            raise ValueError('Binder output was not held at actual C')
        row = dict(layer=b['layer'], binder=b['binder'])
        for name, prefix in (('original', 'base'), ('recovery', 'absolute')):
            row.update({f'{name}_{key}': b[f'{prefix}_{key}'] for key in numeric})
        row.update({f'hold_{key}': h[key] for key in numeric})
        combined.append(row)
    with (out / 'IR_300층_두시트_비교.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(combined)

    fonts = [f for f in font_manager.findSystemFonts() if 'NotoSansCJK-Regular' in f]
    if fonts:
        plt.rcParams['font.family'] = font_manager.FontProperties(fname=fonts[0]).get_name()
    plt.rcParams['axes.unicode_minus'] = False
    fig, axes = plt.subplots(2, 1, figsize=(13, 8), sharex=True, gridspec_kw={'height_ratios': [1.25, 1]})
    colors = ('#8b8b8b', '#1673bc', '#d2791b')
    labels = ('기존 알고리즘', '① 이미지 기반 회복제어', '② 분사층 실제 출력 고정')
    x = list(range(1, 301))
    for ax in axes:
        for start, end in ((41, 100), (161, 220)):
            ax.axvspan(start-.5, end+.5, color='#dbe5dc', alpha=.55, zorder=0)
        ax.grid(alpha=.2)
        ax.set_xlim(1, 300)
    axes[0].axhspan(64.5, 65.5, color='#609c75', alpha=.13)
    axes[0].axhline(65, color='#397c49', linestyle='--', linewidth=1, label='목표 65°C')
    for name, color, label in zip(('original', 'recovery', 'hold'), colors, labels):
        axes[0].plot(x, [float(r[f'{name}_sensor']) for r in combined], color=color, linewidth=1.5, label=label)
        axes[1].plot(x, [float(r[f'{name}_ir']) for r in combined], color=color, linewidth=1.5)
    axes[0].set_ylabel('센서 온도 (°C)')
    axes[1].set_ylabel('권장 IR 출력 (%)')
    axes[1].set_xlabel('층 번호')
    axes[0].legend(loc='lower right', fontsize=9, ncol=2)
    fig.suptitle('300층 합성모델: 두 계산시트 비교', fontsize=17, y=.97)
    fig.text(.5, .925, '녹색 음영: 바인더 분사 계획 41–100층 · 161–220층 | 동일 초기 상태·유한 냉각·측정 잡음', ha='center', fontsize=10)
    fig.text(.5, .055, '센서 최고 온도: 기존 69.37°C / ① 66.34°C / ② 65.44°C     평균 절대 오차: 1.645 / 1.069 / 1.297°C', ha='center', fontsize=10)
    fig.text(.5, .025, '실제 장비 측정 결과가 아닌 오프라인 비교입니다. 실제 Excel COM·장비 검증 및 최종 승인은 보류되어 있습니다.', ha='center', fontsize=9, color='#555555')
    fig.subplots_adjust(top=.88, bottom=.13, hspace=.13)
    for extension in ('png', 'pdf'):
        fig.savefig(out / f'IR_300층_두시트_비교.{extension}', dpi=170)
    plt.close(fig)
    shutil.copy2(ROOT / 'docs/MANUAL_SHEETS.ko.md', out / '두_계산시트_사용방법.md')
    (out / 'manual_sheets_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
