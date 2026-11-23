"""xquirks loader: the device definitions of definitions.jsonl (FORMAT.md) in ZHA's quirk registry.

The stub in ZHA's custom quirks folder calls register() while ZHA imports its custom quirks. build() builds a
definition with the real QuirkBuilder or TuyaQuirkBuilder. Its entry gets the stub's path as source (zhaquirks.setup
purges it like any custom quirk), the label "xquirks:<id> <rev>" and the filter StepBack. register() builds alias
definitions at once and registers a stand-in for every other one, built when ZHA first tries it for a device
(OnDemand). A definition that fails is skipped with one log line. check() builds all at once, compares each rev with
revision() and checks the attributes ZHA reads and computes with (CI). ZHA imports the custom quirks in an executor job,
matching and building run in the event loop: only the import reads files.

Imports only the standard library, zigpy, zha and zhaquirks. Modules that register quirks when imported come from
sys.modules, where zhaquirks.setup put them before the custom quirks (_NAMES, _inherited).
"""

from __future__ import annotations

import collections.abc
import contextlib
import dataclasses
import datetime
import enum
import functools
import hashlib
import inspect
import io
import json
import keyword
import logging
import operator
import os
import pathlib
import re
import sys
import threading
import types
import zlib

from zha.application.platforms.binary_sensor import IASZone
from zha.quirks import DEVICE_REGISTRY, DeviceMatch, DeviceRegistry, ModelInfo, QuirkRegistryEntry, QuirkSource
import zigpy.device
import zigpy.profiles.zha
import zigpy.types as t
import zigpy.zcl
from zigpy.zcl import ClusterType, foundation
import zigpy.zcl.clusters

import zhaquirks
import zhaquirks.builder
from zhaquirks.builder import EntityFilter, QuirkBuilder, ReportingConfig
from zhaquirks.clusters import CustomCluster
import zhaquirks.const
from zhaquirks.tuya.builder import TuyaQuirkBuilder
from zhaquirks.tuya.mcu import DPToAttributeMapping, TuyaMCUCluster
import zhaquirks.xiaomi

_LOGGER = logging.getLogger("xquirks")

DEFINITIONS_FILE = "definitions.jsonl"
OPTIONS_FILE = "xquirks_options.json"
LABEL_PREFIX = "xquirks:"
# zhaquirks' folder as imported and as resolved: a file is ZHA's when its path lies in either (_is_shipped).
_SHIPPED = tuple({os.path.dirname(os.path.normpath(zhaquirks.__file__)) + os.sep, str(pathlib.Path(zhaquirks.__file__).resolve().parent) + os.sep})


class DefinitionError(Exception):
    """A definition the loader refuses."""


# The fixed helpers converters use, once for all definitions: the texts of fixed_code.json while today's files exist
# (test_fixed_code_is_the_loaders).


def _be(data, start: int, width: int) -> int:
    """The unsigned big-endian number in data[start:start + width] of a Tuya RAW datapoint."""
    return int.from_bytes(bytes(data[start : start + width]), "big")


def _power_24(value: int) -> int:
    """24-bit power as z2m decodes it: 0x800000 to 0x99999a count down from 0x99999a (an
    offset encoding), values above are two's complement."""
    if value > 0x99999A:
        return value - 0x1000000
    if value >= 0x800000:
        return value - 0x99999A
    return value


def _zigbee_local_time(value: int):
    """The instant of a device clock value: seconds since 2000-01-01 00:00 in the local time the
    coordinator gives the device, read in the time zone of Home Assistant's process."""
    import datetime

    return (datetime.datetime(2000, 1, 1) + datetime.timedelta(seconds=value)).astimezone(datetime.UTC)


def _none_on_error(converter):
    """The converter, returning None where it raises: ZHA and zhaquirks call some converters where
    nothing catches an error (an entity's state while ZHA loads its devices). One value a converter
    cannot take would stop ZHA for every device. The entity shows no value instead."""

    def convert(value):
        try:
            return converter(value)
        except Exception:
            return None

    return convert


def _value_error_on_error(converter):
    """The Tuya converter, raising ValueError where it raises: zhaquirks skips a datapoint with a
    ValueError (a warning, the attribute keeps its value, the frame's other datapoints still count).
    Any other error would end the whole frame and reach the radio library."""

    def convert(value):
        try:
            return converter(value)
        except Exception as exc:
            raise ValueError(repr(exc)) from exc

    return convert


# The fixed behaviours of cluster classes, once for all definitions (their methods: fixed_code.json, as above). A
# definition names the Tuya cluster class below as replacement_cluster and puts the Aqara mixins in front of
# its cluster's bases ("behaviours").


class TypedTuyaCluster(TuyaMCUCluster):
    """The Tuya cluster of the file: every datapoint value is checked against its attribute's type before zhaquirks stores it."""

    def _dp_2_attr_update(self, datapoint):
        """Each value of the datapoint checked against the type of the attribute it goes to, then zhaquirks' update.

        ZHA computes entity states from these attributes while it loads its devices, where nothing catches an
        error: text where a number is meant (a STRING or RAW datapoint) would stop ZHA at every start, so would a
        number its formatter cannot take (Illuminance raises 10 to the value, a UTCTime adds it to a date). A
        value for an attribute of a number type must be a number its type holds. ZHA looks the value of an enum, a
        bitmap or a Bool up, so there it must be an int that type holds. A plain integer type of another cluster
        holds its range: ZHA's own entities read such an attribute as its ZCL type, as zigpy stores a ZCL report.
        On this cluster (the file's entities read it with plain arithmetic) it holds what its bits hold read
        signed or unsigned: a VALUE datapoint is signed on the wire, a sensor typed unsigned may read below zero.
        A float type holds what a single precision float holds. Anything else raises ValueError, as does any
        error of a converter: zhaquirks skips the datapoint with a warning, the attributes keep their values, the
        frame's other datapoints still count. zhaquirks' update runs the converters again (a converter holds no
        state).
        """
        try:
            endpoint = self.endpoint
            for mapped in self._dp_to_attributes.get(datapoint.dp, ()):
                if mapped.endpoint_id:
                    endpoint = self.endpoint.device.endpoints[mapped.endpoint_id]
                value = datapoint.data.payload
                if mapped.converter:
                    value = mapped.converter(value)
                cluster = getattr(endpoint, mapped.ep_attribute)
                kind = getattr(cluster.attributes_by_name.get(mapped.attribute_name), "type", None)
                if value is None or not isinstance(kind, type) or not issubclass(kind, (int, float)):
                    continue
                looked_up = hasattr(kind, "__members__")
                low, high = -(2.0 ** 128), 2.0 ** 128
                if issubclass(kind, int) and not looked_up:
                    low, high = (-(2 ** (kind._bits - 1)), 2 ** kind._bits) if cluster is self else (kind.min_value, kind.max_value + 1)
                if not isinstance(value, (int, float)) or not low <= value < high or looked_up and isinstance(value, float):
                    raise TypeError(f"{type(value).__name__} {value!r:.40} for {mapped.attribute_name} ({kind.__name__})")
                if looked_up:
                    kind(value)
        except Exception as exc:
            raise ValueError(repr(exc)) from exc
        super()._dp_2_attr_update(datapoint)


class AqaraStruct:
    """Only the Aqara struct values whose cluster the endpoint has."""

    # The struct keys of zhaquirks' Xiaomi base and the endpoint cluster it writes each one to.
    STRUCT_TARGETS = {
        "battery_voltage_mV": "power",
        "battery_percentage": "power",
        "temperature_measurement": "temperature",
        "humidity_measurement": "humidity",
        "pressure_measurement": "pressure",
        "pressure_measurement_precision": "pressure",
        "power": "electrical_measurement",
        "voltage": "electrical_measurement",
        "consumption": "smartenergy_metering",
        "illuminance_measurement": "illuminance",
        "tvoc_measurement": "voc_level",
        "smoke": "ias_zone",
    }

    def _parse_aqara_attributes(self, value):
        """Only the struct values whose cluster the endpoint has: the base class stops at a missing one."""
        attributes = super()._parse_aqara_attributes(value)
        return {key: v for key, v in attributes.items() if key not in self.STRUCT_TARGETS or hasattr(self.endpoint, self.STRUCT_TARGETS[key])}


class AqaraStructTags:
    """AqaraStruct with z2m's struct tags of the model (the cluster's STRUCT_TAGS)."""

    STRUCT_TARGETS = AqaraStruct.STRUCT_TARGETS
    STRUCT_TAGS: dict = {}

    def _parse_aqara_attributes(self, value):
        """Only the struct values whose cluster the endpoint has: the base class stops at a missing one."""
        attributes = super()._parse_aqara_attributes(value)
        for tag, (key, factor) in self.STRUCT_TAGS.items():
            if f"0xff01-{tag}" in attributes:
                attributes[key] = attributes.pop(f"0xff01-{tag}") * factor
        return {key: v for key, v in attributes.items() if key not in self.STRUCT_TARGETS or hasattr(self.endpoint, self.STRUCT_TARGETS[key])}


class AqaraCurrent:
    """The Aqara struct's current as rms_current."""

    def _handle_attribute_event(self, event):
        """The base has no key for the current: rms_current in mA (the cluster divides by 1000)."""
        super()._handle_attribute_event(event)
        if event.attribute_id == 0x00F7 and hasattr(self.endpoint, "electrical_measurement"):
            current = self._parse_aqara_attributes(event.value).get("current")
            if current is not None:
                self.endpoint.electrical_measurement.update_attribute(ElectricalMeasurement.AttributeDefs.rms_current.id, round(current * 1000))


class AqaraOutage:
    """The Aqara struct's tag 5 as z2m's power outage count."""

    def _handle_attribute_event(self, event):
        """The base keeps struct tag 5 to itself: z2m's power outage count is the value minus one."""
        super()._handle_attribute_event(event)
        if event.attribute_id != 0x00F7:
            return
        attributes = self._parse_aqara_attributes(event.value)
        tag_5 = attributes.get("X-attrib-5", attributes.get("power_outage_count"))
        if tag_5 is not None and hasattr(self.endpoint, "aqara_struct_outage"):
            self.endpoint.aqara_struct_outage.update_attribute(0x0000, tag_5 - 1)


