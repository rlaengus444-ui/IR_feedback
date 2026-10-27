"""Read-only plots of completed-layer observations.

IR curves are settings observed when the CSV record was received, not a
measurement of heater power. Only successful Save calls create save markers.
"""

from collections import OrderedDict
from bisect import bisect_left
from dataclasses import dataclass
import math
import tkinter as tk
from tkinter import ttk


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _power(value):
    value = _number(value)
    return value if value is not None and 0 <= value <= 100 else None


class _HoverIndex:
    """Nearest plotted observation, searched in a bounded horizontal window."""

    def __init__(self, points=()):
        self.points = tuple(sorted(points, key=lambda point: point[0]))
        self.xs = tuple(point[0] for point in self.points)

    def nearest(self, x, y, radius=10):
        at = bisect_left(self.xs, x - radius)
        best, best_distance = None, radius * radius
        lines = []
        while at < len(self.points) and self.points[at][0] <= x + radius:
            candidate = self.points[at]
            distance = (candidate[0] - x) ** 2 + (candidate[1] - y) ** 2
            if distance < best_distance or (best is None and distance <= best_distance):
                best, best_distance = candidate, distance
                lines = candidate[2].splitlines()
            elif (best is not None and distance == best_distance
                  and candidate[:2] == best[:2]):
                for line in candidate[2].splitlines():
                    if line not in lines:
                        lines.append(line)
            at += 1
        return None if best is None else (best[0], best[1], "\n".join(lines))


@dataclass(frozen=True)
class LayerPoint:
    layer: int
    temperature: float
    ir1_observed: float | None
    ir2_observed: float | None
    ir1_saved: float | None
    ir2_saved: float | None
    coupled: bool


class LayerHistory:
    """One point per observed layer, scoped to a production run."""

    def __init__(self, capacity=None):
        if capacity is not None and (not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1):
            raise ValueError("capacity must be a positive integer")
        self.capacity = capacity
        self.reset()

    def reset(self):
        self._run_key = None
        self._has_run = False
        self._points = OrderedDict()
        self._keys = {}

    def record(self, payload):
        """Return True if graph data changed; no control or I/O is performed."""
        snapshot = payload.get("snapshot")
        if snapshot is None:
            return False
        layer = snapshot.layer_index
        temperature = _number(snapshot.t_recoating_c)
        key = snapshot.observation_key
        if (not isinstance(layer, int) or isinstance(layer, bool) or layer < 0
                or temperature is None or not -273.15 <= temperature <= 2000 or key is None):
            return False
        run_key = snapshot.run_key
        if self._has_run and run_key != self._run_key:
            self.reset()
        self._run_key = run_key
        self._has_run = True
        if self._keys.get(layer) == key:
            return False
        saved = payload.get("applied")
        if not isinstance(saved, dict):
            saved = {}
        coupled = bool(snapshot.coupled_output)
        point = LayerPoint(layer + 1, temperature,
            _power(snapshot.heater1_setting_percent),
            None if coupled else _power(snapshot.heater2_setting_percent),
            _power(saved.get(1)), None if coupled else _power(saved.get(2)), coupled)
        self._points[layer] = point
        self._points.move_to_end(layer)
        self._keys[layer] = key
        while self.capacity is not None and len(self._points) > self.capacity:
            removed, _ = self._points.popitem(last=False)
            del self._keys[removed]
        return True

    @property
    def points(self):
        return tuple(self._points[layer] for layer in sorted(self._points))


