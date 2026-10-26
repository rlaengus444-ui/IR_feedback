"""Explicit translation of the Notion random-80 workbook's per-layer formulas.

Inputs match columns A/B/C: one-based layer, layer temperature, and the
setting actually applied for the previous layer. The live M1 client uses
the existing T_recoating record immediately when received; no remeasurement.
No I/O, formulas from documents are never executed, and settings are explicit.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, fields, replace
import math

from target_temperature import validate_target_temperature


WORKBOOK_SHA256 = "2dbdff1057ab0a2f0bbe539b77ffb71782f10daf61f8d111236d6f5585e16a94"


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def sign(value):
    return (value > 0) - (value < 0)


@dataclass(frozen=True)
class LayerSettings:
    target_temperature_c: float
    deadband_c: float
    prediction_layers: int
    wait_layers: int
    maximum_step_percent: int
    minimum_power_percent: float
    maximum_power_percent: float
    minimum_reaction_c: float
    minimum_change_percent: float
    initial_alpha: float
    initial_beta: float
    initial_g: float
    calibrate_g: bool
    auto_alpha: bool
    auto_beta: bool
    alpha_reactions: int
    alpha_required_improvement: float
    beta_observation_layers: int
    beta_minimum_reactions: int
    beta_maximum_g_change_ratio: float
    beta_step: float
    beta_minimum: float
    beta_maximum: float
    beta_slow_ratio: float

    @classmethod
    def from_dict(cls, document):
        if not isinstance(document, dict):
            raise ValueError("settings document must be an object")
        parameters = document.get("parameters", document)
        if not isinstance(parameters, dict):
            raise ValueError("settings.parameters must be an object")
        expected = {field.name for field in fields(cls)}
        if set(parameters) != expected:
            raise ValueError(f"Settings fields missing={sorted(expected - set(parameters))}, "
                             f"unknown={sorted(set(parameters) - expected)}")
        return cls(**parameters)

    def __post_init__(self):
        flags = {"calibrate_g", "auto_alpha", "auto_beta"}
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name in flags:
                if type(value) is not bool:
                    raise ValueError(f"{field.name} must be boolean")
            elif not number(value):
                raise ValueError(f"{field.name} must be finite numeric")
        for name, minimum in (("prediction_layers", 1), ("wait_layers", 0),
                              ("maximum_step_percent", 1), ("alpha_reactions", 2),
                              ("beta_observation_layers", 6), ("beta_minimum_reactions", 1)):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if self.maximum_step_percent > 5:
            raise ValueError("maximum_step_percent must be <= 5")
        if self.deadband_c < 0 or self.minimum_reaction_c <= 0:
            raise ValueError("deadband must be >= 0 and minimum reaction > 0")
        if not 0 <= self.minimum_power_percent <= self.maximum_power_percent <= 100:
            raise ValueError("power bounds must satisfy 0 <= minimum <= maximum <= 100")
        if not 0 <= self.minimum_change_percent <= 100 or self.initial_g <= 0:
            raise ValueError("minimum change must be 0..100 and initial_g > 0")
        for name in ("initial_alpha", "initial_beta", "beta_maximum_g_change_ratio", "beta_step", "beta_slow_ratio"):
            if not 0 < getattr(self, name) <= 1:
                raise ValueError(f"{name} must be > 0 and <= 1")
        if not 0 <= self.alpha_required_improvement < 1:
            raise ValueError("alpha_required_improvement must be >= 0 and < 1")
        if not 0 < self.beta_minimum <= self.beta_maximum <= 1:
            raise ValueError("beta bounds must satisfy 0 < minimum <= maximum <= 1")
        if self.calibrate_g and self.auto_alpha and self.initial_alpha not in (0.1, 0.2, 0.3):
            raise ValueError("auto alpha requires initial_alpha 0.1, 0.2 or 0.3")
        if self.calibrate_g and self.auto_beta and not self.beta_minimum <= self.initial_beta <= self.beta_maximum:
            raise ValueError("initial_beta is outside automatic beta bounds")


@dataclass(frozen=True)
class LayerRecommendation:
    power_percent: float
    reason: str
    diagnostics: dict


class LayerAdaptiveController:
    """One instance per heater; call once per received T_recoating observation."""

    def __init__(self, settings: LayerSettings):
        self.settings = settings
        self._history = deque(maxlen=3)

    def reset(self):
        self._history.clear()

    def set_target_temperature(self, temperature_c):
        """Change the target for future observations without replaying history."""
        self.settings = replace(self.settings,
            target_temperature_c=validate_target_temperature(temperature_c))

    def process(self, layer: int, temperature_c: float, previous_actual_power: float,
                restart: bool = False) -> LayerRecommendation:
        s = self.settings
        if type(restart) is not bool:
            raise ValueError("restart must be boolean")
        if not isinstance(layer, int) or isinstance(layer, bool) or layer < 1:
            raise ValueError("layer must be a one-based positive integer")
        if not number(temperature_c):
            raise ValueError("temperature must be finite")
        if not number(previous_actual_power) or not s.minimum_power_percent <= previous_actual_power <= s.maximum_power_percent:
            raise ValueError("previous actual power is outside configured bounds")
        if restart or (self._history and layer != self._history[-1]["A"] + 1):
            self.reset()
        first = not self._history
        prev = self._history[-1] if self._history else {}
        row = {"A": layer, "B": temperature_c, "C": previous_actual_power}
        row["O"] = 1 if first else min(3, prev["O"] + 1)
        row["P"] = 0 if first else previous_actual_power - prev["C"]
        row["Q"] = 0 if first else s.wait_layers if row["P"] != 0 else max(0, prev["Q"] - 1)
        row["R"] = None if row["O"] < 3 else (temperature_c - self._history[-2]["B"]) / 2
        row["S"] = None if row["R"] is None else temperature_c + s.prediction_layers * row["R"]
        row["T"] = s.target_temperature_c - temperature_c
        row["U"] = None if row["S"] is None else s.target_temperature_c - row["S"]

        # Detect actual output changes only after three stable preceding layers.
        row["AH"] = int(not first and s.calibrate_g and row["P"] != 0 and prev["O"] == 3
                        and prev["Q"] == 0 and prev["C"] == self._history[-2]["C"] == self._history[-3]["C"]
                        and abs(row["P"]) >= s.minimum_change_percent)
        evaluation_layers = s.wait_layers + 1
        row["X"] = None
        if not first and s.calibrate_g:
            if row["P"] != 0:
                row["X"] = prev["A"] if row["AH"] else None
            elif prev["X"] is not None and layer - prev["X"] <= evaluation_layers:
                row["X"] = prev["X"]
        for column in ("Y", "Z", "AA", "AB", "AC", "AD", "AE", "AF"):
            row[column] = None
        if row["X"] is not None:
            for column, source in (("Y", "B"), ("Z", "R"), ("AA", "P"), ("AB", "C")):
                row[column] = (row[source] if column in ("AA", "AB") else prev[source]) if row["P"] != 0 else prev[column]
            row["AC"] = layer - row["X"]
            row["AD"] = row["Y"] + evaluation_layers * row["Z"]
            if row["AC"] == evaluation_layers:
                row["AE"] = temperature_c - row["AD"]
                row["AF"] = row["AE"] / row["AA"]
        row["AG"] = int(row["AF"] is not None and row["AF"] > 0 and abs(row["AE"]) >= s.minimum_reaction_c)

        # Parallel alpha candidates predict against previous G before updating G.
        for column, alpha in (("AI", s.initial_alpha), ("AJ", 0.1), ("AK", 0.2), ("AL", 0.3)):
            row[column] = (s.initial_g if first else
                           (1 - alpha) * prev[column] + alpha * row["AF"] if row["AG"] else prev[column])
        row["AM"] = 0 if first else (1 if prev["AM"] >= s.alpha_reactions else prev["AM"] + 1) if row["AG"] else prev["AM"]
        for error_column, g_column in (("AN", "AJ"), ("AO", "AK"), ("AP", "AL")):
            row[error_column] = 0 if first else prev[error_column]
            if row["AG"]:
                row[error_column] = ((0 if prev["AM"] >= s.alpha_reactions else prev[error_column])
                                     + (row["AE"] - prev[g_column] * row["AA"]) ** 2)
        errors = [row[column] for column in ("AN", "AO", "AP")]
        row["AQ"] = (1 if s.initial_alpha == 0.1 else 3 if s.initial_alpha == 0.3 else 2) if first else prev["AQ"]
        if (not first and s.calibrate_g and s.auto_alpha and row["AG"] and row["AM"] == s.alpha_reactions
                and errors[prev["AQ"] - 1] > 0
                and min(errors) <= errors[prev["AQ"] - 1] * (1 - s.alpha_required_improvement)
                and errors.count(min(errors)) == 1):
            row["AQ"] = errors.index(min(errors)) + 1
        row["J"] = (0.1, 0.2, 0.3)[row["AQ"] - 1] if s.calibrate_g and s.auto_alpha else s.initial_alpha
        row["L"] = (row[("AJ", "AK", "AL")[row["AQ"] - 1]] if s.auto_alpha else row["AI"]) if s.calibrate_g else s.initial_g
        row["AR"] = 0 if first else prev["AR"] + row["AG"]
        row["AS"] = 0 if first else int(row["J"] != prev["J"])
        row["AT"] = 0 if first else abs(row["L"] - prev["L"]) / prev["L"]
        row["AX"] = max(0, abs(row["T"]) - s.deadband_c)
        row["BK"] = 0 if first else int(abs(previous_actual_power - prev["D"]) <= 0.000001)

        # Evaluate beta only on an uninterrupted eligible block. Current AU depends
        # on current beta, therefore eligibility deliberately uses previous AU.
        row["AV"] = int(not first and s.calibrate_g and s.auto_beta and row["O"] == 3
                        and row["AR"] >= s.beta_minimum_reactions and row["AS"] == 0
                        and row["AT"] <= s.beta_maximum_g_change_ratio + 0.000000000001
                        and row["BK"] == 1 and prev["AU"] == 0)
        row["AW"] = 0
        for column in ("AY", "AZ", "BA", "BB", "BC", "BD", "BE", "BF"):
            row[column] = 0
        if row["AV"]:
            row["AW"] = 1 if prev["AW"] == 0 or prev["AW"] >= s.beta_observation_layers else prev["AW"] + 1
            block_start = row["AW"] == 1
            prior = lambda column: 0 if block_start else prev[column]
            row["AY"] = prior("AY") + (row["AX"] if row["AW"] <= 3 else 0)
            row["AZ"] = prior("AZ") + (row["AX"] if row["AW"] > s.beta_observation_layers - 3 else 0)
            row["BA"] = prior("BA") + int(row["P"] != 0)
            row["BB"] = sign(row["P"]) if row["P"] != 0 else prior("BB")
            row["BC"] = prior("BC") + int(row["AW"] > 1 and row["P"] != 0 and prev["BB"] != 0 and sign(row["P"]) != prev["BB"])
            row["BD"] = sign(row["T"]) if row["AX"] > 0 else prior("BD")
            row["BE"] = prior("BE") + int(row["AW"] > 1 and row["AX"] > 0 and prev["BD"] != 0 and sign(row["T"]) != prev["BD"])
            row["BF"] = int(prior("BF") == 1 or abs(prev["V"]) >= s.maximum_step_percent
                             or prev["D"] <= s.minimum_power_percent or prev["D"] >= s.maximum_power_percent)
        row["BJ"] = int(row["AV"] == 1 and row["AW"] == s.beta_observation_layers)
        row["BG"] = 0
        if row["BJ"]:
            if row["BE"] >= 1 or row["BC"] >= 2:
                row["BG"] = -1
            elif (row["BE"] == 0 and row["BC"] == 0 and row["AZ"] / 3 > 2 * s.deadband_c
                  and row["AZ"] >= s.beta_slow_ratio * row["AY"] and row["BA"] >= 2 and row["BF"] == 0):
                row["BG"] = 1
        row["K"] = s.initial_beta
        if not first and s.calibrate_g and s.auto_beta:
            row["K"] = min(s.beta_maximum, max(s.beta_minimum, prev["K"] + row["BG"] * s.beta_step))
        row["AU"] = 0 if first else evaluation_layers if row["AS"] == 1 or row["K"] != prev["K"] else max(0, prev["AU"] - 1)
        row["I"] = row["K"] / row["L"]
        row["V"] = 0
        if (row["Q"] == 0 and row["O"] >= 3 and abs(row["T"]) > s.deadband_c
                and abs(row["U"]) > s.deadband_c and row["T"] * row["U"] > 0):
            # Excel ROUND on this nonnegative quantity rounds .5 upward.
            magnitude = math.floor(row["I"] * (abs(row["U"]) - s.deadband_c) + 0.000000000001 + 0.5)
            row["V"] = sign(row["U"]) * min(s.maximum_step_percent, magnitude)
        row["D"] = min(s.maximum_power_percent, max(s.minimum_power_percent, previous_actual_power + row["V"]))
        row["E"] = row["D"] - previous_actual_power
        self._history.append(row)
        return LayerRecommendation(row["D"], self._reason(row), dict(row))

    def _reason(self, row):
        if row["Q"] > 0:
            return f"유지: 반응 대기 {row['Q']}층"
        if row["O"] < 3:
            return f"유지: 온도 기록 {row['O']}/3"
        if abs(row["T"]) <= self.settings.deadband_c:
            return "유지: 현재 온도 목표 근처"
        if abs(row["U"]) <= self.settings.deadband_c:
            return "유지: 예상 온도 목표 근처"
        if row["T"] * row["U"] <= 0:
            return "유지: 현재·예상 방향 다름"
        if row["V"] == 0:
            return "유지: 반올림한 조정량 0"
        if row["E"] == 0:
            return "유지: 출력 범위 도달"
        direction = "출력 올림" if row["E"] > 0 else "출력 내림"
        return direction if row["D"] == row["C"] + row["V"] else direction + ": 범위 반영"