class AqaraCurrentOutage:
    """AqaraCurrent and AqaraOutage in one handler."""

    def _handle_attribute_event(self, event):
        """The base has no key for these: rms_current in mA (the cluster divides by 1000), struct tag 5 as z2m's power outage count (the value minus one)."""
        super()._handle_attribute_event(event)
        if event.attribute_id != 0x00F7:
            return
        attributes = self._parse_aqara_attributes(event.value)
        current = attributes.get("current")
        if current is not None and hasattr(self.endpoint, "electrical_measurement"):
            self.endpoint.electrical_measurement.update_attribute(ElectricalMeasurement.AttributeDefs.rms_current.id, round(current * 1000))
        tag_5 = attributes.get("X-attrib-5", attributes.get("power_outage_count"))
        if tag_5 is not None and hasattr(self.endpoint, "aqara_struct_outage"):
            self.endpoint.aqara_struct_outage.update_attribute(0x0000, tag_5 - 1)


ElectricalMeasurement = zigpy.zcl.clusters.homeautomation.ElectricalMeasurement

# A cluster's "behaviours" in a definition: the mixins in front of its bases (struct parsing first).
BEHAVIOURS = {
    "aqara_struct": AqaraStruct,
    "aqara_struct_tags": AqaraStructTags,
    "aqara_current": AqaraCurrent,
    "aqara_outage": AqaraOutage,
    "aqara_current_outage": AqaraCurrentOutage,
}

# The names a definition may refer to outside its own classes, each with its module, taken from sys.modules
# (zhaquirks.tuya.tuya_motion, zhaquirks.tuya.tuya_trv and zhaquirks.xiaomi.aqara.opple_remote register quirks
# when imported: never imported here).
_NAMES: dict[str, str] = {
    "t": "zigpy.types",
    "foundation": "zigpy.zcl.foundation",
    **dict.fromkeys(("ZCLAttributeDef", "ZCLCommandDef", "BaseAttributeDefs", "BaseCommandDefs", "Direction", "DataTypeId"), "zigpy.zcl.foundation"),
    "DeviceType": "zigpy.profiles.zha",
    "RunningState": "zigpy.zcl.clusters.hvac",
    "IASZone": "zha.application.platforms.binary_sensor",
    **dict.fromkeys(
        (
            "EntityPlatform", "EntityType", "SensorStateClass", "SensorDeviceClass", "BinarySensorDeviceClass", "NumberDeviceClass", "ReportingConfig",
            "EntityFilter", "UnitOfElectricCurrent", "UnitOfElectricPotential", "UnitOfEnergy", "UnitOfFrequency", "UnitOfLength", "UnitOfPower",
            "UnitOfPressure", "UnitOfSoundPressure", "UnitOfTemperature", "UnitOfTime", "UnitOfVolume", "PERCENTAGE", "LIGHT_LUX",
            "SIGNAL_STRENGTH_DECIBELS", "CONCENTRATION_PARTS_PER_MILLION", "CONCENTRATION_PARTS_PER_BILLION", "CONCENTRATION_MICROGRAMS_PER_CUBIC_METER",
            "CONCENTRATION_MILLIGRAMS_PER_CUBIC_METER",
        ),
        "zhaquirks.builder",
    ),
    "TUYA_CLUSTER_ID": "zhaquirks.tuya",
    **dict.fromkeys(("DPToAttributeMapping", "TuyaCoverControl", "TuyaMCUCluster", "TuyaWindowCovering"), "zhaquirks.tuya.mcu"),
    "TuyaOccupancySensing": "zhaquirks.tuya.tuya_motion",
    "TuyaThermostatV2": "zhaquirks.tuya.tuya_trv",
    "LocalDataCluster": "zhaquirks",
    "CustomCluster": "zhaquirks.clusters",
    **dict.fromkeys((*(f"BUTTON_{n}" for n in range(1, 9)), "COMMAND", "COMMAND_HOLD", "COMMAND_SINGLE", "COMMAND_DOUBLE", "COMMAND_TRIPLE", "COMMAND_RELEASE"), "zhaquirks.const"),
    **dict.fromkeys(("XiaomiAqaraE1Cluster", "DeviceTemperatureCluster", "XiaomiPowerConfiguration", "ElectricalMeasurementCluster", "MeteringCluster"), "zhaquirks.xiaomi"),
    "MultistateInputCluster": "zhaquirks.xiaomi.aqara.opple_remote",
}
# Every cluster class of these zigpy modules (zigpy's standard clusters) by its name.
_CLUSTER_MODULES = ("closures", "general", "greenpower", "homeautomation", "hvac", "lighting", "lightlink", "measurement", "protocol", "security", "smartenergy")
for _module_name in _CLUSTER_MODULES:
    _module = getattr(zigpy.zcl.clusters, _module_name)
    for _name, _value in vars(_module).items():
        if isinstance(_value, type) and issubclass(_value, zigpy.zcl.Cluster) and _value.__module__ == _module.__name__:
            _NAMES.setdefault(_name, _module.__name__)
del _module_name, _module, _name, _value
_OWN = {"TypedTuyaCluster": TypedTuyaCluster}

_PART = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_INHERIT = re.compile(r"(zhaquirks(?:\.[A-Za-z][A-Za-z0-9_]*)+):([A-Za-z][A-Za-z0-9_]*)")
_ID = re.compile(r"[a-z0-9][a-z0-9_]{0,99}")
_REV = re.compile(r"[0-9a-f]{8}")
_DIGEST = re.compile(r"[0-9a-f]{16}")


def _external(name: str):
    """The object an outside name of a definition refers to."""
    if name in _OWN:
        return _OWN[name]
    module_name = _NAMES.get(name)
    if module_name is None:
        raise DefinitionError(f"unknown name {name!r}")
    module = sys.modules.get(module_name)
    if module is None or getattr(getattr(module, "__spec__", None), "_initializing", False):
        raise DefinitionError(f"{module_name} is not loaded")
    return module if name in ("t", "foundation") else vars(module)[name]


def _inherited(name: str, target: str):
    """The zhaquirks cluster class "module:Class" a definition builds on, checked as inherit.py does."""
    match = _INHERIT.fullmatch(target) if isinstance(target, str) else None
    if match is None or not _PART.fullmatch(name):
        raise DefinitionError(f"inherit {name!r}: no zhaquirks module and class name")
    module_name, class_name = match.groups()
    module = sys.modules.get(module_name)
    if module is None or getattr(getattr(module, "__spec__", None), "_initializing", False):
        raise DefinitionError(f"inherit {name!r}: {module_name} is not loaded")
    if not _is_shipped(getattr(module, "__file__", None)):
        raise DefinitionError(f"inherit {name!r}: no module of the installed zhaquirks")
    cls = vars(module).get(class_name)
    if not isinstance(cls, type) or not issubclass(cls, CustomCluster):
        raise DefinitionError(f"inherit {name!r}: no zhaquirks cluster class")
    if not str(getattr(cls, "__module__", "")).startswith("zhaquirks"):  # a CustomCluster stays out of zigpy's registry
        raise DefinitionError(f"inherit {name!r}: not a zhaquirks class kept out of zigpy's registry")
    if not isinstance(getattr(cls, "cluster_id", None), int):
        raise DefinitionError(f"inherit {name!r}: no cluster id")
    return cls


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise DefinitionError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _refuse_constant(name):
    raise DefinitionError(f"{name} is no number")


def _number(text: str):
    """A JSON number, finite and at most 2**64 in size (JSON reads 1e999 as inf and integers of 4300 digits)."""
    value = int(text) if text.lstrip("-").isdigit() else float(text)
    if not abs(value) <= 2**64:
        raise DefinitionError(f"{text:.40} is no finite number of at most 2**64")
    return value


def parse(line: str) -> dict:
    """One line of definitions.jsonl as a definition (a JSON object)."""
    definition = json.loads(line, object_pairs_hook=_no_duplicates, parse_constant=_refuse_constant, parse_float=_number, parse_int=_number)
    if not isinstance(definition, dict):
        raise DefinitionError("not a JSON object")
    return definition


# Values: JSON scalars and lists as they are, objects tagged by their one key.


