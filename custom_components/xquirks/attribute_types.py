"""The types of the attributes a quirk file's entities compute with where the guard cannot see them, checked inside
Home Assistant.

ZHA computes an entity's state from its attribute's value while it adds the device's entities, also at every start
inside load_devices, where nothing catches an error: a sensor's `value /= divisor` and `value *= multiplier`, a
number's `value * multiplier`, an enum entity's `EnumClass(value)`. zigpy converts what a device reports or answers
to the attribute's type, so the cache holds values of that type. The guard checks the type where the file defines the
attribute (guard.QuirkFile.entity_value). Where the definition comes from a class the file imports (zigpy's class of a
standard cluster, a zhaquirks class, ZHA's class the file builds on), the guard hands an AttributeNeed here
(guard.attribute_needs). This check looks the name up in the nested AttributeDefs of each class it may come from,
among the modules ZHA has loaded, and refuses the file the way the guard does, with a reason, unless one of them
defines it (or the need is optional: a class that may stand in the place of the one the entity names, where a missing
name only leaves the entity without a value) and each type found fits:

- for a divisor or a multiplier: a number type (a subclass of int or float). Text or bytes would make ZHA's
  formatter raise.
- for an enum entity: an unsigned zigpy integer type (_signed False) of at most the enum's bits (_bits). A zigpy enum
  takes every integer of its width, an unknown one as an undefined member, and raises for anything else (a negative
  or wider integer, a float that is no member's value, text).

Like inherit.py it imports nothing: a module ZHA has not loaded, or is loading, raises inherit.NotLoaded (no verdict,
the check runs again later). It runs in an executor job, never in the event loop. Any other error of it refuses the
file, none ends the check.
"""

from __future__ import annotations

from typing import Any

from .guard import AttributeNeed
from .inherit import MAX_REASON, NotLoaded, loaded


def problems(needs: list[AttributeNeed]) -> tuple[str, ...]:
    """Why a file whose entities compute with these attributes (guard.attribute_needs) is refused, one reason per
    entity; empty when every type fits."""
    out = []
    for need in needs:
        try:
            reason = _reason(need)
        except NotLoaded:
            raise
        except Exception as err:  # noqa: BLE001 - a type the check cannot judge refuses the file, never a crash
            reason = f"cannot be checked: {type(err).__name__}: {err}"
        if reason:
            out.append(f"line {need.line}: {need.method} on {need.owner}.AttributeDefs.{need.attribute}: {reason}"[:MAX_REASON])
    return tuple(out)


def fits(kind: Any, bits: int | None) -> bool:
    """Whether every value of an attribute of this type fits: a number for a divisor or a multiplier (bits None), else an
    integer an enum of these bits takes."""
    if not isinstance(kind, type):
        return False
    if bits is None:
        return issubclass(kind, (int, float))
    return issubclass(kind, int) and getattr(kind, "_signed", True) is False and getattr(kind, "_bits", bits + 1) <= bits


def _reason(need: AttributeNeed) -> str | None:
    found = []
    for module_name, name in need.candidates:
        definition = getattr(getattr(getattr(loaded(module_name), name, None), "AttributeDefs", None), need.attribute, None)
        if definition is not None:
            found.append(definition.type)
    if not found:
        # A class that may stand in the place of the one the entity names: without the name ZHA shows no value.
        return None if need.optional else f"none of {', '.join(name for _, name in need.candidates)} defines it"
    what = "a number type" if need.bits is None else f"an unsigned integer type of at most {need.bits} bits"
    for kind in found:
        if not fits(kind, need.bits):
            return f"of the type {getattr(kind, '__name__', repr(kind))}, while ZHA computes with it: the type must be {what}"
    return None
