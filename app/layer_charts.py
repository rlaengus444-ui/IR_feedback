"""Bounded, read-only plots of completed-layer observations.

IR curves are settings observed when the CSV record was received, not a
measurement of heater power. Only successful Save calls create save markers.
"""

from collections import OrderedDict
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
    """One point per observed layer, bounded and scoped to a production run."""

    def __init__(self, capacity=2000):
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
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
        while len(self._points) > self.capacity:
            removed, _ = self._points.popitem(last=False)
            del self._keys[removed]
        return True

    @property
    def points(self):
        return tuple(self._points[layer] for layer in sorted(self._points))


class LayerCharts(ttk.Frame):
    """Two lightweight Tk canvases; call record(payload) on the Tk thread."""

    def __init__(self, parent, capacity=2000, **kwargs):
        super().__init__(parent, **kwargs)
        self.history = LayerHistory(capacity)
        self._redraw_pending = False
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
        ttk.Label(self, text="연결 후 수신한 층부터 표시 · IR 선: 확인된 설정 · 점: Save 성공값 (램프 실측값 아님)",
                  foreground="#536173", wraplength=900).grid(row=1, column=0, columnspan=2, sticky="w", pady=(3, 0))
        self._schedule_redraw()

    def reset(self):
        self.history.reset()
        self._schedule_redraw()

    def record(self, payload):
        if self.history.record(payload):
            self._schedule_redraw()

    def _schedule_redraw(self, _event=None):
        if not self._redraw_pending:
            self._redraw_pending = True
            self.after_idle(self._redraw)

    def _redraw(self):
        self._redraw_pending = False
        points = self.history.points
        self._draw(self.temperature_canvas, points, False)
        self._draw(self.power_canvas, points, True)

    @staticmethod
    def _draw(canvas, points, power):
        canvas.delete("all")
        width, height = max(100, canvas.winfo_width()), max(60, canvas.winfo_height())
        left, right, top, bottom = 48, width - 12, 12, height - 28
        canvas.create_line(left, top, left, bottom, right, bottom, fill="#a4afbb")
        if not points:
            canvas.create_text((left + right) / 2, (top + bottom) / 2,
                               text="수신된 층 데이터가 없습니다", fill="#7b8794")
            return
        first, last = points[0].layer, points[-1].layer
        if power:
            low, high = 0.0, 100.0
        else:
            low = min(point.temperature for point in points)
            high = max(point.temperature for point in points)
            padding = max(1.0, (high - low) * 0.1)
            low, high = low - padding, high + padding
        x = lambda layer: (left + right) / 2 if first == last else left + (layer - first) * (right - left) / (last - first)
        y = lambda value: bottom - (value - low) * (bottom - top) / (high - low)
        for value in (low, (low + high) / 2, high):
            yy = y(value)
            canvas.create_line(left, yy, right, yy, fill="#eef1f4")
            canvas.create_text(left - 5, yy, text=f"{value:g}", anchor="e", fill="#657383", font=("Segoe UI", 8))
        canvas.create_text(left, bottom + 13, text=str(first), anchor="w", fill="#657383", font=("Segoe UI", 8))
        if first != last:
            canvas.create_text(right, bottom + 13, text=f"{last} 층", anchor="e", fill="#657383", font=("Segoe UI", 8))
        else:
            canvas.create_text(right, bottom + 13, text="층", anchor="e", fill="#657383", font=("Segoe UI", 8))

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
            for point in points:
                value = getattr(point, attribute)
                if value is None:
                    flush()
                else:
                    segment.extend((x(point.layer), y(value)))
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
        else:
            series("temperature", "#df6854")