class Scope:
    """The names one definition refers to: its own classes and inherited classes, then the outside names."""

    def __init__(self, module_name: str):
        self.module_name = module_name
        self.local: dict[str, object] = {}
        self.inherited: set = set()

    def ref(self, path) -> object:
        if not isinstance(path, str):
            raise DefinitionError(f"reference {path!r} is no string")
        parts = path.split(".")
        if not all(_PART.fullmatch(part) for part in parts):
            raise DefinitionError(f"reference {path!r}: a part is no public name")
        value = self.local[parts[0]] if parts[0] in self.local else _external(parts[0])
        for part in parts[1:]:
            if isinstance(value := getattr(value, part), types.ModuleType):
                break
        if isinstance(value, types.ModuleType):  # "t" alone too
            raise DefinitionError(f"reference {path!r} reaches a module")
        return value

    def value(self, data):
        if data is None or isinstance(data, (bool, int, float, str)):
            return data
        if isinstance(data, list):
            return [self.value(item) for item in data]
        if not isinstance(data, dict) or not data:
            raise DefinitionError(f"value {data!r:.60}")
        if "ops" in data:
            return compile_converter(data, self)
        if len(data) != 1 and not ("new" in data and set(data) == {"new", "kwargs"}):
            raise DefinitionError(f"value {data!r:.60}: one key expected")
        if "ref" in data:
            return self.ref(data["ref"])
        if "tuple" in data:
            return tuple(self.value(_list(data["tuple"])))
        if "set" in data:
            return set(self.value(_list(data["set"])))
        if "dict" in data:
            return {self.value(key): self.value(item) for key, item in (_pair(pair) for pair in _list(data["dict"]))}
        if "flags" in data:
            members = [self.value(item) for item in _list(data["flags"])]
            if not members or not all(isinstance(m, enum.Flag) and type(m) is type(members[0]) for m in members):
                raise DefinitionError("flags of different kinds")
            return functools.reduce(operator.or_, members)
        if "list_of" in data:
            item = self.value(data["list_of"])
            if not _attribute_type(item):
                raise DefinitionError(f"list_of {item!r:.40}: no zigpy type")
            return t.List[item]
        if "new" in data:
            return self.new(data["new"], data.get("kwargs", {}))
        raise DefinitionError(f"value {data!r:.60}: unknown tag")

    def new(self, name, kwargs):
        if not isinstance(kwargs, dict):
            raise DefinitionError(f"{name}: kwargs")
        values = {key: self.value(item) for key, item in kwargs.items()}
        if name == "ReportingConfig":
            if set(values) != {"min_interval", "max_interval", "reportable_change"} or not all(_is_int(v) for v in values.values()):
                raise DefinitionError("ReportingConfig: min_interval, max_interval and reportable_change, all integers")
            return ReportingConfig(**values)
        if name == "EntityFilter":
            if not set(values) <= {"entity_cls", "platform", "endpoint_id", "cluster_id", "cluster_type", "unique_id_suffix", "translation_key", "device_class"}:
                raise DefinitionError("EntityFilter: unknown keyword or a function")
            for key, value in values.items():  # None leaves a keyword out
                if key == "entity_cls" and value not in (None, IASZone):
                    raise DefinitionError("EntityFilter: entity class")
                if key != "entity_cls" and value is not None:
                    _check(key, value)
            return EntityFilter(**values)
        if name == "DPToAttributeMapping":
            if not set(values) <= {"ep_attribute", "attribute_name", "converter", "endpoint_id"}:
                raise DefinitionError("DPToAttributeMapping: unknown keyword")
            for key in ("ep_attribute", "attribute_name", "converter", "endpoint_id"):  # the last two may be missing or None
                _check(key, values.get(key))
            return DPToAttributeMapping(**values)
        raise DefinitionError(f"unknown constructor {name!r}")


def _list(data) -> list:
    if not isinstance(data, list):
        raise DefinitionError(f"{data!r:.60} is no list")
    return data


def _pair(data) -> list:
    if not isinstance(data, list) or len(data) != 2:
        raise DefinitionError(f"{data!r:.60} is no pair")
    return data


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# Converters: a list of operations applied in turn to the value, then the wrapper on_error names.

_MAX_OPS, _MAX_DEPTH = 16, 3  # today's converters: at most 3 operations, one if
_CONVERTERS = {"value_error": _value_error_on_error, "none": _none_on_error, "raise": None}
_PLAIN_OPS = {"int": int, "str": str, "bool": bool, "not": operator.not_, "power_24": _power_24, "zigbee_local_time": _zigbee_local_time}
_CONSTANT_OPS = {
    "eq": operator.eq, "gt": operator.gt, "sub": operator.sub, "rsub": lambda value, constant: constant - value, "mul": operator.mul,
    "div": operator.truediv, "floordiv": operator.floordiv, "band": operator.and_,
}


def compile_converter(data: dict, scope: Scope):
    """The converter function of {"ops": [...], "on_error": ...}."""
    if set(data) != {"ops", "on_error"} or data["on_error"] not in _CONVERTERS:
        raise DefinitionError(f"converter {data!r:.60}: ops and on_error (value_error, none or raise)")
    if _size(data["ops"]) > _MAX_OPS:
        raise DefinitionError(f"converter: more than {_MAX_OPS} operations")
    run = _pipeline(data["ops"], scope)
    wrapper = _CONVERTERS[data["on_error"]]
    convert = wrapper(run) if wrapper else run
    convert.xquirks_on_error = data["on_error"]
    return convert


def _size(ops, depth: int = 0) -> int:
    """The operations of the list and of its if branches."""
    if depth > _MAX_DEPTH:
        raise DefinitionError(f"converter: if deeper than {_MAX_DEPTH}")
    return sum(1 + (sum(_size(part, depth + 1) for part in op[1:]) if isinstance(op, list) and op[:1] == ["if"] else 0) for op in _list(ops))


def _pipeline(ops, scope: Scope):
    steps = [_operation(op, scope) for op in _list(ops)]

    def run(value):
        for step in steps:
            value = step(value)
        return value

    return run


def _operation(op, scope: Scope):
    if not isinstance(op, list) or not op or not isinstance(op[0], str):
        raise DefinitionError(f"operation {op!r:.60}")
    name, args = op[0], op[1:]

    def arity(n):
        if len(args) != n:
            raise DefinitionError(f"operation {name}: {n} argument(s) expected")

    if name in _PLAIN_OPS:
        arity(0)
        return _PLAIN_OPS[name]
    if name in _CONSTANT_OPS:
        arity(1)
        constant = scope.value(args[0])
        if name != "eq" and not _is_number(constant):
            raise DefinitionError(f"operation {name}: the operand is no number")
        function = _CONSTANT_OPS[name]
        return lambda value: function(value, constant)
    if name == "enum":
        arity(1)
        cls = scope.value(args[0])
        if not (isinstance(cls, type) and issubclass(cls, enum.Enum)):
            raise DefinitionError("operation enum: no enum class")
        return cls
    if name == "be":
        arity(2)
        start, width = args
        if not (_is_int(start) and _is_int(width) and 0 <= start < 256 and 0 < width <= 8):
            raise DefinitionError("operation be: start and width")
        return lambda value: _be(value, start, width)
    if name == "lookup":
        arity(1)
        table = {}
        for pair in _list(args[0]):
            key, item = _pair(pair)
            table[scope.value(key)] = scope.value(item)
        return lambda value: table[value]
    if name == "const":
        arity(1)
        constant = scope.value(args[0])
        return lambda value: constant
    if name == "if":
        arity(3)
        test, then, otherwise = (_pipeline(part, scope) for part in args)
        return lambda value: then(value) if test(value) else otherwise(value)
    raise DefinitionError(f"unknown operation {name!r}")


# Arguments: each builder argument checked by its parameter name.


def _of(*kinds):
    return lambda value: isinstance(value, kinds) and not (isinstance(value, bool) and bool not in kinds)


def _optional(check):
    return lambda value: value is None or check(value)


def _int_in(low, high):
    return lambda value: _is_int(value) and low <= value <= high


def _cluster_class(value) -> bool:
    return isinstance(value, type) and issubclass(value, zigpy.zcl.Cluster) and getattr(value, "_skip_registry", False) is True


def _attribute_type(value) -> bool:
    """One of zigpy's integer, float, string and bytes types or an enum on one of zigpy's enum and bitmap types (a
    definition's own enum)."""
    if not isinstance(value, type) or not issubclass(value, (int, float, str, bytes)):
        return False
    return value.__module__.startswith("zigpy.types.") or issubclass(value, tuple(_ENUM_BASES))


def _field_type(value) -> bool:
    """The type of a command schema field: an attribute type, or t.List[...] (list_of checked its item type)."""
    return _attribute_type(value) or isinstance(value, type) and issubclass(value, t.List) and value is not t.List


def _converter(*on_error):
    """A converter of the definition with one of these wrappers: ZHA reads attribute converters where nothing catches
    an error (none), zhaquirks skips a datapoint on a ValueError (value_error), a write reports its error (raise)."""
    return lambda value: value is None or callable(value) and getattr(value, "xquirks_on_error", None) in on_error


def _str_mapping(value) -> bool:
    return isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())


