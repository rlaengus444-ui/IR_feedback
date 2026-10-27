"""Offline reference for powder-only adaptation and binder/unknown holds."""
from dataclasses import replace
from pathlib import Path
import sys
APP = Path(__file__).resolve().parents[1] / 'app'
if str(APP) not in sys.path: sys.path.insert(0, str(APP))
from layer_algorithm import LayerAdaptiveController, LayerRecommendation, number
from image_guard import classify
from slice_data import validate_features
from target_temperature import validate_target_temperature

class PowderHoldReference:
    def __init__(self, settings):
        self.settings = settings
        self.controller = None
        self.last_layer = None
        self.phase = -1
        self.anchor = None
        self.verified = 0
        self.segment = None
        self.last = None

    def reset(self):
        self.controller = self.last_layer = self.last = self.segment = None
        self.phase = -1; self.anchor = None; self.verified = 0

    def set_target_temperature(self, value):
        self.settings = replace(self.settings, target_temperature_c=validate_target_temperature(value))
        if self.controller is not None: self.controller.set_target_temperature(value)

    def process(self, layer, temperature_c, previous_actual_power, restart=False, *, slice_features=None):
        if type(restart) is not bool or type(layer) is not int or layer < 1:
            raise ValueError('invalid layer or restart')
        if not number(temperature_c) or temperature_c == -1:
            raise ValueError('invalid temperature')
        if not number(previous_actual_power) or not self.settings.minimum_power_percent <= previous_actual_power <= self.settings.maximum_power_percent:
            raise ValueError('invalid actual power')
        values = validate_features(slice_features)
        phase = classify(values)
        fresh_job = restart or self.last_layer is None or layer != self.last_layer + 1
        prior_phase = -1 if fresh_job else self.phase
        boundary = fresh_job or phase != 0 or prior_phase != 0
        if fresh_job:
            segment = (self.settings.initial_g, self.settings.initial_alpha, self.settings.initial_beta)
        elif boundary:
            segment = (self.last['L'], self.last['J'], self.last['K'])
        else:
            segment = self.segment
        if boundary:
            settings = replace(self.settings, initial_g=segment[0], initial_alpha=segment[1], initial_beta=segment[2])
            controller = LayerAdaptiveController(settings)
        else:
            controller = self.controller
        result = controller.process(layer, temperature_c, previous_actual_power)
        d = dict(result.diagnostics)
        if phase == 1:
            anchor = previous_actual_power
            verified = int(not fresh_job and (prior_phase == 0 or
                (prior_phase == 1 and self.verified and abs(previous_actual_power - self.anchor) <= 1e-6)))
        else:
            anchor, verified = None, 0
        if phase != 0:
            d['D'] = previous_actual_power; d['E'] = 0
            reason = '분사층: 현재 IR 고정' if phase == 1 else '이미지 미확인: 현재 IR 고정'
        else: reason = result.reason
        d.update(CD=phase, CE=prior_phase, CF=anchor, CG=verified,
                 CH=segment[0], CI=segment[1], CJ=segment[2], CK=1)
        self.controller, self.last_layer, self.phase = controller, layer, phase
        self.anchor, self.verified, self.segment, self.last = anchor, verified, segment, d
        return LayerRecommendation(d['D'], reason, d)
