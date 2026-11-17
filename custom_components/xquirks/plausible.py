"""Whether a value Home Assistant shows can be right at all: plausible ranges per device class and a
number's own limits. No Home Assistant imports: the inputs are the plain state and attributes of an
entity.

Only clear cases count. A range is wide on purpose: a value outside it is a wrong scale, a wrong
datapoint or a wrong sign, not a cold winter.

A choice outside a select's or an enum sensor's options never reaches the state machine: a select shows
unknown then (SelectEntity.state), an enum sensor keeps its state (SensorEntity.state raises). The checks
note a select that shows unknown right after a value came (checks.SHOWS_UNKNOWN). An enum sensor's
refused value cannot be seen in its state."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

# Device class -> (to the unit of the range, keyed by the unit HA shows), lowest, highest, the unit
# of the range as the finding names it.
_Units = dict[str | None, Callable[[float], float]]
_SAME: Callable[[float], float] = lambda value: value  # noqa: E731
TEMPERATURE: _Units = {"°C": _SAME, "°F": lambda f: (f - 32) * 5 / 9, "K": lambda k: k - 273.15}
PERCENT: _Units = {"%": _SAME}
PRESSURE: _Units = {
    "hPa": _SAME, "mbar": _SAME, "kPa": lambda v: v * 10, "Pa": lambda v: v / 100, "bar": lambda v: v * 1000,
    "inHg": lambda v: v * 33.8639, "mmHg": lambda v: v * 1.33322, "psi": lambda v: v * 68.9476,
}
# Micrograms per cubic metre as Home Assistant shows it (Greek mu, U+03BC) and as older code wrote it
# (micro sign, U+00B5).
MICROGRAMS_M3 = "\u03bcg/m\u00b3"
DENSITY: _Units = {MICROGRAMS_M3: _SAME, "\u00b5g/m\u00b3": _SAME}
RANGES: dict[str, tuple[_Units, float, float, str]] = {
    "temperature": (TEMPERATURE, -50, 150, "°C"),
    "humidity": (PERCENT, 0, 100, "%"),
    "moisture": (PERCENT, 0, 100, "%"),
    "battery": (PERCENT, 0, 100, "%"),
    "illuminance": ({"lx": _SAME}, 0, 200000, "lx"),
    "carbon_dioxide": ({"ppm": _SAME}, 0, 50000, "ppm"),
    "atmospheric_pressure": (PRESSURE, 300, 1100, "hPa"),
    "pm1": (DENSITY, 0, 2000, MICROGRAMS_M3),
    "pm25": (DENSITY, 0, 2000, MICROGRAMS_M3),
    "pm10": (DENSITY, 0, 2000, MICROGRAMS_M3),
    "voltage": ({"V": _SAME, "mV": lambda v: v / 1000, "kV": lambda v: v * 1000}, 0, 1000, "V"),
    "power_factor": ({"%": _SAME, None: lambda v: v * 100}, -100, 100, "%"),
}
# States that are no value.
NO_VALUE = ("unknown", "unavailable", "", None)


def _number(state: Any) -> float | None:
    try:
        value = float(state)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _show(value: float) -> str:
    return f"{value:g}"


def implausible(domain: str, state: Any, attributes: Mapping[str, Any]) -> str | None:
    """Why the state of an entity of this domain (sensor, number) cannot be right; None when it can be
    or when nothing here knows. In English, for the notices and the report."""
    if state in NO_VALUE:
        return None
    if domain == "select" or (domain == "sensor" and attributes.get("device_class") == "enum"):
        # A state Home Assistant shows is one of its options (module docstring).
        return None
    value = _number(state)
    if value is None:
        return None
    if domain == "number":
        low, high = _number(attributes.get("min")), _number(attributes.get("max"))
        if (low is not None and value < low) or (high is not None and value > high):
            return f"{_show(value)} is outside its own range {_show(low) if low is not None else '?'} to {_show(high) if high is not None else '?'}"
        return None
    if domain != "sensor":
        return None
    found = RANGES.get(str(attributes.get("device_class")))
    if found is None:
        return None
    units, low, high, unit = found
    convert = units.get(attributes.get("unit_of_measurement"))
    if convert is None:
        return None
    if not low <= convert(value) <= high:
        shown = attributes.get("unit_of_measurement")
        return f"{_show(value)}{' ' + shown if shown else ''} is outside {_show(low)} to {_show(high)} {unit}"
    return None