_STR = _of(str)
_BOOL = _of(bool)
_NUMBER = _optional(_is_number)
_NONZERO = _optional(lambda value: _is_number(value) and value != 0)
_DP = _int_in(0, 255)
_DEVICE_CLASS = _optional(_of(zhaquirks.builder.SensorDeviceClass, zhaquirks.builder.NumberDeviceClass, zhaquirks.builder.BinarySensorDeviceClass))
_PARAMETERS = {
    "manufacturer": _optional(_STR),
    "model": _optional(_STR),
    "dp_id": _DP,
    "control_dp": _DP,
    "position_state_dp": _DP,
    "position_control_dp": _DP,
    "attribute_name": _STR,
    "ep_attribute": _STR,
    "command_name": _STR,
    "invert_attribute_name": _optional(_STR),
    "cluster_id": _int_in(0, 0xFFFF),
    "endpoint_id": _optional(_int_in(1, 240)),
    "translation_key": _optional(_STR),
    "translation_placeholders": _optional(_str_mapping),
    "unique_id_suffix": _optional(_STR),
    "fallback_name": _optional(_STR),
    "mode": _optional(_STR),
    "unit": _optional(_STR),
    "min_value": _NUMBER,
    "max_value": _NUMBER,
    "step": _NUMBER,
    "multiplier": _NONZERO,
    "divisor": _NONZERO,
    "scale": _NUMBER,
    "suggested_display_precision": _optional(_is_int),
    "on_value": _is_int,
    "off_value": _is_int,
    "attribute_value": _is_int,
    "force_inverted": _BOOL,
    "initially_disabled": _BOOL,
    "attribute_initialized_from_cache": _BOOL,
    "invert": _BOOL,
    "skip_configuration": _BOOL,
    "force_add_cluster": _BOOL,
    "allow_missing": _BOOL,
    "primary": _optional(_BOOL),
    "entity_type": _of(zhaquirks.builder.EntityType),
    "entity_platform": _of(zhaquirks.builder.EntityPlatform),
    "platform": _of(zhaquirks.builder.EntityPlatform),
    "device_class": _DEVICE_CLASS,
    "state_class": _optional(_of(zhaquirks.builder.SensorStateClass)),
    "type": _attribute_type,
    "enum_class": lambda value: isinstance(value, type) and issubclass(value, enum.Enum),
    "access": _of(foundation.ZCLAttributeAccess),
    "converter": _converter("value_error"),
    "dp_converter": _converter("raise", "value_error", "none"),
    "attribute_converter": _converter("none"),
    "replacement_cluster": lambda value: isinstance(value, type) and issubclass(value, TypedTuyaCluster),
    "replacement_cluster_class": _cluster_class,
    "cluster": lambda value: _is_int(value) or _cluster_class(value),
    "cluster_type": _of(ClusterType),
    "reporting_config": _optional(_of(ReportingConfig)),
    "attribute_mapping": lambda value: isinstance(value, list) and value and all(isinstance(m, DPToAttributeMapping) for m in value),
    "dp_handler": lambda value: value == "_dp_2_attr_update",
    "device_type": _int_in(0, 0xFFFF),
    "profile_id": _int_in(0, 0xFFFF),
    "command_args": _optional(_of(tuple)),
    "command_kwargs": _optional(_of(dict)),
    "filters": lambda value: all(isinstance(f, EntityFilter) for f in value),
    "feature": _STR,
    "config": _optional(_of(dict)),
    "min_version": _optional(_is_int),
    "max_version": _optional(_is_int),
    "mcu_write_command": _int_in(0, 0xFF),
    "new_primary": _optional(_BOOL),
    "new_entity_category": _optional(_of(zhaquirks.builder.EntityType)),
    "new_translation_key": _optional(_STR),
    "new_fallback_name": _optional(_STR),
    "new_device_class": _DEVICE_CLASS,
    "new_state_class": _optional(_of(zhaquirks.builder.SensorStateClass)),
    "new_entity_registry_enabled_default": _optional(_BOOL),
    "device_automation_triggers": lambda value: isinstance(value, dict)
    and all(isinstance(k, tuple) and len(k) == 2 and all(isinstance(p, str) for p in k) and _str_mapping(v) for k, v in value.items()),
}
# Per builder class, the methods a definition may call (none takes a function or a device or entity class).
_METHODS = {
    QuirkBuilder: frozenset(
        {
            "applies_to", "firmware_version_filter", "skip_configuration", "adds", "removes", "replaces", "replace_cluster_occurrences", "adds_endpoint",
            "removes_endpoint", "replaces_endpoint", "enum", "sensor", "switch", "number", "binary_sensor", "write_attr_button", "command_button",
            "device_automation_triggers", "friendly_name", "exposes_feature", "prevent_default_entity_creation", "removes_entity", "change_entity_metadata",
        }
    ),
}
_METHODS[TuyaQuirkBuilder] = _METHODS[QuirkBuilder] | {
    "tuya_battery", "tuya_illuminance", "tuya_contact", "tuya_co2", "tuya_electrical_conductivity", "tuya_formaldehyde", "tuya_pm25", "tuya_gas",
    "tuya_smoke", "tuya_metering", "tuya_onoff", "tuya_cover", "tuya_humidity", "tuya_soil_moisture", "tuya_temperature", "tuya_vibration", "tuya_voc",
    "tuya_water_leak", "tuya_attribute", "tuya_dp", "tuya_dp_multi", "tuya_dp_attribute", "tuya_switch", "tuya_enum", "tuya_number", "tuya_binary_sensor",
    "tuya_sensor",
}
_BUILDERS = {"QuirkBuilder": QuirkBuilder, "TuyaQuirkBuilder": TuyaQuirkBuilder}


def _check(name: str, value) -> None:
    check = _PARAMETERS.get(name)
    if check is None:
        raise DefinitionError(f"parameter {name!r} is not allowed")
    if not check(value):
        raise DefinitionError(f"parameter {name!r}: {value!r:.60}")


@functools.lru_cache(maxsize=None)
def _signature(function) -> inspect.Signature:
    return inspect.signature(function)


def _call_parts(call) -> tuple[list, dict]:
    """The positional and keyword arguments of one call [name, args?, kwargs?] of the chain, as data."""
    args, kwargs = [], {}
    rest = call[1:]
    if rest and isinstance(rest[0], list):
        args, rest = rest[0], rest[1:]
    if rest and isinstance(rest[0], dict):
        kwargs, rest = rest[0], rest[1:]
    if rest:
        raise DefinitionError(f"call {call[0]}: [name, args?, kwargs?] expected")
    return args, kwargs


def _arguments(function, call, scope: Scope) -> tuple[list, dict]:
    """The arguments of one call [name, args?, kwargs?] of the chain to a builder method (self first), each checked
    by its parameter's name."""
    args, kwargs = _call_parts(call)
    args = [scope.value(item) for item in args]
    kwargs = {key: scope.value(item) for key, item in kwargs.items()}
    signature = _signature(function)
    try:
        bound = signature.bind(None, *args, **kwargs)
    except TypeError as err:
        raise DefinitionError(f"call {call[0]}: {err}") from None
    for parameter, value in list(bound.arguments.items())[1:]:
        if signature.parameters[parameter].kind is inspect.Parameter.VAR_KEYWORD:
            raise DefinitionError(f"call {call[0]}: keyword arguments of an entity class")
        _check(parameter, value)
    return args, kwargs


# Classes: enums and clusters, built through their metaclass as a class statement builds them.

_ENUM_BASES = frozenset({t.enum8, t.enum16, t.enum32, t.bitmap8, t.bitmap16, t.bitmap32})
# The data a cluster may set, each checked by its key (each constant by its attribute's type once the class is built).
_CLUSTER_DATA = {
    "cluster_id": _int_in(0, 0xFFFF), "ep_attribute": lambda value: isinstance(value, str) and _PART.fullmatch(value) is not None, "name": _STR,
    "_VALID_ATTRIBUTES": lambda value: isinstance(value, set) and all(map(_int_in(0, 0xFFFF), value)),
    "_CONSTANT_ATTRIBUTES": lambda value: isinstance(value, dict) and all(map(_is_int, value)),
    "STRUCT_TAGS": lambda value: isinstance(value, dict) and all(_is_int(k) and isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], str) and _is_number(v[1]) for k, v in value.items()),
    "MIN_VOLTS_MV": _is_int, "MAX_VOLTS_MV": _is_int,
}
_NESTED = {"attribute_defs": ("AttributeDefs", foundation.ZCLAttributeDef), "server_command_defs": ("ServerCommandDefs", foundation.ZCLCommandDef), "client_command_defs": ("ClientCommandDefs", foundation.ZCLCommandDef)}
_DEFINITION_KEYWORDS = {
    foundation.ZCLAttributeDef: {"id", "type", "access", "manufacturer_code", "is_manufacturer_specific", "mandatory"},
    foundation.ZCLCommandDef: {"id", "schema", "direction", "is_manufacturer_specific", "manufacturer_code"},
}


def _class_body(module_name: str, qualname: str, items):
    def body(namespace):
        namespace["__module__"] = module_name
        namespace["__qualname__"] = qualname
        for key, value in items:
            namespace[key] = value

    return body


def _make_enum(spec: dict, scope: Scope):
    name, base = spec["name"], scope.value(spec["base"])
    if base not in _ENUM_BASES:
        raise DefinitionError(f"enum {name}: base")
    members = []
    for pair in _list(spec["members"]):
        member, value = _pair(pair)
        if not (isinstance(member, str) and _PART.fullmatch(member) and not keyword.iskeyword(member) and _is_int(value)):
            raise DefinitionError(f"enum {name}: member {member!r:.40}")
        members.append((member, value))
    if not members:
        raise DefinitionError(f"enum {name}: no member")
    return types.new_class(name, (base,), exec_body=_class_body(scope.module_name, name, members))


def _make_definitions(cluster_name: str, key: str, spec: dict, scope: Scope):
    nested, kind = _NESTED[key]
    base = scope.value(spec["base"])
    allowed = (foundation.BaseAttributeDefs if kind is foundation.ZCLAttributeDef else foundation.BaseCommandDefs)
    if not (isinstance(base, type) and issubclass(base, allowed)):
        raise DefinitionError(f"{cluster_name}.{nested}: base")
    items = []
    for pair in _list(spec["members"]):
        member, keywords = _pair(pair)
        if not (isinstance(member, str) and _PART.fullmatch(member)) or not isinstance(keywords, dict) or not set(keywords) <= _DEFINITION_KEYWORDS[kind]:
            raise DefinitionError(f"{cluster_name}.{nested}: member {member!r:.40}")
        values = {k: scope.value(v) for k, v in keywords.items()}
        if not _int_in(0, 0xFFFF)(values.get("id")):
            raise DefinitionError(f"{cluster_name}.{nested}.{member}: id")
        if kind is foundation.ZCLAttributeDef and not _attribute_type(values.get("type")):
            raise DefinitionError(f"{cluster_name}.{nested}.{member}: type")
        if "schema" in values and not (isinstance(values["schema"], dict) and all(isinstance(k, str) and _field_type(v) for k, v in values["schema"].items())):
            raise DefinitionError(f"{cluster_name}.{nested}.{member}: schema")
        items.append((member, kind(**values)))
    return types.new_class(nested, (base,), exec_body=_class_body(scope.module_name, f"{cluster_name}.{nested}", items))


