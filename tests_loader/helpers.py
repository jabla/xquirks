"""Helpers of the loader tests: the definitions they use (sample.jsonl), devices for them, ZHA devices, structural descriptions."""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import json
import types
from unittest import mock

import attrs
from frozendict import frozendict
from zha.quirks import DeviceRegistry
import zigpy.device
import zigpy.types as t
from zigpy.types.basic import KwargTypeMeta
import zigpy.zcl
from zigpy.zcl.clusters.hvac import Thermostat

import loader
from conftest import FOLDER, TESTS

STUB_FILE = "/config/custom_zha_quirks/xquirks.py"
MIXINS = frozenset(loader.BEHAVIOURS.values())
BEHAVIOUR_METHODS = ("_dp_2_attr_update", "_parse_aqara_attributes", "_handle_attribute_event")
TUYA_CLUSTER_ID = 0xEF00
IEEE = "a4:c1:38:00:00:00:00:01"

# The definitions the tests use, by id: lines of definitions.jsonl frozen in sample.jsonl, so a change of the shipped
# data leaves the tests alone. Copy a line again when a raise of zha-quirks breaks a test through it (a take-over's
# digests).
LINES = {json.loads(line)["id"]: line for line in (TESTS / "sample.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()}
# The sample the tests install: chains on QuirkBuilder and TuyaQuirkBuilder, an alias, three take-overs. The tests take
# two of them for high-risk definitions nobody verified, whatever the data says now.
SAMPLE_IDS = (
    "sonoff_zbmicro", "sonoff_zbminir2", "alias_aqara_cl_l02d", "sonoff_swv", "heat_decor_hd_t1000", "chayo_toqcb2_80", "manhot_bx82_tyz1",
    "ctl_ctl_mini_dtp_tyz_ac", "dongguan_pinjia_technology_co_ltd_pj3201a", "box_erc2206_z", "coolo_cs_201z", "tuya_ts0601_3_phase_clamp_meter_relay",
    "shelly_sbdw_103c", "aqara_qbkg30lm", "sonoff_mini_zbdim",
)
HIGH_RISK = ("sonoff_zbmicro", "heat_decor_hd_t1000")


def definition_of(ident: str) -> dict:
    """The definition with this id, a copy of its own."""
    return json.loads(LINES[ident])


def _sample_line(ident: str) -> str:
    if ident not in HIGH_RISK:
        return LINES[ident]
    definition = {key: value for key, value in definition_of(ident).items() if key != "verified"}
    definition["risk"] = "high"  # risk and verified are no part of the rev
    return json.dumps(definition, ensure_ascii=False, separators=(",", ":"))


SAMPLE_TEXT = "".join(_sample_line(ident) + "\n" for ident in SAMPLE_IDS)
SAMPLE = [json.loads(line) for line in SAMPLE_TEXT.splitlines()]
BY_ID = {definition["id"]: definition for definition in SAMPLE}

def label_of(definition: dict) -> str:
    return f"{loader.LABEL_PREFIX}{definition['id']} {definition['rev']}"


def build_new(definition: dict, options: loader.Options | None = None):
    """The loader's entries of the definition, in the order a registry tries them."""
    registry = DeviceRegistry()
    for entry in loader.build(definition, STUB_FILE, "xquirks", options or loader.Options(high_risk=True)):
        registry.register(entry)
    return list(registry)


# Devices before quirks, as make_raw takes them: a Tuya device (CLUSTERS on endpoint 1) or a ZCL device (ENDPOINTS with
# their input clusters, the Basic cluster on ENDPOINT, POWER for the power configuration cluster, OUTPUTS).


def layout_of(definition: dict) -> dict:
    """The device of a chain definition: its first name (a model alone with any manufacturer), endpoint 1 with the Tuya
    cluster for a Tuya chain, else the definition's endpoints."""
    manufacturer, model = loader._names(definition["chain"])[0]
    names = {"MANUFACTURER": manufacturer or "any manufacturer", "MODEL": model}
    if definition["chain"][0][0] == "TuyaQuirkBuilder":
        return {**names, "CLUSTERS": [0x0000, 0x0004, 0x0005, TUYA_CLUSTER_ID]}
    endpoints = {int(ep): ids for ep, ids in (definition.get("endpoints") or {"1": []}).items()}
    return {**names, "ENDPOINTS": endpoints, "ENDPOINT": min(endpoints), "POWER": False}


def alias_layouts(definition: dict) -> list:
    """The devices of an alias definition: one per fingerprint, endpoint 1 with the Basic cluster."""
    return [
        {"MANUFACTURER": manufacturer, "MODEL": model, "ENDPOINTS": {1: [0x0000]}, "ENDPOINT": 1, "POWER": False}
        for group in definition["alias"]
        for manufacturer, model in group["fingerprints"]
    ]


def make_raw(layout: dict, manufacturer=None, model=None):
    """The device of the layout before quirks, under its own name or the one given."""
    app = mock.MagicMock()
    app.get_sequence.return_value = 1
    raw = zigpy.device.Device(app, t.EUI64.convert(IEEE), t.NWK(0x1234))
    manufacturer = manufacturer if manufacturer is not None else layout["MANUFACTURER"]
    raw.manufacturer, raw.model = manufacturer, model if model is not None else layout["MODEL"]
    if "CLUSTERS" in layout:
        ep = raw.add_endpoint(1)
        ep.profile_id, ep.device_type = 0x0104, 0x0051
        for cluster_id in layout["CLUSTERS"]:
            ep.add_input_cluster(cluster_id)
        ep.add_output_cluster(0x000A)
        ep.add_output_cluster(0x0019)
        return raw
    for endpoint_id, cluster_ids in layout["ENDPOINTS"].items():
        ep = raw.add_endpoint(endpoint_id)
        ep.profile_id, ep.device_type = 0x0104, 0x0301
        basic = (0x0000, *((0x0001,) if layout.get("POWER") else ()), 0x0003) if endpoint_id == layout["ENDPOINT"] else ()
        for cluster_id in dict.fromkeys((*basic, *cluster_ids)):
            ep.add_input_cluster(cluster_id)
        if endpoint_id == layout["ENDPOINT"]:
            ep.add_output_cluster(0x000A)
            ep.add_output_cluster(0x0019)
        for cluster_id in layout.get("OUTPUTS", {}).get(endpoint_id, ()):
            if cluster_id not in ep.out_clusters:
                ep.add_output_cluster(cluster_id)
    return raw


def raw_of(ident: str, manufacturer=None, model=None):
    """The device of a sample chain definition, under its own name or the one given."""
    return make_raw(layout_of(BY_ID[ident]), manufacturer, model)


def without_ours(entry):
    """The entry without the loader's StepBack, which decides elsewhere."""
    filters = tuple(f for f in entry.device_match.filters if not isinstance(f, loader.StepBack))
    return dataclasses.replace(entry, device_match=dataclasses.replace(entry.device_match, filters=filters))


def resolve_with(entry, raw):
    """The device resolved by this one entry alone."""
    registry = DeviceRegistry()
    registry.register(without_ours(entry))
    resolved = registry.resolve(raw)
    assert resolved is not raw, "the entry did not resolve the device"
    return resolved


# ZHA devices: built as ZHA's gateway builds them (Device.new), inside an event loop as in ZHA. The test's own where one
# runs, else the session's, closed at its end (close_loop). Some entities keep the loop (a cover's transition timers).
_LOOP = None


def close_loop() -> None:
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


def build_zha_device(resolved):
    """The ZHA device of a resolved zigpy device and the entities its discovery creates, after the quirk's removals and
    metadata changes: (device, entities)."""
    global _LOOP
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        if _LOOP is None or _LOOP.is_closed():
            _LOOP = asyncio.new_event_loop()

        async def build():
            return _build_zha_device(resolved)

        return _LOOP.run_until_complete(build())
    return _build_zha_device(resolved)


def _build_zha_device(resolved):
    from zha.zigbee.device import Device

    gateway = mock.MagicMock()
    gateway.config.config.device_options.consider_unavailable_mains = 7200
    gateway.config.config.device_options.consider_unavailable_battery = 21600
    gateway.state.node_info.ieee = t.EUI64.convert("00:00:00:00:00:00:00:00")
    device = Device.new(resolved, gateway)
    entities = []
    for entity in device.discover_entities():
        if device._is_entity_removed_by_quirk(entity):
            continue
        device._apply_entity_metadata_changes(entity)
        entities.append(entity)
    return device, entities


def build_device(entry, layout, manufacturer=None, model=None):
    resolved = resolve_with(entry, make_raw(layout, manufacturer, model))
    device, entities = build_zha_device(resolved)
    return resolved, device, entities


# Structural descriptions: plain values that compare equal when the built quirks are the same.


def code_print(code: types.CodeType):
    consts = tuple(code_print(c) if isinstance(c, types.CodeType) else c for c in code.co_consts)
    return (code.co_name, code.co_code, consts, code.co_names, code.co_varnames, code.co_freevars, code.co_cellvars)


class Describer:
    """Describes registry entries, the classes of the quirk itself (own modules, builder-local classes) by content."""

    def __init__(self, own_modules: set, inputs: list):
        self.own_modules = own_modules
        self.inputs = inputs
        self.seen: dict[int, str] = {}

    def own(self, cls) -> bool:
        return getattr(cls, "__module__", None) in self.own_modules or "<locals>" in getattr(cls, "__qualname__", "")

    def entry(self, entry) -> dict:
        match = entry.device_match
        return {
            "applies_to": [tuple(pair) for pair in match.applies_to],
            "firmware": (match.firmware_version_min, match.firmware_version_max, match.firmware_version_allow_missing),
            "transforms": [self.value(op) for op in entry.zigpy_transforms],
            "factory": self.value(entry.zha_device_factory),
        }

    def type_(self, cls):
        if isinstance(cls, KwargTypeMeta) and cls.__name__.startswith("Anonymous"):
            # t.List[...] and its kin: the generic and its arguments (the item type).
            base = cls.__mro__[1]
            return ("anonymous", (base.__module__, base.__qualname__), [self.value(getattr(cls, f"_{key}", None)) for key in cls._getitem_kwargs])
        if issubclass(cls, enum.Enum) and self.own(cls):
            base = next((b for b in cls.__mro__[1:] if not self.own(b)), None)
            members = [(name, type(m.value).__qualname__, int(m.value)) for name, m in cls.__members__.items()]
            return ("own enum", cls.__name__, (base.__module__, base.__qualname__), members)
        if issubclass(cls, zigpy.zcl.Cluster):
            return self.cluster(cls)
        if self.own(cls):
            return ("own class", cls.__name__, [self.type_(b) for b in cls.__bases__ if b not in MIXINS])
        return (cls.__module__, cls.__qualname__)

    def cluster(self, cls):
        if id(cls) in self.seen:
            return ("cluster", cls.__name__)
        self.seen[id(cls)] = cls.__name__
        out = {
            "name": cls.__name__,
            "mro": [("own", c.__name__) if self.own(c) else (c.__module__, c.__qualname__) for c in cls.__mro__ if c not in MIXINS],
            "cluster_id": self.value(cls.cluster_id),
            "ep_attribute": cls.ep_attribute,
            "cluster_name": getattr(cls, "name", None),
            "skip_registry": cls._skip_registry,
            "attributes": sorted((self.value(a) for a in cls.AttributeDefs), key=repr),
            "server_commands": sorted((self.command(c) for c in cls.ServerCommandDefs), key=repr),
            "client_commands": sorted((self.command(c) for c in cls.ClientCommandDefs), key=repr),
            "data": {k: self.value(getattr(cls, k, None)) for k in ("_CONSTANT_ATTRIBUTES", "_VALID_ATTRIBUTES", "STRUCT_TARGETS", "STRUCT_TAGS", "MIN_VOLTS_MV", "MAX_VOLTS_MV")},
            "behaviour": {name: self.code(getattr(cls, name, None)) for name in BEHAVIOUR_METHODS},
        }
        if "dp_to_attribute" in cls.__dict__:
            out["tuya"] = {
                "dp_to_attribute": [(dp, [self.value(m) for m in maps]) for dp, maps in sorted(cls.dp_to_attribute.items())],
                "data_point_handlers": sorted(cls.data_point_handlers.items()),
                "attributes_to_dp_converters": [(dp, self.value(f)) for dp, f in sorted(cls.attributes_to_dp_converters.items())],
                "mcu_write_command": self.value(cls.mcu_write_command),
            }
        return out

    def command(self, command):
        fields = [(f.name, self.value(f.type)) for f in getattr(command.schema, "fields", ())]
        return (int(command.id), command.name, fields, self.value(command.direction), command.is_manufacturer_specific, repr(command.manufacturer_code))

    def code(self, function):
        if function is None:
            return None
        if getattr(function, "__module__", None) in self.own_modules:
            return ("own", code_print(function.__code__))
        return ("external", function.__module__, function.__qualname__)

    def value(self, v):
        if isinstance(v, enum.Enum):
            return ("member", self.type_(type(v)), v.name)
        if v is None or isinstance(v, (bool, str)):
            return v
        if isinstance(v, int):
            return (type(v).__qualname__, int(v))
        if isinstance(v, float):
            return ("float", repr(v))
        if isinstance(v, type):
            return self.type_(v)
        if isinstance(v, (bytes, bytearray)):
            return ("bytes", bytes(v).hex())
        if attrs.has(type(v)):
            out = {f.name: self.value(getattr(v, f.name)) for f in attrs.fields(type(v))}
            if hasattr(v, "resolved_unique_id_suffix"):
                out["resolved_unique_id_suffix"] = v.resolved_unique_id_suffix
            return (type(v).__name__, out)
        if dataclasses.is_dataclass(v):
            return (type(v).__name__, {f.name: self.value(getattr(v, f.name)) for f in dataclasses.fields(v)})
        if isinstance(v, (list, tuple)):
            return (type(v).__name__, [self.value(x) for x in v])
        if isinstance(v, (set, frozenset)):
            return ("set", sorted((self.value(x) for x in v), key=repr))
        if isinstance(v, (dict, frozendict)):
            return ("dict", sorted(((self.value(k), self.value(x)) for k, x in v.items()), key=repr))
        if hasattr(v, "fields") and hasattr(v, "serialize"):  # a zigpy struct
            return (type(v).__qualname__, repr(v))
        if callable(v):
            return ("callable", [self.call(v, x) for x in self.inputs])
        return ("other", type(v).__qualname__, repr(v))

    def call(self, function, x):
        try:
            result = function(x)
        except Exception as exc:  # noqa: BLE001 - the error is what is compared
            return ("raise", type(exc).__name__, type(exc.__cause__).__name__ if exc.__cause__ else None)
        return ("ok", self.value(result))


# Edge values every converter is evaluated on.
EDGE_INPUTS = [
    0, 1, 2, 3, 5, 9, 10, 20, 99, 100, 101, 255, 256, 1000, 65535, 0x7FFFFF, 0x800000, 0x99999A, 0x99999B, 0xFFFFFF, 0x0FFFFFFF,
    0x10000000, 0x1999999C, 2**31 - 1, 2**32 - 1, -1, -100, -(2**31), 0.5, 2.55, -2.5, 1e30, True, False, None, "", "1", "abc",
    "0x10", b"", b"\x01", bytes(range(16)), bytes.fromhex("09260012340005dc0000000000000000"), [1, 2, 3, 4, 5, 6, 7, 8],
    t.Bool.true, t.Bool.false, t.uint16_t(1), t.int32s(-5), t.LVBytes(bytes(range(8))), Thermostat.SystemMode.Heat,
    Thermostat.SystemMode.Off, Thermostat.SystemMode.Cool, 5e9,
]


def device_view(resolved, device, entities, describer: Describer) -> dict:
    """What ZHA makes of the device: clusters after the quirk, the device's own data, every entity with its state."""
    clusters = {
        (ep_id, side, cluster_id): describer.type_(type(cluster)) if describer.own(type(cluster)) else (type(cluster).__module__, type(cluster).__qualname__)
        for ep_id, ep in resolved.endpoints.items()
        if ep_id
        for side, table in (("in", ep.in_clusters), ("out", ep.out_clusters))
        for cluster_id, cluster in table.items()
    }
    return {
        "clusters": sorted(clusters.items(), key=repr),
        "device": {name: describer.value(getattr(device, name, None)) for name in ("manufacturer", "model", "name", "exposes_features", "device_automation_triggers", "skip_configuration")},
        "entities": sorted((entity_view(e, describer) for e in entities), key=lambda v: (v["platform"], v["unique_id"])),
    }


ENTITY_PROPERTIES = (
    "translation_key", "fallback_name", "entity_category", "entity_registry_enabled_default", "device_class", "state_class",
    "native_unit_of_measurement", "native_min_value", "native_max_value", "native_step", "mode", "options", "native_value", "is_on",
    "current_option", "hvac_modes", "hvac_mode", "hvac_action", "current_temperature", "target_temperature", "min_temp", "max_temp",
    "current_cover_position", "is_closed",
)


def entity_view(entity, describer: Describer) -> dict:
    out = {"platform": str(entity.PLATFORM), "unique_id": entity.unique_id, "class": type(entity).__qualname__}
    for name in ENTITY_PROPERTIES:
        try:
            value = getattr(entity, name)
        except Exception as exc:  # noqa: BLE001 - a property that raises is compared as such
            value = ("raises", type(exc).__name__)
        if not callable(value):
            out[name] = describer.value(value)
    try:
        state = entity.state
    except Exception as exc:  # noqa: BLE001 - ZHA's own window covering switch raises before its attribute is known
        out["state"] = ("raises", type(exc).__name__)
        return out
    out["state"] = {f.name: describer.value(getattr(state, f.name)) for f in dataclasses.fields(state)} if dataclasses.is_dataclass(state) else describer.value(state)
    return out


# A Home Assistant config folder with the stub installed.


class Installation:
    """custom_zha_quirks/ with the stub and its options, custom_components/xquirks/ with loader and definitions.

    The integration's __init__.py raises: the stub must find the folder without running it.
    """

    def __init__(self, root, definitions: str = SAMPLE_TEXT):
        self.config = root / "config"
        self.quirks = self.config / "custom_zha_quirks"
        self.integration = self.config / "custom_components" / "xquirks"
        self.quirks.mkdir(parents=True)
        self.integration.mkdir(parents=True)
        self.stub = self.quirks / "xquirks.py"
        self.stub.write_text((FOLDER / "stub.py.txt").read_text())
        (self.integration / "loader.py").write_text((FOLDER / "loader.py").read_text())
        (self.integration / "__init__.py").write_text('raise RuntimeError("the integration\'s __init__ ran inside ZHA\'s quirk import")\n')
        self.definitions(definitions)

    def definitions(self, text: str) -> None:
        (self.integration / loader.DEFINITIONS_FILE).write_text(text)

    def options(self, text: str | None = None, **values) -> None:
        (self.quirks / loader.OPTIONS_FILE).write_text(text if text is not None else json.dumps(values))

    def user_file(self, name: str, text: str):
        path = self.quirks / name
        path.write_text(text)
        return path

    def setup(self) -> None:
        import zhaquirks

        zhaquirks.setup(custom_quirks_path=str(self.quirks))
