"""Configuration and validation for the finite image-corroborated limiter."""
from dataclasses import dataclass, fields
import math


@dataclass(frozen=True)
class ImageGuardSettings:
    enabled: bool = True
    drop_c: float = 4
    prior_stability_c: float = 1
    hold_observations: int = 2
    window_observations: int = 24
    positive_limit_pp: float = 1
    recovery_band_c: float = 0.5
    recovery_step_c: float = 0.25
    recovery_observations: int = 3
    exclude_print_learning_after_event: bool = False
    absolute_recovery: bool = True
    forecast_layers: float = 8
    negative_limit_pp: float = 2
    maximum_recovery_k: float = 2
    residual_after_observations: int = 12
    integral_time_layers: float = 30
    maximum_recovery_observations: int = 120
    handoff_observations: int = 12
    exit_observations: int = 5
    stable_ir_step_pp: float = 1

    @classmethod
    def from_dict(cls, document):
        value = document.get('image_guard', {})
        if not isinstance(value, dict):
            raise ValueError('image_guard must be an object')
        if set(value) - {f.name for f in fields(cls)}:
            raise ValueError('unknown image_guard setting')
        return cls(**value)

    def __post_init__(self):
        if any(type(getattr(self, name)) is not bool for name in ("enabled", "exclude_print_learning_after_event", "absolute_recovery")):
            raise ValueError('image guard flags must be boolean')
        for name in ('hold_observations', 'window_observations', 'recovery_observations', 'residual_after_observations', 'maximum_recovery_observations', 'handoff_observations', 'exit_observations'):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(name + ' must be a positive integer')
        if self.hold_observations > self.window_observations:
            raise ValueError('hold_observations exceeds window_observations')
        for name in ('drop_c', 'prior_stability_c', 'positive_limit_pp', 'recovery_band_c', 'recovery_step_c', 'forecast_layers', 'negative_limit_pp', 'maximum_recovery_k', 'integral_time_layers', 'stable_ir_step_pp'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(name + ' must be finite and nonnegative')
        if self.maximum_recovery_observations < self.hold_observations or self.residual_after_observations > self.maximum_recovery_observations:
            raise ValueError('recovery observation limits are inconsistent')
        if self.negative_limit_pp <= 0 or self.negative_limit_pp > 5 or self.maximum_recovery_k <= 0 or self.integral_time_layers <= 0 or self.forecast_layers <= 0:
            raise ValueError('recovery gains and limits must be positive; negative limit <= 5')
        if self.drop_c <= 0 or self.positive_limit_pp > 5:
            raise ValueError('drop_c must be positive and positive_limit_pp <= 5')


def classify(features, previous=False):
    prefix = 'previous_' if previous else ''
    valid = features.get(prefix + 'image_valid')
    coverage = features.get(prefix + 'whole_coverage_pct')
    if valid not in (0, 1) or isinstance(valid, bool):
        raise ValueError('image validity must be 0 or 1')
    if coverage is not None and (isinstance(coverage, bool) or not isinstance(coverage, (int, float)) or not math.isfinite(coverage) or not 0 <= coverage <= 100):
        raise ValueError('image coverage must be 0..100')
    if valid == 1 and coverage is None:
        raise ValueError('valid image requires coverage')
    return -1 if valid == 0 else int(coverage > 0)