def _make_cluster(spec: dict, scope: Scope):
    name = spec["name"]
    behaviours = _list(spec.get("behaviours", []))
    if unknown := [b for b in behaviours if b not in BEHAVIOURS]:
        raise DefinitionError(f"cluster {name}: behaviour {unknown[0]!r:.40}")
    bases = [scope.value(base) for base in _list(spec["bases"])]
    if not bases or not all(isinstance(b, type) and issubclass(b, zigpy.zcl.Cluster) for b in bases):
        raise DefinitionError(f"cluster {name}: bases")
    if not any(issubclass(b, CustomCluster) for b in bases):
        raise DefinitionError(f"cluster {name}: no zhaquirks cluster base (zigpy would build every device's cluster of its id from it)")
    items, own = [], {}
    data = spec.get("data", {})
    if not isinstance(data, dict) or not set(data) <= set(_CLUSTER_DATA):
        raise DefinitionError(f"cluster {name}: data")
    for key, value in data.items():
        if key == "_CONSTANT_ATTRIBUTES" and isinstance(value, dict) and "base" in value:
            # {**Base._CONSTANT_ATTRIBUTES, key: value, ...}
            base = scope.value(value["base"])
            if set(value) != {"base", "items"} or not _cluster_class(base):
                raise DefinitionError(f"cluster {name}: _CONSTANT_ATTRIBUTES")
            own = {scope.value(k): scope.value(v) for k, v in (_pair(p) for p in _list(value["items"]))}
            value = {**(getattr(base, "_CONSTANT_ATTRIBUTES", None) or {}), **own}
        else:
            value = scope.value(value)
            own = value if key == "_CONSTANT_ATTRIBUTES" else own
        if not _CLUSTER_DATA[key](value):
            raise DefinitionError(f"cluster {name}: {key} {value!r:.40}")
        items.append((key, value))
    for key in _NESTED:
        if key in spec:
            items.append((_NESTED[key][0], _make_definitions(name, key, spec[key], scope)))
    cls = types.new_class(name, (*(BEHAVIOURS[b] for b in behaviours), *bases), exec_body=_class_body(scope.module_name, name, items))
    for attribute_id, constant in own.items():  # ZHA reads a constant as a value of its attribute's type
        try:
            fits = constant is None or getattr(cls.attributes.get(attribute_id), "type", None)(constant) == constant
        except Exception:  # noqa: BLE001 - no attribute of that id, or a value its type does not hold
            fits = False
        if not fits:
            raise DefinitionError(f"cluster {name}: constant {constant!r:.40} for attribute {attribute_id}")
    if {"MIN_VOLTS_MV", "MAX_VOLTS_MV"} & set(data) and not getattr(cls, "MIN_VOLTS_MV", 0) < getattr(cls, "MAX_VOLTS_MV", 0):
        raise DefinitionError(f"cluster {name}: MIN_VOLTS_MV not below MAX_VOLTS_MV")  # XiaomiPowerConfiguration divides by the difference
    _check_inherited(cls, bases, scope)
    return cls


def _check_inherited(cls, bases, scope: Scope) -> None:
    """A class on an inherited class repeats none of its definition names or ids (inherit.py)."""
    for base in bases:
        if base not in scope.inherited:
            continue
        for nested in ("AttributeDefs", "ServerCommandDefs", "ClientCommandDefs"):
            own = cls.__dict__.get(nested)
            if own is None:
                continue
            theirs = {m: d.id for m in dir(getattr(base, nested)) if not m.startswith("_") and isinstance(d := getattr(getattr(base, nested), m), (foundation.ZCLAttributeDef, foundation.ZCLCommandDef))}
            for member, definition in own.__dict__.items():
                if isinstance(definition, (foundation.ZCLAttributeDef, foundation.ZCLCommandDef)) and (member in theirs or definition.id in theirs.values()):
                    raise DefinitionError(f"{cls.__name__}.{nested}.{member} repeats a name or id of {base.__name__}")


# The filter of every entry, and the options it reads.


class Options:
    """xquirks_options.json next to the stub: excluded devices (IEEE) and the high-risk opt-in."""

    def __init__(self, excluded=(), high_risk=False):
        self.excluded = frozenset(str(ieee).lower() for ieee in excluded)
        self.high_risk = high_risk is True

    @classmethod
    def read(cls, path: pathlib.Path) -> Options:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            excluded = data.get("excluded", [])
            if not isinstance(excluded, list) or not all(isinstance(ieee, str) for ieee in excluded):
                raise ValueError("excluded is no list of IEEE strings")
            return cls(excluded, data.get("high_risk", False))
        except FileNotFoundError:
            return cls()
        except Exception as err:  # noqa: BLE001 - a broken options file means the defaults
            _LOGGER.warning("xquirks: %s unreadable (%s), defaults used", path.name, _why(err))
            return cls()


@functools.lru_cache(maxsize=4096)
def _is_shipped(file) -> bool:
    """Whether the file lies in zhaquirks' folder, compared as text: the event loop calls this."""
    return isinstance(file, str) and os.path.normpath(file).startswith(_SHIPPED)


# The take-over check: a take-over definition stays over ZHA's own entry only while that entry is one the definition was
# built over. The generator records the digest of each such entry (zha_digests), register() computes ZHA's current ones.

# The classes a definition shares with ZHA's quirks count by name: the builder's and v1's machinery (both build with
# it) and every class the loader names (a definition uses it as it is). Every other class of zhaquirks counts with
# its content.
_SHARED_MODULES = ("zhaquirks.builder", "zhaquirks.device", "zhaquirks.legacy")
# What zigpy derives from a class's own definitions (attribute and command tables, a struct's signature) and Python's
# own class data: left out.
_DERIVED = frozenset(
    {
        "_attributes_by_id", "attributes", "attributes_by_name", "server_commands", "client_commands", "commands_by_name", "_signature",
        "_hash", "_frozen", "_abc_impl", "__annotate__", "__annotate_func__",
    }
)
_FUNCTIONS = (types.FunctionType, staticmethod, classmethod, property)
_BUILTINS = (types.BuiltinFunctionType, types.MethodWrapperType, types.WrapperDescriptorType, types.MethodDescriptorType)
_BY_REPR = (datetime.date, datetime.time, datetime.timedelta, datetime.tzinfo, re.Pattern, types.UnionType, types.GenericAlias)
_DIGESTS: dict[int, tuple[object, str]] = {}


def zha_digest(entry) -> str:
    """The digest of a registry entry as data (16 hex digits): what it matches, its transforms and what ZHA builds from
    it. A class of zhaquirks counts with its content, a function with its source text and the globals it reads, a
    class or function the definition shares with ZHA by name. No memory address, hash order or bytecode goes in: the
    digest is the same in every Python run."""
    cached = _DIGESTS.get(id(entry))
    if cached is None or cached[0] is not entry:
        value = _Digest().value((entry.device_match, entry.zigpy_transforms, entry.zha_device_factory))
        cached = _DIGESTS[id(entry)] = (entry, hashlib.sha256(repr(value).encode()).hexdigest()[:16])
    return cached[1]


def built_over(entry, names) -> bool:
    """Whether a take-over with these (manufacturer, model) names can be built over the entry: a v2 entry of ZHA's own
    quirks for one of its exact names. Only such an entry's digest can be in zha_digests (the digest covers the names)."""
    if not isinstance(entry.device_match, DeviceMatch) or getattr(entry.zha_device_factory, "quirk_definition", None) is None:
        return False
    return _is_shipped(getattr(entry.source, "file", None)) and any(
        (zm is None or zm == m) and (zmodel is None or zmodel == model) for zm, zmodel in entry.device_match.applies_to for m, model in names if m is not None
    )


def _names_read(code: types.CodeType) -> set:
    names = set(code.co_names)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= _names_read(const)
    return names


class _Digest:
    """Values as plain tuples for zha_digest, each class and function of zhaquirks once (again: by name)."""

    def __init__(self):
        self.done: set[int] = set()
        self.shared: set[int] = set()
        for name in _NAMES:
            try:
                self.shared.add(id(_external(name)))
            except (DefinitionError, KeyError):
                pass

    def own(self, obj) -> bool:
        module = getattr(obj, "__module__", None) or ""
        if module.split(".")[0] != "zhaquirks":
            return False
        return "<locals>" in obj.__qualname__ or not module.startswith(_SHARED_MODULES) and id(obj) not in self.shared

    def value(self, v):
        if isinstance(v, enum.Enum):
            return ("member", self.value(type(v)), v.name)
        if v is None or isinstance(v, (bool, str, float)):
            return v
        if isinstance(v, int):
            return v if type(v) is int else (type(v).__name__, int(v))
        if isinstance(v, (bytes, bytearray)):
            return ("bytes", bytes(v).hex())
        if isinstance(v, type):
            return self.cls(v)
        if isinstance(v, types.FunctionType):
            return self.function(v)
        if isinstance(v, types.ModuleType):
            return ("module", v.__name__)
        if isinstance(v, (staticmethod, classmethod)):
            return (type(v).__name__, self.value(v.__func__))
        if isinstance(v, property):
            return ("property", self.value(v.fget), self.value(v.fset), self.value(v.fdel))
        if isinstance(v, functools.partial):
            return ("partial", self.value(v.func), self.value(v.args), self.value(v.keywords))
        if isinstance(v, _BUILTINS):
            return ("builtin", getattr(v, "__module__", None), getattr(v, "__qualname__", None))
        if isinstance(v, (list, tuple)):
            return tuple(self.value(x) for x in v)
        if isinstance(v, (set, frozenset)):
            # Each element as though it came first: a set's order is its hash order, which may differ in the next run.
            done, parts = self.done, []
            for x in v:
                self.done = set(done)
                parts.append(repr(self.value(x)))
            self.done = done
            return ("set", tuple(sorted(parts)))
        if isinstance(v, collections.abc.Mapping):
            return ("dict", tuple(sorted(((self.value(k), self.value(x)) for k, x in v.items()), key=repr)))
        if isinstance(v, t.Struct):
            return ("struct", self.value(type(v)), tuple((f.name, self.value(getattr(v, f.name))) for f in v.fields))
        if isinstance(v, _BY_REPR) or type(v).__module__ == "typing":
            return ("repr", repr(v))
        if hasattr(type(v), "__attrs_attrs__"):
            names = [f.name for f in type(v).__attrs_attrs__]
        elif dataclasses.is_dataclass(v):
            names = [f.name for f in dataclasses.fields(v)]
        elif self.own(type(v)) and hasattr(v, "__dict__"):
            names = sorted(vars(v))
        else:  # a logger, a lock, a registry: what it is, not what it holds now
            return ("object", type(v).__module__, type(v).__qualname__)
        return ("object", self.value(type(v)), tuple((n, self.value(getattr(v, n))) for n in names))

    def cls(self, c):
        if not self.own(c):
            if issubclass(c, t.Struct) and "command" in vars(c):  # the schema zigpy builds from a command's dict
                return ("struct", c.__qualname__, self.value(c.fields))
            return ("class", c.__module__, c.__qualname__)
        if id(c) in self.done:
            return ("again", c.__qualname__)
        self.done.add(id(c))
        if issubclass(c, enum.Enum):
            # The methods of its body count when it has any (the other functions there are Python's enum machinery).
            methods = tuple((k, self.value(x)) for k, x in sorted(vars(c).items()) if isinstance(x, _FUNCTIONS) and getattr(getattr(x, "fget", x), "__module__", None) == c.__module__)
            return ("enum", c.__qualname__, self.value(c.__bases__), tuple((n, self.value(m.value)) for n, m in c.__members__.items()), *((methods,) if methods else ()))
        items = [(k, x) for k, x in sorted(vars(c).items()) if k not in _DERIVED and (k[:2] != "__" or isinstance(x, _FUNCTIONS))]
        return ("class", c.__qualname__, self.value(c.__bases__), tuple((k, self.value(x)) for k, x in items))

    def function(self, f):
        if not self.own(f):
            return ("function", f.__module__, f.__qualname__)
        if id(f) in self.done:
            return ("again", f.__qualname__)
        self.done.add(id(f))
        try:
            source = inspect.getsource(f)
        except (OSError, TypeError):
            source = None
        reads = [(n, f.__globals__[n]) for n in sorted(_names_read(f.__code__)) if n in f.__globals__ and n[:2] != "__"]
        cells = []
        for cell in f.__closure__ or ():
            try:
                cells.append(cell.cell_contents)
            except ValueError:  # an empty cell
                cells.append(None)
        return ("function", f.__qualname__, source, self.value(reads), self.value((f.__defaults__, f.__kwdefaults__, cells)))