class LayerCharts(ttk.Frame):
    """Two lightweight Tk canvases; call record(payload) on the Tk thread."""

    def __init__(self, parent, capacity=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.history = LayerHistory(capacity)
        self._target_temperature = None
        self._hover_indexes = {}
        self._redraw_pending = False
        self._full_window = None
        self._full_canvases = None
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        temp = ttk.LabelFrame(self, text="층별 T_recoating (°C)")
        power = ttk.LabelFrame(self, text="층별 IR 설정 (%)")
        temp.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        power.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        self.temperature_canvas = tk.Canvas(temp, height=160, background="white", highlightthickness=0)
        self.power_canvas = tk.Canvas(power, height=160, background="white", highlightthickness=0)
        self.temperature_canvas.pack(fill="both", expand=True)
        self.power_canvas.pack(fill="both", expand=True)
        self.temperature_canvas.bind("<Configure>", self._schedule_redraw)
        self.power_canvas.bind("<Configure>", self._schedule_redraw)
        for canvas in (self.temperature_canvas, self.power_canvas):
            canvas.bind("<Motion>", self._hover)
            canvas.bind("<Leave>", self._hide_tooltip)
        ttk.Label(self, text="최근 100층 표시 · IR 선: 확인된 설정 · 점: Save 성공값 (램프 실측값 아님)",
                  foreground="#536173", wraplength=750).grid(row=1, column=0, sticky="w", pady=(3, 0))
        ttk.Button(self, text="전체보기", command=self.show_all).grid(row=1, column=1, sticky="e", pady=(3, 0))
        self._schedule_redraw()

    def reset(self):
        self.history.reset()
        self._schedule_redraw()

    def record(self, payload):
        if self.history.record(payload):
            self._schedule_redraw()

    def set_target_temperature(self, value):
        """Display the currently effective target; None removes the reference."""
        if value is not None:
            value = _number(value)
            if value is None or not -273.15 <= value <= 2000:
                raise ValueError("목표 온도는 -273.15~2000°C 범위의 숫자여야 합니다.")
        if self._target_temperature != value:
            self._target_temperature = value
            self._schedule_redraw()

    def _schedule_redraw(self, _event=None):
        if not self._redraw_pending:
            self._redraw_pending = True
            self.after_idle(self._redraw)

    def _redraw(self):
        self._redraw_pending = False
        points = self.history.points
        recent = points[-100:]
        self._hover_indexes = {
            self.temperature_canvas: self._draw(self.temperature_canvas, recent, False, self._target_temperature),
            self.power_canvas: self._draw(self.power_canvas, recent, True),
        }
        if self._full_canvases is not None:
            temperature, power = self._full_canvases
            self._hover_indexes[temperature] = self._draw(temperature, points, False, self._target_temperature)
            self._hover_indexes[power] = self._draw(power, points, True)

    def show_all(self):
        """Show the complete current-run history, updating as observations arrive."""
        if self._full_window is not None:
            self._full_window.deiconify()
            self._full_window.lift()
            return
        window = tk.Toplevel(self)
        self._full_window = window
        window.title("층별 그래프 전체보기")
        window.geometry("1100x650")
        window.columnconfigure(0, weight=1)
        for row, title in enumerate(("층별 T_recoating (°C)", "층별 IR 설정 (%)")):
            window.rowconfigure(row, weight=1)
            frame = ttk.LabelFrame(window, text=title)
            frame.grid(row=row, column=0, sticky="nsew", padx=8, pady=4)
            canvas = tk.Canvas(frame, background="white", highlightthickness=0)
            canvas.pack(fill="both", expand=True)
            canvas.bind("<Configure>", self._schedule_redraw)
            canvas.bind("<Motion>", self._hover)
            canvas.bind("<Leave>", self._hide_tooltip)
            if row == 0:
                temperature = canvas
            else:
                power = canvas
        self._full_canvases = (temperature, power)
        window.protocol("WM_DELETE_WINDOW", self._close_all)
        self._schedule_redraw()

    def _close_all(self):
        window = self._full_window
        self._full_window = None
        self._full_canvases = None
        if window is not None:
            window.destroy()
        self._schedule_redraw()

    def _hide_tooltip(self, event):
        event.widget.delete("hover-tooltip")

    def _hover(self, event):
        canvas = event.widget
        canvas.delete("hover-tooltip")
        index = self._hover_indexes.get(canvas)
        if index is None:
            return
        candidate = index.nearest(event.x, event.y)
        if candidate is None:
            return
        _, _, label = candidate
        text = canvas.create_text(0, 0, text=label, anchor="nw", fill="#1e293b",
                                  font=("Segoe UI", 9), tags=("hover-tooltip", "hover-text"))
        bounds = canvas.bbox(text)
        if bounds is None:
            return
        width, height = bounds[2] - bounds[0] + 8, bounds[3] - bounds[1] + 6
        x = max(1, min(event.x + 12, canvas.winfo_width() - width - 1))
        y = max(1, min(event.y + 12, canvas.winfo_height() - height - 1))
        canvas.coords(text, x + 4, y + 3)
        canvas.create_rectangle(x, y, x + width, y + height, fill="#fffde8", outline="#7b8794",
                                tags=("hover-tooltip",))
        canvas.tag_raise(text)

    @staticmethod
    def _draw(canvas, points, power, target=None):
        canvas.delete("all")
        width, height = max(100, canvas.winfo_width()), max(60, canvas.winfo_height())
        left, right, top, bottom = 48, width - 12, 12, height - 28
        canvas.create_line(left, top, left, bottom, right, bottom, fill="#a4afbb")
        if not points and (power or target is None):
            canvas.create_text((left + right) / 2, (top + bottom) / 2,
                               text="수신된 층 데이터가 없습니다", fill="#7b8794")
            return _HoverIndex()
        first, last = (points[0].layer, points[-1].layer) if points else (0, 0)
        if power:
            low, high = 0.0, 100.0
        else:
            temperatures = [point.temperature for point in points]
            if target is not None:
                temperatures.append(target)
            low = min(temperatures)
            high = max(temperatures)
            padding = max(1.0, (high - low) * 0.1)
            low, high = low - padding, high + padding
        x = lambda layer: (left + right) / 2 if first == last else left + (layer - first) * (right - left) / (last - first)
        y = lambda value: bottom - (value - low) * (bottom - top) / (high - low)
        for value in (low, (low + high) / 2, high):
            yy = y(value)
            canvas.create_line(left, yy, right, yy, fill="#eef1f4")
            canvas.create_text(left - 5, yy, text=f"{value:g}", anchor="e", fill="#657383", font=("Segoe UI", 8))
        if points:
            canvas.create_text(left, bottom + 13, text=str(first), anchor="w", fill="#657383", font=("Segoe UI", 8))
        if points and first != last:
            canvas.create_text(right, bottom + 13, text=f"{last} 층", anchor="e", fill="#657383", font=("Segoe UI", 8))
        elif points:
            canvas.create_text(right, bottom + 13, text="층", anchor="e", fill="#657383", font=("Segoe UI", 8))
        if not power and target is not None:
            yy = y(target)
            canvas.create_line(left, yy, right, yy, fill="#27845b", dash=(4, 3), width=1)
            canvas.create_text(right - 2, yy - 3, text=f"현재 목표 {target:g} °C", anchor="se",
                               fill="#18714a", font=("Segoe UI", 8, "bold"))
        if not points:
            canvas.create_text((left + right) / 2, top + 2, anchor="n",
                               text="수신된 층 데이터가 없습니다", fill="#7b8794")
            return _HoverIndex()

        hover = []
        marker_stride = max(1, math.ceil(len(points) / 300))

        def series(attribute, color):
            # Do not join across records with missing readings.
            segment = []
            def flush():
                if len(segment) == 2:
                    px, py = segment
                    canvas.create_oval(px - 2, py - 2, px + 2, py + 2, fill=color, outline=color)
                elif len(segment) >= 4:
                    canvas.create_line(*segment, fill=color, width=2)
                segment.clear()
            for index, point in enumerate(points):
                value = getattr(point, attribute)
                if value is None:
                    flush()
                else:
                    px, py = x(point.layer), y(value)
                    segment.extend((px, py))
                    if power:
                        name = "IR 설정" if point.coupled else ("IR 1 설정" if attribute == "ir1_observed" else "IR 2 설정")
                        label = f"{point.layer}층\n{name}: {value}%"
                    else:
                        label = f"{point.layer}층\nT_recoating: {value} °C"
                    hover.append((px, py, label))
                    if len(points) > 1 and (index % marker_stride == 0 or index == len(points) - 1):
                        canvas.create_oval(px - 2, py - 2, px + 2, py + 2, fill=color, outline=color)
            flush()

        if power:
            series("ir1_observed", "#2467b2")
            series("ir2_observed", "#a44f9c")
            for point in points:
                for attribute, color in (("ir1_saved", "#ef8b26"), ("ir2_saved", "#a44f9c")):
                    value = getattr(point, attribute)
                    if value is not None:
                        px, py = x(point.layer), y(value)
                        canvas.create_oval(px - 3, py - 3, px + 3, py + 3, fill=color, outline="white")
                        name = "IR 저장값" if point.coupled else ("IR 1 저장값" if attribute == "ir1_saved" else "IR 2 저장값")
                        hover.append((px, py, f"{point.layer}층\n{name}: {value}%"))
        else:
            series("temperature", "#df6854")
        return _HoverIndex(hover)
