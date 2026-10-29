"""Pillow-only full-history plot renderer. It never calls Tk or equipment."""
import math
from PIL import Image, ImageDraw, ImageFont, PngImagePlugin


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _segments(points, field):
    """Separate lines across missing observations and nonconsecutive layers."""
    result, segment, previous = [], [], None
    for point in points:
        value = getattr(point, field)
        if not _finite(value) or (previous is not None and point.layer != previous + 1):
            if segment:
                result.append(segment)
            segment = []
        if _finite(value):
            segment.append((point.layer, value))
        previous = point.layer
    if segment:
        result.append(segment)
    return result


def _font(size):
    for name in ('segoeui.ttf', 'arial.ttf', 'DejaVuSans.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def write_plot(path, points, kind, target_temperature=None):
    if kind not in ('temperature', 'ir'):
        raise ValueError('unknown plot kind')
    points = tuple(points)
    image = Image.new('RGB', (1600, 900), 'white')
    draw = ImageDraw.Draw(image)
    title_font, font = _font(26), _font(17)
    left, top, right, bottom = 115, 115, 1540, 790
    first, last = points[0].layer, points[-1].layer
    fields = ('temperature',) if kind == 'temperature' else ('ir1_observed', 'ir2_observed')
    values = [getattr(p, f) for p in points for f in fields if _finite(getattr(p, f))]
    if kind == 'temperature' and _finite(target_temperature):
        values.append(target_temperature)
    if kind == 'ir':
        values += [getattr(p, f) for p in points for f in ('ir1_saved', 'ir2_saved') if _finite(getattr(p, f))]
        low, high = 0., 100.
    elif values:
        low, high = min(values), max(values)
        pad = max(1., (high - low) * .1)
        low, high = low - pad, high + pad
    else:
        low, high = 0., 1.
    x = lambda layer: (left + right) / 2 if first == last else left + (layer - first) * (right - left) / (last - first)
    y = lambda value: bottom - (value - low) * (bottom - top) / (high - low)
    draw.text((left, 25), 'Temperature after recoating (C)' if kind == 'temperature' else 'Observed IR settings and confirmed Save values (%)', fill='#202b39', font=title_font)
    draw.text((left, 65), f'Layers {first} - {last}  |  {len(points)} observations', fill='#39485a', font=font)
    for i in range(6):
        value = low + (high - low) * i / 5
        yy = y(value)
        draw.line((left, yy, right, yy), fill='#e5e9ef', width=1)
        draw.text((8, yy - 9), f'{value:.1f}', fill='#39485a', font=font)
    draw.line((left, top, left, bottom, right, bottom), fill='#536173', width=2)
    draw.text((left, bottom + 15), str(first), fill='#39485a', font=font)
    draw.text((right - 70, bottom + 15), str(last), fill='#39485a', font=font)
    draw.text(((left + right) / 2 - 20, 855), 'Layer', fill='#39485a', font=font)
    colors = ('#df6854',) if kind == 'temperature' else ('#2467b2', '#a44f9c')
    for field, color in zip(fields, colors):
        for segment in _segments(points, field):
            xy = [(x(layer), y(value)) for layer, value in segment]
            if len(xy) > 1:
                draw.line(xy, fill=color, width=3)
            else:
                xx, yy = xy[0]
                draw.ellipse((xx-2, yy-2, xx+2, yy+2), fill=color)
    if kind == 'temperature' and _finite(target_temperature):
        yy = y(target_temperature)
        for xx in range(left, right, 14):
            draw.line((xx, yy, min(xx+7, right), yy), fill='#27845b', width=2)
        draw.text((right - 300, top - 30), f'Target at capture: {target_temperature:g} C', fill='#27845b', font=font)
    if kind == 'ir':
        draw.text((left, 820), 'Blue/Purple: observed IR 1/2     Orange/Magenta dots: confirmed Save 1/2', fill='#39485a', font=font)
        for p in points:
            for field, color in (('ir1_saved', '#ef8b26'), ('ir2_saved', '#d52b92')):
                value = getattr(p, field)
                if _finite(value):
                    xx, yy = x(p.layer), y(value)
                    draw.ellipse((xx-5, yy-5, xx+5, yy+5), fill=color, outline='white')
    meta = PngImagePlugin.PngInfo()
    for name, value in {'plot':kind, 'first_layer':first, 'last_layer':last, 'row_count':len(points),
                        'target_at_capture':target_temperature if target_temperature is not None else ''}.items():
        meta.add_text(name, str(value))
    image.save(path, format='PNG', dpi=(150,150), pnginfo=meta)