def misfit(device, endpoints: dict) -> str | None:
    """Why the device does not fit the definition's endpoints, None when it does (the fixed _misfit)."""
    announced = {ep_id: endpoint.in_clusters for ep_id, endpoint in sorted(device.endpoints.items()) if ep_id}
    for ep_id, cluster_ids in endpoints.items():
        if ep_id not in announced:
            return f"it has no endpoint {ep_id}"
        for cluster_id in cluster_ids:
            first = next((other for other, clusters in announced.items() if cluster_id in clusters), None)
            if first is None and min(announced) != ep_id:
                return f"no endpoint has cluster 0x{cluster_id:04X} and the first endpoint is {min(announced)}, not {ep_id}"
            if first is not None and first != ep_id:
                return f"the first endpoint with cluster 0x{cluster_id:04X} is {first}, not {ep_id}"
    return None


_LOGGED: set = set()
_ACTIVE = threading.local()


def _log_once(key, level: int, message: str, *args, exc_info=False) -> None:
    if key not in _LOGGED:
        _LOGGED.add(key)
        _LOGGER.log(level, message, *args, exc_info=exc_info)


class StepBack:
    """The filter of a definition's entries: True keeps the definition for the device, False steps back (reason() says
    why). It never raises: ZHA calls it where nothing catches an error."""

    def __init__(self, ident: str, definition: dict, options: Options):
        self.ident = ident
        self.options = options
        # As v0.3.0 holds a file back (content.high_risk): high risk and not verified on a device.
        self.high_risk = definition.get("risk") == "high" and definition.get("verified") is not True
        self.take_over = definition.get("take_over") is True
        self.zha_digests = frozenset(definition.get("zha_digests", ()))
        self.claimed = frozenset(tuple(pair) for pair in definition.get("claimed_elsewhere", []))
        self.endpoints = {int(ep): tuple(ids) for ep, ids in definition.get("endpoints", {}).items()}

    def __call__(self, device) -> bool:
        if getattr(_ACTIVE, "running", False):
            return False
        _ACTIVE.running = True
        try:
            reason = self.reason(device)
        except Exception:  # noqa: BLE001
            _log_once(("check failed", self.ident), logging.WARNING, "xquirks: %s could not check a device and steps back", self.ident, exc_info=True)
            return False
        finally:
            _ACTIVE.running = False
        if reason is None:
            return True
        _log_once((self.ident, str(device.ieee), reason), logging.INFO, "xquirks: %s steps back for %s (%s)", self.ident, device.ieee, reason)
        return False

    def reason(self, device) -> str | None:
        if str(device.ieee).lower() in self.options.excluded:
            return "excluded in the options"
        if self.high_risk and not self.options.high_risk:
            return "high risk, not switched on in the options"
        if (device.manufacturer, device.model) in self.claimed:
            return "Zigbee2MQTT gives this name to another device"
        if self.endpoints and (why := misfit(device, self.endpoints)) is not None:
            return why
        for entry in DEVICE_REGISTRY:
            if not isinstance(entry.device_match, DeviceMatch):
                continue
            label = getattr(entry.source, "label", None)
            if isinstance(label, str) and label.startswith(LABEL_PREFIX):
                continue
            if not entry.device_match.matches(device):
                continue
            file = getattr(entry.source, "file", None)
            if self.take_over and _is_shipped(file):
                if _DIGESTS.get(id(entry), (None, None))[1] in self.zha_digests:  # by register(): a digest reads files
                    continue
                return f"ZHA's quirk {label} is not the one this take-over was built over"
            return f"{label} ({'ZHA' if _is_shipped(file) else file}) matches it"
        return None


# Building and registering.


def _check_top(definition: dict) -> tuple[str, str]:
    known = {"id", "rev", "risk", "verified", "take_over", "zha_digests", "claimed_elsewhere", "endpoints", "inherits", "classes", "chain", "alias"}
    if not set(definition) <= known:
        raise DefinitionError(f"unknown keys {sorted(set(definition) - known)}")
    ident, rev = definition.get("id"), definition.get("rev")
    if not (isinstance(ident, str) and _ID.fullmatch(ident)) or not (isinstance(rev, str) and _REV.fullmatch(rev)):
        raise DefinitionError("id or rev")
    if definition.get("risk") not in (None, "low", "medium", "high") or not all(definition.get(k) in (None, True, False) for k in ("verified", "take_over")):
        raise DefinitionError("risk, verified or take_over")
    digests = definition.get("zha_digests")
    if (definition.get("take_over") is True) != (digests is not None) or digests is not None and not (
        isinstance(digests, list) and digests and all(isinstance(d, str) and _DIGEST.fullmatch(d) for d in digests)
    ):
        raise DefinitionError("take_over and zha_digests (the digests of ZHA's entries it was built over) go together")
    pairs = _list(definition.get("claimed_elsewhere", []))
    if not all(isinstance(p, list) and len(p) == 2 and all(v is None or isinstance(v, str) for v in p) for p in pairs):
        raise DefinitionError("claimed_elsewhere")
    endpoints = definition.get("endpoints", {})
    if not isinstance(endpoints, dict) or not all(
        re.fullmatch("[1-9][0-9]{0,2}", k) and int(k) <= 240 and isinstance(v, list) and all(_int_in(0, 0xFFFF)(c) for c in v) for k, v in endpoints.items()
    ):
        raise DefinitionError("endpoints")
    if ("chain" in definition) == ("alias" in definition):
        raise DefinitionError("a chain or an alias")
    return ident, rev


def _source(ident: str, rev: str, stub_file: str, stub_module: str) -> QuirkSource:
    return QuirkSource(module=stub_module, file=str(stub_file), line=None, label=f"{LABEL_PREFIX}{ident} {rev}")


def build(definition: dict, stub_file: str, stub_module: str, options: Options) -> list:
    """The registry entries of one definition, with the loader's source and filter (not registered yet)."""
    ident, rev = _check_top(definition)
    source = _source(ident, rev, stub_file, stub_module)
    step_back = StepBack(ident, definition, options)
    if "alias" in definition:
        return _build_alias(definition, step_back, source)
    scope = Scope(__name__)
    for name, target in (definition.get("inherits") or {}).items():
        scope.local[name] = cls = _inherited(name, target)
        scope.inherited.add(cls)
    for spec in _list(definition.get("classes", [])):
        if not isinstance(spec, dict) or spec.get("kind") not in ("enum", "cluster") or not _PART.fullmatch(str(spec.get("name"))):
            raise DefinitionError(f"class {str(spec)[:60]}")
        if spec["name"] in scope.local or spec["name"] in _OWN:
            raise DefinitionError(f"class {spec['name']} defined twice")
        scope.local[spec["name"]] = (_make_enum if spec["kind"] == "enum" else _make_cluster)(spec, scope)
    chain = _list(definition["chain"])
    if len(chain) < 2 or not isinstance(chain[0], list) or chain[0][:1] not in (["QuirkBuilder"], ["TuyaQuirkBuilder"]):
        raise DefinitionError("the chain starts with QuirkBuilder or TuyaQuirkBuilder")
    if not isinstance(chain[-1], list) or chain[-1][:1] != ["add_to_registry"]:
        raise DefinitionError("the chain ends with add_to_registry")
    builder_class = _BUILDERS[chain[0][0]]
    # TuyaQuirkBuilder passes its arguments on to QuirkBuilder's.
    args, kwargs = _arguments(QuirkBuilder.__init__, chain[0], scope)
    builder = builder_class(*args, registry=DeviceRegistry(), **kwargs)
    for call in chain[1:-1]:
        if not isinstance(call, list) or not call or call[0] not in _METHODS[builder_class]:
            raise DefinitionError(f"call {str(call)[:60]} is not allowed")
        args, kwargs = _arguments(getattr(builder_class, call[0]), call, scope)
        getattr(builder, call[0])(*args, **kwargs)
    builder.filter(step_back)
    return [dataclasses.replace(_call_add(builder, chain[-1], scope), source=source)]


def _call_add(builder, call, scope: Scope) -> QuirkRegistryEntry:
    rest = call[1:]
    kwargs = rest[0] if rest and isinstance(rest[0], dict) else {}
    if len(rest) > (1 if kwargs else 0) or not set(kwargs) <= {"replacement_cluster", "force_add_cluster", "mcu_write_command"}:
        raise DefinitionError("add_to_registry: keywords")
    if kwargs and not isinstance(builder, TuyaQuirkBuilder):
        raise DefinitionError("add_to_registry: keywords of the Tuya builder")
    values = {key: scope.value(item) for key, item in kwargs.items()}
    if isinstance(builder, TuyaQuirkBuilder) and "replacement_cluster" not in values:  # zhaquirks' default stores any value
        raise DefinitionError("add_to_registry: a Tuya definition names TypedTuyaCluster as replacement_cluster")
    for key, value in values.items():
        _check(key, value)
    return builder.add_to_registry(**values)


def _build_alias(definition: dict, step_back: StepBack, source: QuirkSource) -> list:
    """Copies of ZHA's own entries for the targets, for the fingerprints ZHA lists for none of its quirks."""
    shipped = [e for e in DEVICE_REGISTRY if isinstance(e.device_match, DeviceMatch) and _is_shipped(getattr(e.source, "file", None))]
    listed = {info for e in shipped for info in e.device_match.applies_to}
    out, missed = [], []
    for group in _list(definition["alias"]):
        if not isinstance(group, dict) or set(group) != {"targets", "fingerprints"}:
            raise DefinitionError("alias group: targets and fingerprints")
        targets = [ModelInfo(*_pair(p)) for p in _list(group["targets"])]
        fingerprints = [ModelInfo(*_pair(p)) for p in _list(group["fingerprints"])]
        if not all(all(v is None or isinstance(v, str) for v in pair) and pair != (None, None) for pair in (*targets, *fingerprints)):
            raise DefinitionError("alias group: names, each with a manufacturer or a model")
        if len(set(fingerprints)) < len(fingerprints):
            raise DefinitionError("alias group: a fingerprint named twice")
        entries = []
        for target in targets:
            for entry in shipped:
                if target in entry.device_match.applies_to and entry not in entries:
                    entries.append(entry)
        names = tuple(fp for fp in fingerprints if fp not in listed)
        if not entries or not names:
            missed.append(f"{targets} ({'ZHA ships no quirk for them' if not entries else 'ZHA lists its names'})")
            continue
        # register() puts an entry first in its key: the last one registered is tried first.
        for entry in reversed(entries):
            match = dataclasses.replace(entry.device_match, applies_to=names, filters=(*entry.device_match.filters, step_back))
            out.append(dataclasses.replace(entry, device_match=match, source=source))
    step_back.missed = "; ".join(missed)  # check() fails the alias for it
    if not out:
        raise DefinitionError(f"no alias registered: {step_back.missed}")
    if missed:  # one line per definition
        _LOGGER.warning("xquirks: %s: alias for %s not registered", step_back.ident, step_back.missed)
    return out


# Building on demand.

_BUILD_LOCK = threading.Lock()


class OnDemand:
    """A chain definition built when ZHA first tries it for a device (until then only its line is kept, compressed).

    Its stand-in entry carries this object as filter, transform and factory, each answering as the built entry does.
    A definition that fails to build is skipped with one log line and never matches."""

    __slots__ = ("ident", "names", "failure", "_line", "_stub_file", "_stub_module", "_options", "_entry")

    def __init__(self, ident: str, names: tuple, line: bytes, stub_file: str, stub_module: str, options: Options):
        self.ident = ident
        self.names = names
        self.failure = None
        self._line = zlib.compress(line, 1)
        self._stub_file = stub_file
        self._stub_module = stub_module
        self._options = options
        self._entry = None

    @property
    def state(self) -> str:
        return "pending" if self._line is not None else "built" if self._entry is not None else "failed"

    def __repr__(self) -> str:
        return f"OnDemand({self.ident!r}, {self.state})"

    def entry(self) -> QuirkRegistryEntry | None:
        """The built entry, built now if it is not yet. None when the definition failed to build."""
        if self._line is not None:
            with _BUILD_LOCK:
                if self._line is not None:
                    self._build()
        return self._entry

    def _build(self) -> None:
        try:
            (entry,) = build(parse(zlib.decompress(self._line)), self._stub_file, self._stub_module, self._options)
            if tuple(entry.device_match.applies_to) != self.names:
                raise DefinitionError("the built names differ from the line's")
            self._entry = entry
        except Exception as err:  # noqa: BLE001 - one broken definition must not stop the others or ZHA
            self.failure = _why(err)
            _LOGGER.warning("xquirks: definition %s skipped: %s", self.ident, self.failure)
        self._line = None

    def __call__(self, device) -> bool:
        """The stand-in's filter. Never raises: ZHA calls it where nothing catches an error."""
        if getattr(_ACTIVE, "running", False):
            return False  # as the built entry's StepBack answers inside another definition's check
        try:
            entry = self.entry()
            return entry is not None and entry.device_match.matches(device)
        except Exception:  # noqa: BLE001
            _log_once(("check failed", self.ident), logging.WARNING, "xquirks: %s could not check a device and steps back", self.ident, exc_info=True)
            return False

    # ZHA calls these only for a stand-in that matched, so the definition built.
    def transform(self, device):
        for step in self.entry().zigpy_transforms:
            device = step(device)
        return device

    def factory(self, *args, **kwargs):
        return self.entry().zha_device_factory(*args, **kwargs)


def _names(chain: list) -> tuple:
    """The (manufacturer, model) pairs the chain registers under, collected as the builder collects them: the
    constructor's when it names either, then each applies_to's. Only literal strings or null."""
    if not chain or not isinstance(chain[0], list) or chain[0][:1] not in (["QuirkBuilder"], ["TuyaQuirkBuilder"]):
        raise DefinitionError("the chain starts with QuirkBuilder or TuyaQuirkBuilder")
    names = []
    for index, call in enumerate(chain):
        if index and (not isinstance(call, list) or call[:1] != ["applies_to"]):
            continue
        args, kwargs = _call_parts(call)
        try:
            bound = _signature(QuirkBuilder.applies_to if index else QuirkBuilder.__init__).bind(None, *args, **kwargs).arguments
        except TypeError as err:
            raise DefinitionError(f"call {call[0]}: {err}") from None
        given = {key: value for key, value in bound.items() if key != "self"}
        if not set(given) <= {"manufacturer", "model"} or not all(v is None or isinstance(v, str) for v in given.values()):
            raise DefinitionError(f"call {call[0]}: manufacturer and model as literal strings or null")
        if not given:
            continue
        pair = ModelInfo(given.get("manufacturer"), given.get("model"))
        if pair == (None, None):
            raise DefinitionError(f"call {call[0]}: a manufacturer and/or model must be named")
        if pair in names:  # ZHA's remove would stop at the second one and leave the entry under the names after it
            raise DefinitionError(f"call {call[0]}: {tuple(pair)} named twice")
        names.append(pair)
    if not names:
        raise DefinitionError("no manufacturer and model")
    return tuple(names)


def _stand_in(definition: dict, line: bytes, stub_file: str, stub_module: str, options: Options) -> QuirkRegistryEntry:
    """The entry register() puts in place of a chain definition (OnDemand)."""
    ident, rev = _check_top(definition)
    holder = OnDemand(ident, _names(_list(definition["chain"])), line, stub_file, stub_module, options)
    return QuirkRegistryEntry(
        device_match=DeviceMatch(applies_to=holder.names, filters=(holder,)),
        zigpy_transforms=(holder.transform,),
        zha_device_factory=holder.factory,
        source=_source(ident, rev, stub_file, stub_module),
    )


def purge() -> None:
    """Remove the entries an earlier run registered (by label), wherever their source lies."""
    for entry in list(DEVICE_REGISTRY):
        label = getattr(entry.source, "label", None)
        if isinstance(label, str) and label.startswith(LABEL_PREFIX):
            with contextlib.suppress(ValueError):  # ZHA's remove stops at the first name the entry is not under
                DEVICE_REGISTRY.remove(entry)


def _why(err: BaseException) -> str:
    text = f"{type(err).__name__}: {err}" if str(err) else type(err).__name__
    return text if len(text) <= 300 else text[:297] + "..."


def _load(data: bytes, stub_file: str, stub_module: str, options: Options, registry: DeviceRegistry):
    """Each line registered in the registry, an alias built at once, any other as a stand-in: (id, definition, its
    entries), or (id, None, the error) for a line that is skipped (what of it was registered taken out again)."""
    seen: set = set()
    # One line at a time: the copies of all lines at once would stay in the heap as freed memory.
    for number, line in enumerate(io.BytesIO(data), 1):
        if not line.strip():
            continue
        ident, entries = f"line {number}", []
        try:
            definition = parse(line)
            if isinstance(definition.get("id"), str):
                ident = definition["id"][:100]
            if ident in seen:
                raise DefinitionError("id used twice")
            if "alias" in definition:
                entries = build(definition, stub_file, stub_module, options)
            else:
                entries = [_stand_in(definition, line, stub_file, stub_module, options)]
            for entry in entries:
                registry.register(entry)
        except Exception as err:  # noqa: BLE001 - one broken definition must not stop the others or ZHA
            for entry in entries:  # ZHA's register raises after it put the entry under the names before
                with contextlib.suppress(ValueError):
                    registry.remove(entry)
            yield ident, None, err
            continue
        seen.add(ident)
        yield ident, definition, entries


def register(stub_file: str, stub_module: str, definitions_file: str | None = None) -> tuple[int, int]:
    """Register every definition (an alias built at once, any other on demand) and return (loaded, skipped)."""
    stub = pathlib.Path(stub_file)
    options = Options.read(stub.with_name(OPTIONS_FILE))
    purge()
    path = pathlib.Path(definitions_file) if definitions_file else pathlib.Path(__file__).with_name(DEFINITIONS_FILE)
    try:
        data = path.read_bytes()
    except OSError as err:
        _LOGGER.error("xquirks: no definitions (%s)", _why(err))
        return 0, 0
    loaded = skipped = 0
    over: set = set()
    for ident, definition, entries in _load(data, str(stub), stub_module, options, DEVICE_REGISTRY):
        if definition is None:
            skipped += 1
            _LOGGER.warning("xquirks: definition %s skipped: %s", ident, _why(entries))
            continue
        if definition.get("take_over") is True:
            over.update(entries[0].device_match.applies_to)
        loaded += 1
    for entry in DEVICE_REGISTRY if over else ():
        try:
            if built_over(entry, over):
                zha_digest(entry)  # now, for StepBack to look up
        except Exception as err:  # noqa: BLE001 - without its digest a take-over steps back from the entry
            _LOGGER.warning("xquirks: ZHA's quirk %s could not be digested: %s", getattr(entry.source, "label", None), _why(err))
    _LOGGER.info("xquirks: %d definitions loaded, %d skipped", loaded, skipped)
    return loaded, skipped


def revision(definition: dict) -> str:
    """The rev of a definition (FORMAT.md, Revision): the first 8 hex digits of the SHA-256 of its canonical JSON without
    rev, verified and risk. The generator writes it, check() compares it. The load never computes it: it would cost every
    ZHA start."""
    rest = {key: value for key, value in definition.items() if key not in ("rev", "verified", "risk")}
    return hashlib.sha256(json.dumps(rest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()[:8]


def _wrong_rev(definition: dict) -> str | None:
    """Why the line's rev is not the one computed from it, None when it is."""
    try:
        rev = revision(definition)
    except Exception as err:  # noqa: BLE001 - a lone surrogate (only an escape writes one) has no UTF-8: this line fails
        return f"rev cannot be computed: {_why(err)}"
    return None if rev == definition["rev"] else f"rev {definition['rev']} differs from the computed rev {rev}"


# What ZHA reads while it loads its devices, where nothing catches an error: the attribute of each entity and a switch's
# invert attribute (zigpy raises KeyError for a name the cluster lacks). The values it computes with: a sensor without
# an attribute converter divides by its divisor and multiplies by its multiplier (ZHA leaves out 1), a number always
# multiplies by its multiplier (1 without one), an enum entity looks its value up in its enum class. zigpy keeps a value
# in its attribute's type. A zigpy enum takes every unsigned integer of its width (an unknown one as an undefined
# member) and raises for any other value. The typed Tuya cluster lets a float into a plain integer type and reads its
# own plain integer attributes signed. It lets a negative value into a bitmap too: zigpy's bitmaps wrap it instead of
# raising.


class _NoRadio:
    """The application of the device the type check resolves: it sends nothing."""

    _dblistener = None

    def register_callback_listener(self, *args, **kwargs):
        return lambda: None


def _computes(metadata) -> bool:
    """Whether ZHA computes with the value of the entity's attribute while it loads its devices."""
    if isinstance(metadata, zhaquirks.builder.metadata.NumberMetadata) or getattr(metadata, "enum", None) is not None:
        return True
    if getattr(metadata, "attribute_converter", None) is not None:
        return False
    return any(getattr(metadata, key, None) not in (None, 1) for key in ("divisor", "multiplier"))


def _resolved(definition: dict, entry, entities: list):
    """The device the entry's transforms make of one with the definition's endpoints and the clusters these entities read."""
    raw = zigpy.device.Device(_NoRadio(), t.EUI64.convert("00:00:00:00:00:00:00:00"), t.NWK(0))
    raw.manufacturer, raw.model = entry.device_match.applies_to[0]
    layout = {int(ep): ids for ep, ids in definition.get("endpoints", {}).items()}
    for metadata in entities:
        layout.setdefault(metadata.endpoint_id, [])
    for ep_id, cluster_ids in sorted(layout.items()):
        endpoint = raw.add_endpoint(ep_id)
        endpoint.profile_id, endpoint.device_type = zigpy.profiles.zha.PROFILE_ID, 0xFFFF
        for cluster_id in cluster_ids:
            endpoint.add_input_cluster(cluster_id)
    for metadata in entities:
        endpoint = raw.endpoints[metadata.endpoint_id]
        if metadata.cluster_type is ClusterType.Server and metadata.cluster_id not in endpoint.in_clusters:
            endpoint.add_input_cluster(metadata.cluster_id)
        elif metadata.cluster_type is not ClusterType.Server and metadata.cluster_id not in endpoint.out_clusters:
            endpoint.add_output_cluster(metadata.cluster_id)
    device = raw
    for transform in entry.zigpy_transforms:
        device = transform(device)
    return device


def _type_reason(metadata, device, tuya: bool) -> str | None:
    """Why the entity reads an attribute its cluster lacks or a value ZHA cannot compute with on the resolved device,
    None when the cluster it reads has its attribute of a type that holds every value (tuya: the device has the typed
    Tuya cluster)."""
    clusters = getattr(device.endpoints.get(metadata.endpoint_id), "in_clusters" if metadata.cluster_type is ClusterType.Server else "out_clusters", {})
    if (cluster := clusters.get(metadata.cluster_id)) is None:
        return None  # ZHA makes no entity of it
    entity = f"the {metadata.entity_platform.value} {metadata.resolved_unique_id_suffix} on endpoint {metadata.endpoint_id}"
    attribute = f"{type(cluster).__name__}.AttributeDefs.{metadata.attribute_name}"
    kind = getattr(cluster.attributes_by_name.get(metadata.attribute_name), "type", None)
    if kind is None:
        return f"{entity} reads {attribute}, which the cluster lacks"
    if (invert := getattr(metadata, "invert_attribute_name", None)) and invert not in cluster.attributes_by_name:
        return f"{entity} reads {type(cluster).__name__}.AttributeDefs.{invert}, which the cluster lacks"
    if not _computes(metadata):
        return None
    enum_class = getattr(metadata, "enum", None)
    if enum_class is None:
        need, fits = "a number type", isinstance(kind, type) and issubclass(kind, (int, float))
    else:
        bits = getattr(enum_class, "_bits", None)
        if not isinstance(bits, int):
            return f"{entity} looks its value up in {enum_class.__name__}, which is no zigpy enum or bitmap"
        need = f"{'an enum or Bool type (no bitmap)' if tuya else 'an unsigned integer type'} of at most {bits} bits"
        holds = isinstance(kind, type) and (issubclass(kind, enum.Enum) and not issubclass(kind, enum.Flag) if tuya else issubclass(kind, int))
        fits = holds and getattr(kind, "_signed", True) is False and getattr(kind, "_bits", bits + 1) <= bits
    return None if fits else f"{entity} reads {attribute} of the type {getattr(kind, '__name__', kind)}: ZHA computes with it while it loads its devices and needs {need}"


def _mistyped(definition: dict, entry) -> str | None:
    """Why an entity of the built entry reads an attribute its cluster lacks or a value ZHA cannot compute with, None
    when each fits (_type_reason on a device _resolved gives)."""
    reading = [metadata for metadata in entry.zha_device_factory.quirk_definition.entity_metadata if getattr(metadata, "attribute_name", None) is not None]
    if not reading:
        return None
    try:
        device = _resolved(definition, entry, reading)
        tuya = any(isinstance(cluster, TypedTuyaCluster) for ep_id, endpoint in device.endpoints.items() if ep_id for cluster in endpoint.in_clusters.values())
        return next((reason for metadata in reading if (reason := _type_reason(metadata, device, tuya))), None)
    except Exception as err:  # noqa: BLE001 - this definition fails, the definitions after it are still checked
        return f"the attributes its entities read cannot be checked: {_why(err)}"


def check(definitions_file: str) -> dict[str, str]:
    """The trial build (CI): every rev the one computed from its line, every definition of the file built now as for a
    device and registered in a registry of its own (call zhaquirks.setup() first), each entity's attribute in its
    cluster and each value ZHA computes with of a type that holds it (_mistyped), each alias group registered and each
    take-over kept over every entry of ZHA's for its names. The reason of each failure by definition id, "line <n>" for
    a line without one."""
    failures = {}
    for ident, definition, entries in _load(pathlib.Path(definitions_file).read_bytes(), "xquirks.py", "xquirks", Options(), DeviceRegistry()):
        if definition is None:
            failures[ident] = _why(entries)
        elif why := _wrong_rev(definition):
            failures[ident] = why
        elif isinstance(holder := entries[0].device_match.filters[0], OnDemand) and holder.entry() is None:
            failures[ident] = holder.failure
        elif isinstance(holder, OnDemand) and (why := _mistyped(definition, holder.entry())):
            failures[ident] = why
        elif "alias" in definition and (missed := entries[0].device_match.filters[-1].missed):  # the alias's StepBack
            failures[ident] = f"alias for {missed} not registered"
        elif definition.get("take_over") is True:
            over = [e for e in DEVICE_REGISTRY if built_over(e, entries[0].device_match.applies_to)]
            try:
                changed = [str(e.source.label) for e in over if zha_digest(digesting := e) not in definition["zha_digests"]]
            except Exception as err:  # noqa: BLE001 - as at load: this take-over fails, the definitions after it are still checked
                failures[ident] = f"ZHA's quirk {digesting.source.label} could not be digested: {_why(err)}"
                continue
            if changed or not over:
                failures[ident] = f"ZHA's quirk {', '.join(changed)} is not the one this take-over was built over" if changed else "take-over: ZHA ships no v2 quirk for its exact names"
    return failures


if __name__ == "__main__":
    if sys.argv[1:2] != ["--check"] or len(sys.argv) != 3:
        sys.exit("usage: python loader.py --check <definitions.jsonl>")
    zhaquirks.setup()
    failed = check(sys.argv[2])
    for ident, why in failed.items():
        print(f"{ident}: {why}")
    sys.exit(1 if failed else 0)
