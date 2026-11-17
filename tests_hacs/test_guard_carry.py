"""The guard's shapes for what a quirk file carries over from ZHA's own quirk: ReportingConfig, the builder methods
friendly_name, exposes_feature, change_entity_metadata, prevent_default_entity_creation, removes_entity,
replaces_endpoint, replace_cluster_occurrences and firmware_version_filter, `x & <int>` in converters, DataTypeId
as a zcl_type and enum32. Each accepted shape is the way a quirk of the pinned zhaquirks writes it (literal values
only); each refused one is a near miss: a value that is not a literal, a call where a literal belongs, a name or
keyword outside the allowlist."""

from __future__ import annotations

import inspect

import pytest

from .integration import content, guard
from .test_guard_files import BE, CHAIN_END, FROZEN, METER, SAFE_HELPER, SHIPPED, TUYA, ZCL, frozen, targets, wrapped

FILTER = "    .filter(no_builtin_quirk)\n"
PATHLIB = "import pathlib\n"
CLUSTER_CLASS = "BoschWaterAlarmCluster"
# Names the cases import: every line goes after `import pathlib`.
REPORTING = "from zhaquirks.builder import ReportingConfig\n"
ENUMS = "from zhaquirks.builder import BinarySensorDeviceClass, NumberDeviceClass, SensorDeviceClass, SensorStateClass\n"
IAS = "from zha.application.platforms.binary_sensor import IASZone\nfrom zigpy.zcl.clusters.security import IasZone\nfrom zhaquirks.builder import EntityFilter\n"
DEVICE_TYPE = "from zigpy.profiles.zha import DeviceType\n"
GENERAL = "from zigpy.zcl.clusters.general import OnOff\n"
DATA_TYPE = "from zigpy.zcl.foundation import BaseAttributeDefs, DataTypeId, ZCLAttributeDef\n"
FOUNDATION = "from zigpy.zcl.foundation import BaseAttributeDefs, ZCLAttributeDef\n"
# A class before the cluster class: an enum8 of the file, an enum class with a member, any other class text.
BEFORE_CLUSTER = "\n\nclass BoschWaterAlarmCluster("


def before_cluster(text: str) -> tuple[str, str]:
    """The edit that puts a class (its text) in front of the file's cluster class."""
    return (BEFORE_CLUSTER, f"\n\n{text}\n\n\nclass BoschWaterAlarmCluster(")


ONLINE_STATE = before_cluster("class OnlineState(t.enum8):\n    off = 0x00")


def enum_class(base: str, member: str) -> tuple[tuple[str, str], ...]:
    return (before_cluster(f"class AudioEffect({base}):\n    {member}"),)
CLUSTER = f"cluster_id={CLUSTER_CLASS}.cluster_id"
ATTRIBUTE = f"attribute_name={CLUSTER_CLASS}.AttributeDefs.alarm_on_motion.name"
SENSOR = f"    .binary_sensor({ATTRIBUTE}, {CLUSTER}, {{extra}}fallback_name='Leak')\n"
CONVERTER = f"attribute_converter={guard.SAFE}(lambda x: {{}}), "
REPORT = "reporting_config=ReportingConfig(min_interval=30, max_interval=900, reportable_change=1), "


def case(name: str = ZCL, imports: str = "", chain: str = "", edits: tuple[tuple[str, str], ...] = ()) -> str:
    """The frozen file with imports after `import pathlib`, calls before its filter, and edits (old, new)."""
    text = frozen(name)
    for old, new in edits:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    assert PATHLIB in text and FILTER in text
    text = text.replace(PATHLIB, PATHLIB + imports, 1).replace(FILTER, chain + FILTER, 1)
    # A converter stands inside the fixed helper (guard.SAFE): the file holds it before its chain, as the quirk files do.
    if f"{guard.SAFE}(" in text and f"def {guard.SAFE}(" not in text:
        start = "\n\n(\n    QuirkBuilder("
        assert text.count(start) == 1
        text = text.replace(start, f"\n\n{SAFE_HELPER}\n{start}", 1)
    return text


def with_sensor(extra: str, imports: str = "", name: str = ZCL, edits: tuple[tuple[str, str], ...] = ()) -> str:
    """The file with a binary_sensor that carries the keywords in extra (a `, ` at the end)."""
    return case(name, imports, SENSOR.format(extra=extra), edits)


def with_calls(calls: str, imports: str = "", name: str = ZCL, edits: tuple[tuple[str, str], ...] = ()) -> str:
    """The file with these builder calls (one per line of calls, each starting with a dot) before its filter."""
    return case(name, imports, "".join(f"    {call}\n" for call in calls.split("\n")), edits)


def problems(text: str, name: str = ZCL, **changes: object) -> list[str]:
    """The guard's reasons for the text under the manifest entry of the frozen file, with changed fields."""
    return list(SHIPPED.problems(text.encode(), targets(name, **changes)))


def passes(text: str, name: str = ZCL, **changes: object) -> None:
    assert problems(text, name, **changes) == []


def refused(text: str, reason: str, name: str = ZCL, **changes: object) -> None:
    found = problems(text, name, **changes)
    assert found, "the guard passes it"
    assert any(reason in problem for problem in found), found


# What a quirk of the pinned zhaquirks writes (zhaquirks/heiman/s1_tl.py, shelly/door_window.py, sonoff/swv.py,
# sonoff/mini_zb2gs.py, xiaomi/aqara/led_strip_t1.py, bosch/relay_rbsh_mms_zb_eu.py, ubisys/trv_h1.py,
# thirdreality/soil_moisture_sensor.py, ctm/valve.py, xiaomi/aqara/switch_agl011.py), with values the file can name.
ACCEPTED = {
    "ReportingConfig on a switch (heiman)": case(imports=REPORTING, edits=(("fallback_name='Alarm on motion')", "reporting_config=ReportingConfig(min_interval=2, max_interval=0, reportable_change=1), fallback_name='Alarm on motion')"),)),
    "ReportingConfig with a float change": with_sensor("reporting_config=ReportingConfig(min_interval=15, max_interval=300, reportable_change=0.5), ", REPORTING),
    "ReportingConfig on a binary sensor (swv)": with_sensor(REPORT, REPORTING),
    "ReportingConfig with equal intervals": with_sensor("reporting_config=ReportingConfig(min_interval=60, max_interval=60, reportable_change=1), ", REPORTING),
    "ReportingConfig with a zero minimum and a zero maximum": with_sensor("reporting_config=ReportingConfig(min_interval=0, max_interval=0, reportable_change=0), ", REPORTING),
    "ReportingConfig with a huge minimum and no maximum": with_sensor("reporting_config=ReportingConfig(min_interval=65535, max_interval=0, reportable_change=1), ", REPORTING),
    "ReportingConfig at the limits": with_sensor("reporting_config=ReportingConfig(min_interval=0, max_interval=65535, reportable_change=4294967295), ", REPORTING),
    "ReportingConfig on an enum": with_calls(f".enum({ATTRIBUTE}, enum_class=OnlineState, {CLUSTER}, {REPORT}fallback_name='Mode')", REPORTING, edits=(ONLINE_STATE,)),
    "ReportingConfig on a sensor": with_calls(f".sensor({ATTRIBUTE}, {CLUSTER}, {REPORT}fallback_name='Level')", REPORTING),
    "ReportingConfig with a periodic report every minute": with_sensor("reporting_config=ReportingConfig(min_interval=1, max_interval=60, reportable_change=0), ", REPORTING),
    "reporting_config None": with_sensor("reporting_config=None, "),
    "friendly_name (relay: the entry's vendor and model)": with_calls(".friendly_name(manufacturer='Bosch', model='BSEN-W')"),
    "friendly_name in either keyword order": with_calls(".friendly_name(model='BSEN-W', manufacturer='Bosch')"),
    "friendly_name in another case (heiman: HEIMAN for the vendor Heiman)": with_calls(".friendly_name(manufacturer='BOSCH', model='bsen-w')"),
    "exposes_feature (heiman)": with_calls(".exposes_feature('siren_basic')"),
    "exposes_feature with a dot": with_calls(".exposes_feature('bega.light_switchable_white')"),
    "change_entity_metadata (heiman)": with_calls(f".change_entity_metadata(endpoint_id=1, {CLUSTER}, new_primary=False, new_entity_category=EntityType.CONFIG)"),
    "change_entity_metadata (third reality)": with_calls(".change_entity_metadata(endpoint_id=1, cluster_id=0x0405, new_device_class=SensorDeviceClass.MOISTURE)", ENUMS),
    "change_entity_metadata of every kind": with_calls(
        ".change_entity_metadata(endpoint_id=2, cluster_id=0x0500, unique_id_suffix='1280', new_translation_key='door', "
        "new_device_class=BinarySensorDeviceClass.DOOR, new_state_class=SensorStateClass.MEASUREMENT, new_entity_category=EntityType.DIAGNOSTIC, "
        "new_entity_registry_enabled_default=False, new_fallback_name='Door', new_translation_placeholders={'name': 'Door'})", ENUMS
    ),
    "change_entity_metadata to a number device class": with_calls(".change_entity_metadata(unique_id_suffix='level', new_device_class=NumberDeviceClass.HUMIDITY)", ENUMS),
    "change_entity_metadata on a suffix only": with_calls(".change_entity_metadata(unique_id_suffix='level', new_primary=False)"),
    "change_entity_metadata to the diagnostic category": with_calls(".change_entity_metadata(endpoint_id=1, new_entity_category=EntityType.DIAGNOSTIC)"),
    "an entity method's primary (one explicitly primary entity)": with_sensor("primary=True, "),
    "prevent_default_entity_creation on QuirkBuilder": with_calls(f".prevent_default_entity_creation(endpoint_id=35, {CLUSTER})"),
    "prevent_default_entity_creation on a suffix": with_calls(".prevent_default_entity_creation(endpoint_id=1, cluster_id=0x0405, unique_id_suffix='level')"),
    "removes_entity of an entity class (shelly, ctm)": with_calls(".removes_entity(EntityFilter(entity_cls=IASZone, endpoint_id=1, cluster_id=IasZone.cluster_id))", IAS),
    "removes_entity with a literal cluster": with_calls(".removes_entity(EntityFilter(entity_cls=IASZone, endpoint_id=2, cluster_id=0x0500, unique_id_suffix='1280'))", IAS),
    "removes_entity without an entity class": with_calls(".removes_entity(EntityFilter(endpoint_id=1, cluster_id=0x0500))", "from zhaquirks.builder import EntityFilter\n"),
    "replaces_endpoint (aqara, ctm)": with_calls(".replaces_endpoint(1, device_type=DeviceType.DIMMABLE_LIGHT)", DEVICE_TYPE),
    "replaces_endpoint without a device type": with_calls(".replaces_endpoint(2)"),
    "replaces_endpoint on the Tuya builder": case(METER, DEVICE_TYPE, "    .replaces_endpoint(1, device_type=DeviceType.WINDOW_COVERING_DEVICE)\n"),
    "replace_cluster_occurrences (bosch)": with_calls(f".replace_cluster_occurrences({CLUSTER_CLASS})"),
    "replace_cluster_occurrences of the file's class with both flags": with_calls(f".replace_cluster_occurrences({CLUSTER_CLASS}, replace_server_instances=True, replace_client_instances=False)"),
    "firmware_version_filter (ubisys)": with_calls(".firmware_version_filter(min_version=0x0170044D)"),
    "firmware_version_filter of every keyword": with_calls(".firmware_version_filter(min_version=1, max_version=0xFFFFFFFF, allow_missing=False)"),
    "firmware_version_filter with a maximum": with_calls(".firmware_version_filter(max_version=100)"),
    "mask of a value (swv)": with_sensor(CONVERTER.format("x & 4")),
    "mask in a comparison (shelly)": with_sensor(CONVERTER.format("(x & 3) == 1")),
    "mask in a cast": with_sensor(CONVERTER.format("bool(x & 0x10)")),
    "mask at the limit": with_sensor(CONVERTER.format("x & 0xFFFFFFFF")),
    "mask by zero": with_sensor(CONVERTER.format("x & 0")),
    "mask in a Tuya converter": case(METER, "", edits=((BE, wrapped("lambda x: x & 255")),)),
    "DataTypeId as a zcl_type (sonoff, bosch)": case(edits=((FOUNDATION, DATA_TYPE), ("type=t.Bool, manufacturer_code=0x1209", "type=t.Bool, zcl_type=DataTypeId.uint8, manufacturer_code=0x1209"))),
    "DataTypeId member with a digit and an underscore": case(edits=((FOUNDATION, DATA_TYPE), ("type=t.Bool,", "type=t.Bool, zcl_type=DataTypeId.bool_,"))),
    "DataTypeId map8 beside other types": case(edits=((FOUNDATION, DATA_TYPE), ("type=t.Bool, manufacturer_code=0x1209", "type=t.bitmap8, zcl_type=DataTypeId.map8, manufacturer_code=0x1209"))),
    "enum32 class (aqara led strip)": case(edits=enum_class("t.enum32", "Random = 0x00\n    Blink = 0x01\n    Last = 0xFFFFFFFF")),
    "enum32 as an attribute type": case(edits=(("type=t.Bool,", "type=t.enum32,"),)),
}


@pytest.mark.parametrize("text", ACCEPTED.values(), ids=ACCEPTED)
def test_the_guard_passes_what_a_quirk_carries_over(text):
    name = METER if "TuyaQuirkBuilder" in text else ZCL
    passes(text, name)


# Near misses: each is refused for the reason named.
L = CONVERTER.format
REFUSED = {
    # ReportingConfig: the three literal keywords, in a reporting_config of an entity method, nowhere else.
    "ReportingConfig with a call in it": (with_sensor("reporting_config=ReportingConfig(min_interval=int(30), max_interval=900, reportable_change=1), ", REPORTING), "ReportingConfig interval"),
    "ReportingConfig interval computed": (with_sensor("reporting_config=ReportingConfig(min_interval=30 + 1, max_interval=900, reportable_change=1), ", REPORTING), "interval"),
    "ReportingConfig interval a name": (with_sensor("reporting_config=ReportingConfig(min_interval=_LOGGER, max_interval=900, reportable_change=1), ", REPORTING), "interval"),
    "ReportingConfig interval a bool": (with_sensor("reporting_config=ReportingConfig(min_interval=True, max_interval=900, reportable_change=1), ", REPORTING), "interval"),
    "ReportingConfig interval negative": (with_sensor("reporting_config=ReportingConfig(min_interval=-1, max_interval=900, reportable_change=1), ", REPORTING), "interval"),
    "ReportingConfig interval beyond 16 bits": (with_sensor("reporting_config=ReportingConfig(min_interval=1, max_interval=65536, reportable_change=1), ", REPORTING), "interval"),
    "ReportingConfig interval a float": (with_sensor("reporting_config=ReportingConfig(min_interval=1.5, max_interval=900, reportable_change=1), ", REPORTING), "interval"),
    "ReportingConfig change a text": (with_sensor("reporting_config=ReportingConfig(min_interval=1, max_interval=900, reportable_change='1'), ", REPORTING), "reportable_change"),
    "ReportingConfig change negative": (with_sensor("reporting_config=ReportingConfig(min_interval=1, max_interval=900, reportable_change=-1), ", REPORTING), "reportable_change"),
    "ReportingConfig change a call": (with_sensor("reporting_config=ReportingConfig(min_interval=1, max_interval=900, reportable_change=float(1)), ", REPORTING), "ReportingConfig reportable_change"),
    "ReportingConfig change infinite": (with_sensor("reporting_config=ReportingConfig(min_interval=1, max_interval=900, reportable_change=1e999), ", REPORTING), "reportable_change"),
    "ReportingConfig change beyond 32 bits": (with_sensor("reporting_config=ReportingConfig(min_interval=1, max_interval=900, reportable_change=4294967296), ", REPORTING), "reportable_change"),
    "ReportingConfig without a keyword": (with_sensor("reporting_config=ReportingConfig(min_interval=1, max_interval=900), ", REPORTING), "ReportingConfig other than the keywords"),
    "ReportingConfig with another keyword": (with_sensor("reporting_config=ReportingConfig(min_interval=1, max_interval=900, reportable_change=1, x=1), ", REPORTING), "ReportingConfig other than the keywords"),
    "ReportingConfig with positional arguments": (with_sensor("reporting_config=ReportingConfig(1, 900, 1), ", REPORTING), "ReportingConfig other than the keywords"),
    "ReportingConfig with spread keywords": (with_sensor("reporting_config=ReportingConfig(**{'min_interval': 1}), ", REPORTING), "ReportingConfig other than the keywords"),
    "ReportingConfig under another keyword": (with_sensor("entity_type=ReportingConfig(min_interval=1, max_interval=900, reportable_change=1), ", REPORTING), "call of ReportingConfig"),
    "ReportingConfig as a positional argument": (with_calls(".adds(ReportingConfig(min_interval=1, max_interval=900, reportable_change=1))", REPORTING), "call of ReportingConfig"),
    "ReportingConfig on another method": (with_calls(f".adds({CLUSTER_CLASS}, " + REPORT.rstrip(", ") + ")", REPORTING), "ReportingConfig other than the reporting_config of an entity method"),
    "ReportingConfig on a Tuya sensor": (case(METER, REPORTING, "    .sensor(attribute_name='current2', cluster_id=TUYA_CLUSTER_ID, " + REPORT + "fallback_name='Current')\n"), "of a QuirkBuilder chain"),
    "ReportingConfig in a converter": (with_sensor(L("ReportingConfig(min_interval=1, max_interval=900, reportable_change=1)"), REPORTING), "call of ReportingConfig"),
    "ReportingConfig by name": (with_sensor("reporting_config=ReportingConfig, ", REPORTING), "reporting_config other than ReportingConfig(...) or None"),
    "ReportingConfig from another module": (with_sensor("", "from zhaquirks.builder.metadata import ReportingConfig\n"), "import of ReportingConfig from zhaquirks.builder.metadata"),
    "ReportingConfig not imported": (with_sensor("reporting_config=ReportingConfig(min_interval=1, max_interval=900, reportable_change=1), "), "reporting_config other than ReportingConfig(...) or None"),
    # Anything else as a reporting config: zhaquirks reads its fields after the entity is built, an error there ends the
    # device's quirk entities.
    "reporting_config as a tuple": (with_sensor("reporting_config=(30, 900, 1), "), "reporting_config other than ReportingConfig(...) or None"),
    "reporting_config as a number": (with_sensor("reporting_config=1, "), "reporting_config other than ReportingConfig(...) or None"),
    "reporting_config as text": (with_sensor("reporting_config='x', "), "reporting_config other than ReportingConfig(...) or None"),
    "reporting_config as a dict": (with_sensor("reporting_config={'min_interval': 1}, "), "reporting_config other than ReportingConfig(...) or None"),
    "reporting_config as a class of the file": (with_sensor(f"reporting_config={CLUSTER_CLASS}, "), "reporting_config other than ReportingConfig(...) or None"),
    "reporting_config as a bool": (with_sensor("reporting_config=False, "), "reporting_config other than ReportingConfig(...) or None"),
    "reporting_config on a Tuya method": (case(METER, "", edits=((BE, BE + ", reporting_config=1"),)), "reporting_config other than ReportingConfig(...) or None"),
    # friendly_name: both keywords, printable text.
    "friendly_name with a call": (with_calls(".friendly_name(manufacturer=str(1), model='X')"), "friendly_name"),
    "friendly_name without the model": (with_calls(".friendly_name(manufacturer='Bosch')"), "friendly_name"),
    "friendly_name positional": (with_calls(".friendly_name('Bosch', 'X')"), "friendly_name"),
    "friendly_name with another keyword": (with_calls(".friendly_name(manufacturer='Bosch', model='X', x='Y')"), "friendly_name"),
    "friendly_name with a control character": (with_calls(".friendly_name(manufacturer='Bosch', model='X\\n')"), "friendly_name"),
    "friendly_name too long": (with_calls(f".friendly_name(manufacturer='Bosch', model='{'X' * 129}')"), "friendly_name"),
    "friendly_name empty": (with_calls(".friendly_name(manufacturer='', model='X')"), "friendly_name"),
    "friendly_name as an f-string": (with_calls(".friendly_name(manufacturer=f'B{1}', model='X')"), "friendly_name"),
    "friendly_name on the Tuya builder": (case(METER, "", "    .friendly_name(manufacturer='Moes', model='X')\n"), "builder method friendly_name"),
    # exposes_feature: one feature name, no config.
    "exposes_feature of two": (with_calls(".exposes_feature('a', 'b')"), "exposes_feature"),
    "exposes_feature with a config": (with_calls(".exposes_feature('siren_basic', {'a': 1})"), "exposes_feature"),
    "exposes_feature with a config keyword": (with_calls(".exposes_feature('siren_basic', config={})"), "exposes_feature"),
    "exposes_feature by a name": (with_calls(".exposes_feature(SIREN_BASIC)"), "exposes_feature"),
    "exposes_feature not text": (with_calls(".exposes_feature(1)"), "exposes_feature"),
    "exposes_feature in upper case": (with_calls(".exposes_feature('SIREN')"), "exposes_feature"),
    "exposes_feature with a space": (with_calls(".exposes_feature('siren basic')"), "exposes_feature"),
    "exposes_feature empty": (with_calls(".exposes_feature('')"), "exposes_feature"),
    "exposes_feature too long": (with_calls(f".exposes_feature('{'a' * 65}')"), "exposes_feature"),
    "exposes_feature computed": (with_calls(".exposes_feature('siren' + '_basic')"), "exposes_feature"),
    "exposes_feature on the Tuya builder": (case(METER, "", "    .exposes_feature('siren_basic')\n"), "builder method exposes_feature"),
    # change_entity_metadata: literal keywords of its signature, an entity filter and a new_* among them.
    "change_entity_metadata positional": (with_calls(".change_entity_metadata(1, new_primary=False)"), "change_entity_metadata"),
    "change_entity_metadata with a function": (with_calls(".change_entity_metadata(endpoint_id=1, function=lambda e: True, new_primary=False)"), "change_entity_metadata"),
    "change_entity_metadata with a cluster type": (with_calls(".change_entity_metadata(endpoint_id=1, cluster_id=5, cluster_type=1, new_primary=False)"), "change_entity_metadata"),
    "change_entity_metadata with spread keywords": (with_calls(".change_entity_metadata(endpoint_id=1, **{'new_primary': False})"), "change_entity_metadata"),
    "change_entity_metadata without a filter": (with_calls(".change_entity_metadata(new_primary=False)"), "without an entity filter"),
    "change_entity_metadata without a change": (with_calls(".change_entity_metadata(endpoint_id=1)"), "without an entity filter and a new_*"),
    "change_entity_metadata primary not a bool": (with_calls(".change_entity_metadata(endpoint_id=1, new_primary=0)"), "new_primary"),
    "change_entity_metadata primary a text": (with_calls(".change_entity_metadata(endpoint_id=1, new_primary='False')"), "new_primary"),
    "change_entity_metadata endpoint 0": (with_calls(".change_entity_metadata(endpoint_id=0, new_primary=False)"), "endpoint_id"),
    "change_entity_metadata endpoint a name": (with_calls(".change_entity_metadata(endpoint_id=TUYA, new_primary=False)"), "endpoint_id"),
    "change_entity_metadata cluster as a text": (with_calls(".change_entity_metadata(cluster_id='5', new_primary=False)"), "cluster_id"),
    "change_entity_metadata cluster out of range": (with_calls(".change_entity_metadata(cluster_id=0x10000, new_primary=False)"), "cluster_id"),
    "change_entity_metadata suffix with a space": (with_calls(".change_entity_metadata(unique_id_suffix='a b', new_primary=False)"), "unique_id_suffix"),
    "change_entity_metadata translation key with a dot": (with_calls(".change_entity_metadata(endpoint_id=1, new_translation_key='../x')"), "new_translation_key"),
    # new_unique_id is no keyword of the guard: zhaquirks writes it verbatim as the entity's unique id, which would
    # let a file name the id of a ZHA group entity (f"light_zha_group_0x{group_id:04x}").
    "change_entity_metadata unique id of a group entity": (with_calls(".change_entity_metadata(endpoint_id=1, new_unique_id='light_zha_group_0x0002')"), "change_entity_metadata with other arguments than the keywords"),
    "change_entity_metadata unique id alone": (with_calls(".change_entity_metadata(endpoint_id=1, new_unique_id='door')"), "change_entity_metadata with other arguments than the keywords"),
    "change_entity_metadata unique id beside a valid change": (with_calls(".change_entity_metadata(endpoint_id=1, new_primary=False, new_unique_id='door')"), "change_entity_metadata with other arguments than the keywords"),
    "change_entity_metadata unique id computed": (with_calls(".change_entity_metadata(endpoint_id=1, new_unique_id='a' + 'b')"), "change_entity_metadata with other arguments than the keywords"),
    "change_entity_metadata name with a control character": (with_calls(".change_entity_metadata(endpoint_id=1, new_fallback_name='a\\x00')"), "new_fallback_name"),
    "change_entity_metadata name too long": (with_calls(f".change_entity_metadata(endpoint_id=1, new_fallback_name='{'a' * 129}')"), "new_fallback_name"),
    "change_entity_metadata placeholders of a call": (with_calls(".change_entity_metadata(endpoint_id=1, new_translation_placeholders=dict(a='b'))"), "new_translation_placeholders"),
    "change_entity_metadata placeholders with a value that is not text": (with_calls(".change_entity_metadata(endpoint_id=1, new_translation_placeholders={'a': 1})"), "new_translation_placeholders"),
    "change_entity_metadata placeholders spread": (with_calls(".change_entity_metadata(endpoint_id=1, new_translation_placeholders={**{'a': 'b'}})"), "new_translation_placeholders"),
    "change_entity_metadata placeholders empty": (with_calls(".change_entity_metadata(endpoint_id=1, new_translation_placeholders={})"), "new_translation_placeholders"),
    "change_entity_metadata placeholder name with a dot": (with_calls(".change_entity_metadata(endpoint_id=1, new_translation_placeholders={'a.b': 'c'})"), "new_translation_placeholders"),
    "change_entity_metadata placeholders of nine": (with_calls(".change_entity_metadata(endpoint_id=1, new_translation_placeholders={" + ", ".join(f"'k{n}': 'v'" for n in range(9)) + "})"), "new_translation_placeholders"),
    "change_entity_metadata device class as text": (with_calls(".change_entity_metadata(endpoint_id=1, new_device_class='moisture')", ENUMS), "new_device_class"),
    "change_entity_metadata device class of another enum": (with_calls(".change_entity_metadata(endpoint_id=1, new_device_class=EntityType.CONFIG)", ENUMS), "new_device_class"),
    "change_entity_metadata device class not imported": (with_calls(".change_entity_metadata(endpoint_id=1, new_device_class=SensorDeviceClass.MOISTURE)"), "new_device_class"),
    "change_entity_metadata device class called": (with_calls(".change_entity_metadata(endpoint_id=1, new_device_class=SensorDeviceClass('x'))", ENUMS), "new_device_class"),
    "change_entity_metadata device class in lower case": (with_calls(".change_entity_metadata(endpoint_id=1, new_device_class=SensorDeviceClass.mro)", ENUMS), "new_device_class"),
    "change_entity_metadata device class deeper": (with_calls(".change_entity_metadata(endpoint_id=1, new_device_class=SensorDeviceClass.MOISTURE.value)", ENUMS), "new_device_class"),
    "change_entity_metadata state class of a device class": (with_calls(".change_entity_metadata(endpoint_id=1, new_state_class=SensorDeviceClass.MOISTURE)", ENUMS), "new_state_class"),
    "change_entity_metadata category of a state class": (with_calls(".change_entity_metadata(endpoint_id=1, new_entity_category=SensorStateClass.MEASUREMENT)", ENUMS), "new_entity_category"),
    "change_entity_metadata on the Tuya builder": (case(METER, "", "    .change_entity_metadata(endpoint_id=1, new_primary=False)\n"), "builder method change_entity_metadata"),
    # prevent_default_entity_creation on QuirkBuilder: the keywords the Tuya builder has (zhaquirks' method takes no entity_cls).
    "prevent_default_entity_creation with an entity class": (with_calls(".prevent_default_entity_creation(endpoint_id=1, cluster_id=0x0500, entity_cls=IASZone)", IAS), "prevent_default_entity_creation"),
    "prevent_default_entity_creation with a function": (with_calls(".prevent_default_entity_creation(cluster_id=0x0500, function=lambda e: True)"), "prevent_default_entity_creation"),
    "prevent_default_entity_creation without a cluster": (with_calls(".prevent_default_entity_creation(endpoint_id=1)"), "without cluster_id"),
    # removes_entity: one EntityFilter with the keywords of the hide checker and an entity class of the allowlist.
    "removes_entity without a filter": (with_calls(".removes_entity()"), "removes_entity other than one EntityFilter"),
    "removes_entity of two filters": (with_calls(".removes_entity(EntityFilter(cluster_id=1), EntityFilter(cluster_id=2))", IAS), "removes_entity other than one EntityFilter"),
    "removes_entity of something else": (with_calls(".removes_entity(IASZone)", IAS), "removes_entity other than one EntityFilter"),
    "removes_entity with a keyword": (with_calls(".removes_entity(filters=EntityFilter(cluster_id=1))", IAS), "removes_entity other than one EntityFilter"),
    "removes_entity of a filter with positional arguments": (with_calls(".removes_entity(EntityFilter(IASZone, cluster_id=1))", IAS), "removes_entity other than one EntityFilter"),
    "removes_entity of a function filter": (with_calls(".removes_entity(EntityFilter(cluster_id=1, function=lambda e: True))", IAS), "EntityFilter with other arguments"),
    "removes_entity of a platform": (with_calls(".removes_entity(EntityFilter(cluster_id=1, platform=EntityPlatform.SENSOR))", IAS), "EntityFilter with other arguments"),
    "removes_entity of a cluster type": (with_calls(".removes_entity(EntityFilter(cluster_id=1, cluster_type=1))", IAS), "EntityFilter with other arguments"),
    "removes_entity without a cluster": (with_calls(".removes_entity(EntityFilter(entity_cls=IASZone, endpoint_id=1))", IAS), "without cluster_id"),
    "removes_entity of an entity class not on the list": (with_calls(".removes_entity(EntityFilter(entity_cls=OnOff, cluster_id=0x0500))", IAS + GENERAL), "entity_cls other than IASZone"),
    "removes_entity of an entity class by text": (with_calls(".removes_entity(EntityFilter(entity_cls='IASZone', cluster_id=0x0500))", IAS), "entity_cls other than IASZone"),
    "removes_entity of a called entity class": (with_calls(".removes_entity(EntityFilter(entity_cls=IASZone(), cluster_id=0x0500))", IAS), "entity_cls other than IASZone"),
    "removes_entity of an entity class not imported": (with_calls(".removes_entity(EntityFilter(entity_cls=IASZone, cluster_id=0x0500))", "from zhaquirks.builder import EntityFilter\n"), "entity_cls other than IASZone"),
    "removes_entity with a cluster class itself": (with_calls(".removes_entity(EntityFilter(cluster_id=IasZone))", IAS), "cluster_id"),
    "removes_entity with a suffix computed": (with_calls(".removes_entity(EntityFilter(cluster_id=1, unique_id_suffix=str(1280)))", IAS), "unique_id_suffix"),
    "removes_entity with endpoint 0": (with_calls(".removes_entity(EntityFilter(cluster_id=1, endpoint_id=0))", IAS), "endpoint_id"),
    "removes_entity with spread keywords": (with_calls(".removes_entity(EntityFilter(cluster_id=1, **{'endpoint_id': 1}))", IAS), "EntityFilter with other arguments"),
    "removes_entity with EntityFilter not imported": (with_calls(".removes_entity(EntityFilter(cluster_id=0x0500))"), "removes_entity other than one EntityFilter"),
    "removes_entity on the Tuya builder": (case(METER, IAS, "    .removes_entity(EntityFilter(cluster_id=1))\n"), "builder method removes_entity"),
    "EntityFilter outside removes_entity": (with_calls(".adds(EntityFilter(cluster_id=1))", IAS), "call of EntityFilter"),
    "EntityFilter as a keyword value": (with_sensor("entity_type=EntityFilter(cluster_id=1), ", IAS), "call of EntityFilter"),
    "IASZone outside an entity filter": (with_sensor("entity_type=IASZone, ", IAS), "name IASZone"),
    "IASZone from another module": (with_calls(".adds()", "from zha.application.platforms.sensor import IASZone\n"), "import of IASZone from zha.application.platforms.sensor"),
    "another entity class of the module": (with_calls(".adds()", "from zha.application.platforms.binary_sensor import BinarySensor\n"), "import of BinarySensor"),
    "IASZone under another name": (with_calls(".adds()", "from zha.application.platforms.binary_sensor import IASZone as Z\n"), "import of IASZone as Z"),
    # replaces_endpoint: an endpoint and a device type of DeviceType.
    "replaces_endpoint without an endpoint": (with_calls(".replaces_endpoint(device_type=DeviceType.DIMMABLE_LIGHT)", DEVICE_TYPE), "replaces_endpoint"),
    "replaces_endpoint of endpoint 0": (with_calls(".replaces_endpoint(0)"), "replaces_endpoint"),
    "replaces_endpoint of endpoint 255": (with_calls(".replaces_endpoint(255)"), "replaces_endpoint"),
    "replaces_endpoint of a text": (with_calls(".replaces_endpoint('1')"), "replaces_endpoint"),
    "replaces_endpoint of a keyword endpoint": (with_calls(".replaces_endpoint(endpoint_id=1)"), "replaces_endpoint"),
    "replaces_endpoint of two endpoints": (with_calls(".replaces_endpoint(1, 2)"), "replaces_endpoint"),
    "replaces_endpoint with a number device type": (with_calls(".replaces_endpoint(1, device_type=0x0101)"), "replaces_endpoint"),
    "replaces_endpoint with another enum": (with_calls(".replaces_endpoint(1, device_type=EntityType.CONFIG)"), "replaces_endpoint"),
    "replaces_endpoint with a lower case member": (with_calls(".replaces_endpoint(1, device_type=DeviceType.mro)", DEVICE_TYPE), "replaces_endpoint"),
    "replaces_endpoint with a computed device type": (with_calls(".replaces_endpoint(1, device_type=DeviceType.DIMMABLE_LIGHT | 1)", DEVICE_TYPE), "replaces_endpoint"),
    "replaces_endpoint with a profile": (with_calls(".replaces_endpoint(1, profile_id=0x104)"), "replaces_endpoint"),
    "replaces_endpoint with a device type not imported": (with_calls(".replaces_endpoint(1, device_type=DeviceType.DIMMABLE_LIGHT)"), "replaces_endpoint"),
    # replace_cluster_occurrences: a cluster class of the file.
    "replace_cluster_occurrences without a class": (with_calls(".replace_cluster_occurrences()"), "replace_cluster_occurrences"),
    "replace_cluster_occurrences of a text": (with_calls(".replace_cluster_occurrences('OnOff')"), "replace_cluster_occurrences"),
    "replace_cluster_occurrences of an imported class": (with_calls(".replace_cluster_occurrences(OnOff, replace_server_instances=True, replace_client_instances=False)", GENERAL), "replace_cluster_occurrences other than one cluster class the file defines"),
    "replace_cluster_occurrences of an imported zigpy name that is no cluster": (with_calls(".replace_cluster_occurrences(UTC)", "from zigpy.zcl.clusters.general import UTC\n"), "replace_cluster_occurrences other than one cluster class the file defines"),
    "replace_cluster_occurrences of a called class": (with_calls(".replace_cluster_occurrences(OnOff())", GENERAL), "replace_cluster_occurrences"),
    "replace_cluster_occurrences of an unknown name": (with_calls(".replace_cluster_occurrences(NoSuchCluster)"), "replace_cluster_occurrences"),
    "replace_cluster_occurrences of an enum of the file": (with_calls(".replace_cluster_occurrences(OnlineState)", edits=(ONLINE_STATE,)), "replace_cluster_occurrences"),
    "replace_cluster_occurrences of the cluster id": (with_calls(f".replace_cluster_occurrences({CLUSTER_CLASS}.cluster_id)"), "replace_cluster_occurrences"),
    "replace_cluster_occurrences of two classes": (with_calls(f".replace_cluster_occurrences({CLUSTER_CLASS}, OnOff)", GENERAL), "replace_cluster_occurrences"),
    "replace_cluster_occurrences with a class keyword": (with_calls(f".replace_cluster_occurrences(replacement_cluster_class={CLUSTER_CLASS})"), "replace_cluster_occurrences"),
    "replace_cluster_occurrences with a number flag": (with_calls(f".replace_cluster_occurrences({CLUSTER_CLASS}, replace_server_instances=1)"), "replace_cluster_occurrences"),
    "replace_cluster_occurrences with a computed flag": (with_calls(f".replace_cluster_occurrences({CLUSTER_CLASS}, replace_server_instances=not True)"), "replace_cluster_occurrences"),
    "replace_cluster_occurrences with another keyword": (with_calls(f".replace_cluster_occurrences({CLUSTER_CLASS}, x=True)"), "replace_cluster_occurrences"),
    "replace_cluster_occurrences on the Tuya builder": (case(METER, "", "    .replace_cluster_occurrences(TuyaMCUCluster)\n"), "builder method replace_cluster_occurrences"),
    # firmware_version_filter: literal integers and a bool, keywords only.
    "firmware_version_filter without arguments": (with_calls(".firmware_version_filter()"), "firmware_version_filter"),
    "firmware_version_filter positional": (with_calls(".firmware_version_filter(1, 2)"), "firmware_version_filter"),
    "firmware_version_filter with a call": (with_calls(".firmware_version_filter(min_version=int('1'))"), "firmware_version_filter"),
    "firmware_version_filter with a name": (with_calls(".firmware_version_filter(min_version=TUYA)"), "firmware_version_filter"),
    "firmware_version_filter computed": (with_calls(".firmware_version_filter(min_version=0x10 | 1)"), "firmware_version_filter"),
    "firmware_version_filter negative": (with_calls(".firmware_version_filter(min_version=-1)"), "firmware_version_filter"),
    "firmware_version_filter beyond 32 bits": (with_calls(".firmware_version_filter(max_version=0x100000000)"), "firmware_version_filter"),
    "firmware_version_filter a float": (with_calls(".firmware_version_filter(min_version=1.0)"), "firmware_version_filter"),
    "firmware_version_filter a bool version": (with_calls(".firmware_version_filter(min_version=True)"), "firmware_version_filter"),
    "firmware_version_filter a text": (with_calls(".firmware_version_filter(min_version='1')"), "firmware_version_filter"),
    "firmware_version_filter of an empty range": (with_calls(".firmware_version_filter(min_version=5, max_version=5)"), "min_version not below max_version"),
    "firmware_version_filter of an upside down range": (with_calls(".firmware_version_filter(min_version=9, max_version=5)"), "min_version not below max_version"),
    "firmware_version_filter allow_missing a number": (with_calls(".firmware_version_filter(min_version=1, allow_missing=1)"), "allow_missing"),
    "firmware_version_filter allow_missing alone": (with_calls(".firmware_version_filter(allow_missing=True)"), "firmware_version_filter"),
    "firmware_version_filter with another keyword": (with_calls(".firmware_version_filter(min_version=1, x=2)"), "firmware_version_filter"),
    "firmware_version_filter on the Tuya builder": (case(METER, "", "    .firmware_version_filter(min_version=1)\n"), "builder method firmware_version_filter"),
    # zhaquirks keeps the last call's range only, the change list shows each call.
    "firmware_version_filter twice": (with_calls(".firmware_version_filter(min_version=0x100, max_version=0x200, allow_missing=False)\n.firmware_version_filter(min_version=0)"),
                                      "more than one firmware_version_filter"),
    "firmware_version_filter twice, equal": (with_calls(".firmware_version_filter(min_version=1)\n.firmware_version_filter(min_version=1)"), "more than one firmware_version_filter"),
    # A keyword given twice: the first value would go unread where a checker keeps the last one in a dict.
    "prevent_default_entity_creation with a repeated keyword": (with_calls(".prevent_default_entity_creation(endpoint_id=1, cluster_id=0x0500, cluster_id=0x0500)"), "prevent_default_entity_creation with a repeated keyword"),
    "prevent_default_entity_creation with a disguised first value": (with_calls(".prevent_default_entity_creation(cluster_id=__import__('os').system('id'), cluster_id=1)"), "prevent_default_entity_creation with a repeated keyword"),
    "prevent_default_entity_creation with a repeated suffix": (with_calls(".prevent_default_entity_creation(cluster_id=1, unique_id_suffix='a', unique_id_suffix='a')"), "prevent_default_entity_creation with a repeated keyword"),
    "removes_entity with a repeated keyword": (with_calls(".removes_entity(EntityFilter(cluster_id=1, cluster_id=2))", IAS), "EntityFilter with a repeated keyword"),
    "removes_entity with a repeated entity class": (with_calls(".removes_entity(EntityFilter(entity_cls=IASZone, entity_cls=IASZone, cluster_id=1))", IAS), "EntityFilter with a repeated keyword"),
    "change_entity_metadata with a repeated keyword": (with_calls(".change_entity_metadata(endpoint_id=1, new_primary=True, new_primary=False)"), "change_entity_metadata with a repeated keyword"),
    "change_entity_metadata with a repeated filter": (with_calls(".change_entity_metadata(endpoint_id=1, endpoint_id=1, new_primary=True)"), "change_entity_metadata with a repeated keyword"),
    "replaces_endpoint with a repeated keyword": (with_calls(".replaces_endpoint(1, device_type=DeviceType.A, device_type=DeviceType.B)", DEVICE_TYPE), "replaces_endpoint with a repeated keyword"),
    "replace_cluster_occurrences with a repeated keyword": (with_calls(f".replace_cluster_occurrences({CLUSTER_CLASS}, replace_server_instances=True, replace_server_instances=False)"), "replace_cluster_occurrences with a repeated keyword"),
    "ReportingConfig with a repeated keyword": (with_sensor("reporting_config=ReportingConfig(min_interval=1, max_interval=900, reportable_change=1, reportable_change=2), ", REPORTING), "ReportingConfig other than the keywords"),
    "ReportingConfig with a repeated keyword in place of another": (with_sensor("reporting_config=ReportingConfig(min_interval=1, min_interval=2, reportable_change=1), ", REPORTING), "ReportingConfig other than the keywords"),
    "friendly_name with a repeated keyword": (with_calls(".friendly_name(model='A', model='B', manufacturer='C')"), "friendly_name other than the keywords"),
    "friendly_name with a repeated keyword in place of the other": (with_calls(".friendly_name(model='A', model='B')"), "friendly_name other than the keywords"),
    "firmware_version_filter with a repeated keyword": (with_calls(".firmware_version_filter(min_version=1, min_version=2)"), "firmware_version_filter other than the keywords"),
    "firmware_version_filter with a repeated allow_missing": (with_calls(".firmware_version_filter(min_version=1, allow_missing=True, allow_missing=False)"), "firmware_version_filter other than the keywords"),
    # Text literals: no control, format, line or paragraph separator character.
    "friendly_name with a bidi override": (with_calls(".friendly_name(manufacturer='Bosch', model='BM\u202eCT')"), "friendly_name other than the keywords"),
    "friendly_name with a line separator": (with_calls(".friendly_name(manufacturer='Bo\u2028sch', model='BMCT')"), "friendly_name other than the keywords"),
    "friendly_name with a paragraph separator": (with_calls(".friendly_name(manufacturer='Bosch', model='BM\u2029CT')"), "friendly_name other than the keywords"),
    "friendly_name with a next line character": (with_calls(".friendly_name(manufacturer='Bosch', model='BM\x85CT')"), "friendly_name other than the keywords"),
    "friendly_name with a zero width space": (with_calls(".friendly_name(manufacturer='Bosch', model='BM\u200bCT')"), "friendly_name other than the keywords"),
    "change_entity_metadata name with a bidi override": (with_calls(".change_entity_metadata(endpoint_id=1, new_fallback_name='Door\u202e')"), "new_fallback_name"),
    "change_entity_metadata name with a line separator": (with_calls(".change_entity_metadata(endpoint_id=1, new_fallback_name='Do\u2028or')"), "new_fallback_name"),
    "change_entity_metadata name with a next line character": (with_calls(".change_entity_metadata(endpoint_id=1, new_fallback_name='Do\x85or')"), "new_fallback_name"),
    "change_entity_metadata placeholder with a bidi override": (with_calls(".change_entity_metadata(endpoint_id=1, new_translation_placeholders={'name': 'Do\u202eor'})"), "new_translation_placeholders"),
    # ReportingConfig: the minimum interval not above the maximum (a maximum of 0 switches the reports off).
    "ReportingConfig with the minimum above the maximum": (with_sensor("reporting_config=ReportingConfig(min_interval=500, max_interval=120, reportable_change=1), ", REPORTING), "min_interval above max_interval"),
    "ReportingConfig with the minimum one above the maximum": (with_sensor("reporting_config=ReportingConfig(min_interval=61, max_interval=60, reportable_change=1), ", REPORTING), "min_interval above max_interval"),
    # An integer beyond the float range gets the range message (math.isfinite of it raises OverflowError).
    "ReportingConfig change beyond the float range": (with_sensor(f"reporting_config=ReportingConfig(min_interval=1, max_interval=900, reportable_change=1{'0' * 400}), ", REPORTING), "reportable_change other than a number literal from 0 to 4294967295"),
    "ReportingConfig change below the float range": (with_sensor(f"reporting_config=ReportingConfig(min_interval=1, max_interval=900, reportable_change=-1{'0' * 400}), ", REPORTING), "reportable_change other than a number literal from 0 to 4294967295"),
    # Masks: x & an integer literal, nothing else, in a converter only.
    "bit or": (with_sensor(L("x | 4")), "operator BitOr"),
    "bit or after a mask": (with_sensor(L("(x & 1) | 2")), "operator BitOr"),
    "exclusive or": (with_sensor(L("x ^ 4")), "operator BitXor"),
    "shift left": (with_sensor(L("x << 40")), "operator LShift"),
    "shift right": (with_sensor(L("x >> 1")), "operator RShift"),
    "invert": (with_sensor(L("~x")), "operator Invert"),
    "mask with x on the right": (with_sensor(L("4 & x")), "& other than x & an integer literal"),
    "mask by a negative number": (with_sensor(L("x & -1")), "& other than x & an integer literal"),
    "mask by a bool": (with_sensor(L("x & True")), "& other than x & an integer literal"),
    "mask beyond 32 bits": (with_sensor(L("x & 0x100000000")), "& other than x & an integer literal"),
    "mask by a float": (with_sensor(L("x & 4.0")), "& other than x & an integer literal"),
    "mask by a text": (with_sensor(L("x & 'a'")), "& other than x & an integer literal"),
    "mask by x": (with_sensor(L("x & x")), "& other than x & an integer literal"),
    "mask by a call": (with_sensor(L("x & int('4')")), "& other than x & an integer literal"),
    "mask by an enum member of the file": (with_sensor(L("x & OnlineState.off"), edits=(ONLINE_STATE,)), "& other than x & an integer literal"),
    "mask of a sum": (with_sensor(L("(x + 1) & 4")), "& other than x & an integer literal"),
    "two masks": (with_sensor(L("x & 3 & 1")), "& other than x & an integer literal"),
    "mask of a conditional": (with_sensor(L("(x if x else 1) & 4")), "& other than x & an integer literal"),
    "mask of two constants": (with_sensor(L("4 & 1")), "& other than x & an integer literal"),
    "mask outside a converter": (case(edits=(("fallback_name='Alarm on motion')", "fallback_name='Alarm on motion', unit=4 & 2)"),)), "operator BitAnd"),
    "mask in a class body": (case(edits=(("cluster_id = 0xFCAC", "cluster_id = 0xFCAC & 0xFFFF"),)), "cluster_id other than"),
    # DataTypeId: a zcl_type of a ZCLAttributeDef, nowhere else.
    "DataTypeId as a type": (case(edits=((FOUNDATION, DATA_TYPE), ("type=t.Bool,", "type=DataTypeId.uint8,"))), "DataTypeId"),
    "DataTypeId as an id": (case(edits=((FOUNDATION, DATA_TYPE), ("id=0x0003", "id=DataTypeId.uint8"))), "DataTypeId"),
    "DataTypeId by itself": (case(edits=((FOUNDATION, DATA_TYPE), ("type=t.Bool,", "type=t.Bool, zcl_type=DataTypeId,"))), "name DataTypeId"),
    "DataTypeId member in upper case": (case(edits=((FOUNDATION, DATA_TYPE), ("type=t.Bool,", "type=t.Bool, zcl_type=DataTypeId.UINT8,"))), "DataTypeId.UINT8"),
    "DataTypeId member zigpy does not have": (case(edits=((FOUNDATION, DATA_TYPE), ("type=t.Bool,", "type=t.Bool, zcl_type=DataTypeId.uint7,"))), "DataTypeId.uint7"),
    "DataTypeId member called": (case(edits=((FOUNDATION, DATA_TYPE), ("type=t.Bool,", "type=t.Bool, zcl_type=DataTypeId.uint8(),"))), "DataTypeId"),
    "DataTypeId member read further": (case(edits=((FOUNDATION, DATA_TYPE), ("type=t.Bool,", "type=t.Bool, zcl_type=DataTypeId.uint8.value,"))), "DataTypeId"),
    "DataTypeId members": (case(edits=((FOUNDATION, DATA_TYPE), ("type=t.Bool,", "type=t.Bool, zcl_type=DataTypeId.__members__,"))), "DataTypeId"),
    "DataTypeId member private": (case(edits=((FOUNDATION, DATA_TYPE), ("type=t.Bool,", "type=t.Bool, zcl_type=DataTypeId._value_,"))), "DataTypeId"),
    "DataTypeId combined": (case(edits=((FOUNDATION, DATA_TYPE), ("type=t.Bool,", "type=t.Bool, zcl_type=DataTypeId.uint8 | DataTypeId.map8,"))), "operator"),
    "DataTypeId under another keyword of the definition": (case(edits=((FOUNDATION, DATA_TYPE), ("manufacturer_code=0x1209", "manufacturer_code=DataTypeId.uint8"))), "DataTypeId"),
    "DataTypeId in a chain": (with_sensor("entity_type=DataTypeId.uint8, ", DATA_TYPE), "DataTypeId"),
    "DataTypeId in a converter": (with_sensor(L("DataTypeId.uint8"), DATA_TYPE), "DataTypeId"),
    "DataTypeId from the foundation module": (case(edits=((FOUNDATION, FOUNDATION + "from zigpy.zcl import foundation\n"), ("type=t.Bool,", "type=t.Bool, zcl_type=foundation.DataTypeId.uint8,"))), "attribute foundation.DataTypeId.uint8"),
    "DataTypeId under another name": (case(edits=((FOUNDATION, "from zigpy.zcl.foundation import BaseAttributeDefs, DataTypeId as D, ZCLAttributeDef\n"), ("type=t.Bool,", "type=t.Bool, zcl_type=D.uint8,"))), "import of DataTypeId as D"),
    "DataTypeId from another module": (case(edits=((FOUNDATION, FOUNDATION + "from zigpy.zcl.clusters.general import DataTypeId\n"), ("type=t.Bool,", "type=t.Bool, zcl_type=DataTypeId.uint8,"))), "DataTypeId"),
    "DataTypeId not imported": (case(edits=(("type=t.Bool,", "type=t.Bool, zcl_type=DataTypeId.uint8,"),)), "DataTypeId"),
    # enum32: members of 32 bits, no other base.
    "enum32 member beyond 32 bits": (case(edits=enum_class("t.enum32", "Random = 0x100000000")), "enum member Random other than an integer from 0 to 4294967295"),
    "enum32 member negative": (case(edits=enum_class("t.enum32", "Random = -1")), "enum member Random"),
    "enum32 member as text": (case(edits=enum_class("t.enum32", "Random = '1'")), "enum member Random"),
    "enum32 member named like a method": (case(edits=enum_class("t.enum32", "serialize = 1")), "enum member name serialize"),
    "enum16 member beyond 16 bits": (case(edits=enum_class("t.enum16", "Random = 0x10000")), "enum member Random other than an integer from 0 to 65535"),
    "enum8 member beyond 8 bits": (case(edits=enum_class("t.enum8", "Random = 0x100")), "enum member Random other than an integer from 0 to 255"),
    "enum32 of big endian": (case(edits=enum_class("t.enum32_be", "Random = 1")), "base class t.enum32_be"),
    "enum32 with a second base": (case(edits=enum_class("t.enum32, int", "Random = 1")), "base class int"),
}


@pytest.mark.parametrize(("text", "reason"), REFUSED.values(), ids=REFUSED)
def test_the_guard_refuses_the_near_misses(text, reason):
    name = METER if "TuyaQuirkBuilder" in text else ZCL
    refused(text, reason, name)


def test_a_file_the_compiler_refuses_is_refused_though_every_check_passes():
    """A keyword given twice in a call no checker keeps in a dict (here a plain entity method) passes every
    check, and compile() rejects the module (zhaquirks.setup would log it): the guard refuses it as well."""
    twice = with_sensor("fallback_name='First', ")
    assert "fallback_name='First', fallback_name='Leak'" in twice
    found = problems(twice)
    assert found and all("does not compile: keyword argument repeated" in problem for problem in found), found
    passes(with_sensor(""))


def test_the_compile_check_costs_next_to_nothing(monkeypatch):
    """The tree is compiled once, only when the checks passed: a refused file does not reach it. It is compiled
    as importlib does, without the future features of guard.py (dont_inherit)."""
    calls = []
    real = compile
    monkeypatch.setattr(guard, "compile", lambda *args, **kw: calls.append((args[1], kw.get("dont_inherit"))) or real(*args, **kw), raising=False)
    passes(with_calls(".friendly_name(manufacturer='Bosch', model='BSEN-W')"))
    assert calls == [("<quirk>", True)]
    calls.clear()
    assert problems(with_calls(".friendly_name(model='A', model='B')"))
    assert calls == []


SENGLED = ".friendly_name(manufacturer='sengled', model='BSEN-W')"


def test_a_name_zha_applies_to_other_devices_stands_only_where_the_devices_have_it():
    """ZHA gives every light group with a "sengled" member its minimum transition: a file must not name another device so,
    also when its entry says that is the vendor. Where every device the file targets is a sengled one the
    name changes nothing."""
    refused(with_calls(SENGLED), "friendly_name with the manufacturer 'sengled', which ZHA applies to every light group", vendor="sengled")
    refused(with_calls(SENGLED.replace("'sengled'", "'Sengled'")), "which ZHA applies to every light group", vendor="Sengled")
    # A dummy fingerprint of the entry does not make it one: the file targets the Bosch device too.
    two = with_calls(SENGLED.replace("'BSEN-W'", "'E11-G13'") + "\n.applies_to('sengled', 'E11-G13')")
    refused(two, "which ZHA applies to every light group", fingerprints=[[None, "RBSH-WS-ZB-EU"], ["sengled", "E11-G13"]])
    only = with_calls(SENGLED, edits=(("QuirkBuilder(None, 'RBSH-WS-ZB-EU')", "QuirkBuilder('sengled', 'RBSH-WS-ZB-EU')"),
                                      ("FINGERPRINTS = [(None, 'RBSH-WS-ZB-EU')]", "FINGERPRINTS = [('sengled', 'RBSH-WS-ZB-EU')]")))
    passes(only, fingerprints=[["sengled", "RBSH-WS-ZB-EU"]], vendor="sengled")


def test_the_names_zha_applies_to_other_devices_are_zhas():
    from zha.application.platforms.light.const import DEFAULT_MIN_TRANSITION_MANUFACTURERS

    assert guard.GROUP_NAMES == {name.casefold() for name in DEFAULT_MIN_TRANSITION_MANUFACTURERS}


def test_a_file_outside_its_manifest_entry_stays_refused_with_the_new_shapes():
    """Nothing the new shapes allow moves what the file targets: the firmware filter only narrows, the other
    shapes name no device. A fingerprint outside the entry is refused beside them."""
    ok = with_calls(".firmware_version_filter(min_version=0x0170044D)\n.friendly_name(manufacturer='Bosch', model='BSEN-W')")
    passes(ok)
    assert problems(ok.replace("FINGERPRINTS = [(None, 'RBSH-WS-ZB-EU')]", "FINGERPRINTS = [(None, 'RBSH-WS-ZB-EU'), (None, 'OTHER')]"))
    assert problems(ok.replace("QuirkBuilder(None, 'RBSH-WS-ZB-EU')", "QuirkBuilder(None, 'OTHER')"))


# The pinned zhaquirks: each keyword the guard allows exists in the real builder, of the kind the guard checks, and the
# calls the guard passes build a quirk.

def test_the_allowed_keywords_are_the_signatures_of_the_pinned_builder():
    import attrs
    from zhaquirks.builder import EntityFilter, QuirkBuilder, ReportingConfig
    from zigpy.zcl.foundation import DataTypeId

    parameters = {name: inspect.signature(getattr(QuirkBuilder, name)).parameters for name in (guard.CHANGE, "friendly_name", "replaces_endpoint", "replace_cluster_occurrences", "firmware_version_filter", guard.HIDE)}
    assert guard.CHANGE_KEYWORDS <= set(parameters[guard.CHANGE])
    assert all(parameters[guard.CHANGE][name].kind is inspect.Parameter.KEYWORD_ONLY for name in guard.CHANGE_KEYWORDS)
    assert set(parameters["friendly_name"]) - {"self"} == {"model", "manufacturer"}
    assert {"endpoint_id", "device_type"} <= set(parameters["replaces_endpoint"])
    assert guard.REPLACE_KEYWORDS <= set(parameters["replace_cluster_occurrences"])
    assert {*guard.FIRMWARE_VERSIONS, "allow_missing"} == set(parameters["firmware_version_filter"]) - {"self"}
    assert [field.name for field in attrs.fields(ReportingConfig)] == list(guard.REPORTING_KEYWORDS)
    assert guard.FILTER_KEYWORDS <= {field.name for field in attrs.fields(EntityFilter)}
    # The method of the pinned builder takes no entity_cls: the guard allows it on EntityFilter only.
    assert "entity_cls" not in parameters[guard.HIDE]
    assert guard.DATA_TYPES == {member.name for member in DataTypeId}


def test_the_entity_class_of_the_allowlist_is_the_class_zha_names():
    import zha.application.platforms.binary_sensor as module

    assert {name for modules in guard.IMPORTS.values() for name, kind in modules.items() if kind == guard.ENTITY_CLASS} == guard.ENTITY_CLASSES
    assert set(guard.IMPORTS["zha.application.platforms.binary_sensor"]) == guard.ENTITY_CLASSES
    assert all(isinstance(getattr(module, name), type) for name in guard.ENTITY_CLASSES)


def test_the_calls_the_guard_passes_build_a_quirk():
    from zha.application.platforms.binary_sensor import IASZone
    from zigpy.profiles.zha import DeviceType
    from zigpy.zcl.clusters.security import IasZone
    from zhaquirks.builder import BinarySensorDeviceClass, EntityFilter, EntityType, QuirkBuilder, ReportingConfig, SensorDeviceClass

    builder = (
        QuirkBuilder("Acme", "X-1")
        .friendly_name(manufacturer="Bosch", model="BMCT-SLZ")
        .exposes_feature("siren_basic")
        .change_entity_metadata(endpoint_id=1, cluster_id=0x0405, new_primary=False, new_entity_category=EntityType.CONFIG, new_device_class=SensorDeviceClass.MOISTURE)
        .change_entity_metadata(endpoint_id=2, cluster_id=0x0500, unique_id_suffix="1280", new_device_class=BinarySensorDeviceClass.DOOR, new_translation_placeholders={"name": "Door"})
        .prevent_default_entity_creation(endpoint_id=35, cluster_id=0x000F)
        .removes_entity(EntityFilter(entity_cls=IASZone, endpoint_id=1, cluster_id=IasZone.cluster_id))
        .replaces_endpoint(1, device_type=DeviceType.DIMMABLE_LIGHT)
        .firmware_version_filter(min_version=0x0170044D, max_version=0xFFFFFFFF, allow_missing=False)
        .binary_sensor("zone_status", IasZone.cluster_id, reporting_config=ReportingConfig(min_interval=30, max_interval=900, reportable_change=1), translation_key="leak", fallback_name="Leak")
    )
    assert builder.firmware_version_min == 0x0170044D and builder.firmware_version_allow_missing is False
    assert len(builder.changed_entity_metadata) == 2 and len(builder.disabled_default_entities) == 2


# Primary entities: zhaquirks makes every entity a change_entity_metadata filter matches explicitly primary. ZHA asserts
# that a device has one such entity at most while it adds the device's entities, at start too, where nothing catches
# it: ZHA would not start. How many entities a filter matches shows only at runtime.
PRIMARY_REFUSED = {
    "on an endpoint": ".change_entity_metadata(endpoint_id=1, new_primary=True)",
    "on a cluster": ".change_entity_metadata(cluster_id=0x0500, new_primary=True)",
    "on a suffix": ".change_entity_metadata(unique_id_suffix='alarm_on_motion', new_primary=True)",
    "on two clusters": ".change_entity_metadata(cluster_id=0x0500, new_primary=True)\n.change_entity_metadata(cluster_id=0x0001, new_primary=True)",
    # ZHA's develco quirks carry this call. Beside an entity of the file on the same IAS zone cluster it matches two.
    "on a cluster ZHA's quirk names, beside an entity of the file there": (
        ".binary_sensor(attribute_name='zone_status', cluster_id=0x0500, attribute_converter=_none_on_error(lambda x: bool(x & 4)), translation_key='tamper', fallback_name='Tamper')\n"
        ".change_entity_metadata(endpoint_id=1, cluster_id=0x0500, new_primary=True)"
    ),
    "beside an entity method's primary": f".sensor({ATTRIBUTE}, {CLUSTER}, primary=True, translation_key='level', fallback_name='Level')\n.change_entity_metadata(endpoint_id=1, new_primary=True)",
}


@pytest.mark.parametrize("calls", PRIMARY_REFUSED.values(), ids=PRIMARY_REFUSED)
def test_no_file_makes_a_second_entity_explicitly_primary(calls):
    refused(with_calls(calls), "change_entity_metadata new_primary other than False")


def test_a_second_primary_entity_method_fails_in_the_builder_before_the_registry():
    """The other way to two explicitly primary entities: zhaquirks' builder refuses a second entity method with a
    true primary when the file is imported, before add_to_registry, so no quirk of the file reaches ZHA. It tests
    the value as ZHA does (truthiness), so a primary of 1 counts as well."""
    from zhaquirks.builder import QuirkBuilder

    for first, second in ((True, True), (1, True), (True, "yes")):
        builder = QuirkBuilder("Acme", "X-1").sensor("battery_voltage", 0x0001, primary=first, translation_key="a", fallback_name="A")
        with pytest.raises(ValueError, match="Only one primary entity"):
            builder.binary_sensor("zone_status", 0x0500, primary=second, translation_key="b", fallback_name="B")


# A reporting config only where the attribute certainly exists on the cluster the entity reads: ZHA looks its name up
# when it configures the device, outside its error handling. An unknown one stops the configuration on join.
REPLACES = f"    .replaces({CLUSTER_CLASS})\n"
GENERAL_LEVEL = "from zigpy.zcl.clusters.general import LevelControl, OnOff\n"


def reported(cls: str = CLUSTER_CLASS, attribute: str = "alarm_on_motion", extra: str = "") -> str:
    """A sensor with a reporting config on an attribute of the class, with more keywords in extra."""
    return f".sensor(attribute_name={cls}.AttributeDefs.{attribute}.name, cluster_id={cls}.cluster_id, {extra}{REPORT}translation_key='level', fallback_name='Level')"


def own_cluster(name: str = "OtherCluster", cluster_id: str | None = "0xFCAD") -> tuple[str, str]:
    """The edit that adds a cluster class of the file before the Bosch cluster: its own cluster_id (or none) and an attribute."""
    cluster = f"\n    cluster_id = {cluster_id}\n" if cluster_id else ""
    return before_cluster(
        f'class {name}(CustomCluster):\n    """Another cluster of the file."""\n{cluster}\n    class AttributeDefs(BaseAttributeDefs):\n'
        f'        """Attribute definitions."""\n\n        level = ZCLAttributeDef(id=0x0001, type=t.uint8_t)'
    )


def standard_cluster(name: str, std: str) -> tuple[str, str]:
    """The edit that adds a class on a zigpy cluster class (its id) with one attribute more, as gen_zcl writes one."""
    return before_cluster(
        f'class {name}(CustomCluster, {std}):\n    """{std} with an attribute more."""\n\n    class AttributeDefs({std}.AttributeDefs):\n'
        f'        """Attribute definitions."""\n\n        extra = ZCLAttributeDef(id=0x4000, type=t.uint8_t)'
    )


REPORTING_PASSES = {
    # ZHA's quirks report on attributes of the class they replace on that endpoint (heiman, sonoff swv, shelly, develco).
    "on the class the chain replaces": with_calls(reported(), REPORTING),
    "on an endpoint where the chain replaces the class": with_calls(reported(extra="endpoint_id=2, "), REPORTING, edits=((REPLACES, f"    .replaces({CLUSTER_CLASS}, endpoint_id=2)\n"),)),
    "on the class the chain adds": with_calls(reported(), REPORTING, edits=((REPLACES, f"    .adds({CLUSTER_CLASS})\n"),)),
    "on the class the chain puts on every endpoint": with_calls(reported(), REPORTING, edits=((REPLACES, f"    .replace_cluster_occurrences({CLUSTER_CLASS})\n"),)),
    "beside a class of another cluster id (shelly ws90: three clusters)": with_calls(f"{reported()}\n.replaces(OtherCluster)", REPORTING, edits=(own_cluster(),)),
    "beside a class of a zigpy cluster": with_calls(f"{reported()}\n.replaces(StdOnOff)", REPORTING + GENERAL_LEVEL, edits=(standard_cluster("StdOnOff", "OnOff"),)),
    "beside a zhaquirks cluster class": with_calls(f"{reported()}\n.adds(DeviceTemperatureCluster)", REPORTING + "from zhaquirks.xiaomi import DeviceTemperatureCluster\n"),
    "beside another class of its id on another endpoint": with_calls(f"{reported()}\n.replaces(OtherCluster, endpoint_id=2)", REPORTING, edits=(own_cluster(cluster_id="0xFCAC"),)),
    "beside a cluster id adds() names (it takes no place)": with_calls(f"{reported()}\n.adds(0xFCAC)", REPORTING),
    # Bosch RTH0: local_temperature comes from zigpy's Thermostat through the nested AttributeDefs.
    "on an attribute a zigpy class gives, beside another zigpy cluster (bosch)": with_calls(
        f"{reported('StdOnOff', 'on_off')}\n.replaces(StdOnOff)\n.replaces(StdLevel)", REPORTING + GENERAL_LEVEL,
        edits=(standard_cluster("StdOnOff", "OnOff"), standard_cluster("StdLevel", "LevelControl")),
    ),
}


@pytest.mark.parametrize("text", REPORTING_PASSES.values(), ids=REPORTING_PASSES)
def test_a_reporting_config_passes_where_the_attribute_certainly_exists(text):
    passes(text)


ON_ENTITY = "a ReportingConfig on an entity other than of the keywords"
REPORTING_REFUSED = {
    "on an attribute named as text the cluster lacks": (with_calls(f".sensor(attribute_name='no_such_attribute', cluster_id=0x0001, {REPORT}translation_key='x', fallback_name='X')", REPORTING), ON_ENTITY),
    "on an attribute of the class with another cluster id": (with_calls(f".sensor({ATTRIBUTE}, cluster_id=0x0001, {REPORT}translation_key='x', fallback_name='X')", REPORTING), ON_ENTITY),
    "on an attribute of an imported zigpy class": (with_calls(f".sensor(attribute_name=OnOff.AttributeDefs.on_off.name, cluster_id=OnOff.cluster_id, {REPORT}translation_key='x', fallback_name='X')", REPORTING + GENERAL), ON_ENTITY),
    # The reporting config first: the entity check reads the other keywords before the guard refuses the path itself.
    "on an attribute of an enum of the file": (with_calls(f".sensor({REPORT}attribute_name=OnlineState.AttributeDefs.off.name, cluster_id=OnlineState.cluster_id, translation_key='x', fallback_name='X')", REPORTING, edits=(ONLINE_STATE,)), ON_ENTITY),
    # Refused before its ReportingConfig is read: an entity method takes keywords only.
    "with positional arguments": (with_calls(f".sensor({CLUSTER_CLASS}.AttributeDefs.alarm_on_motion.name, {CLUSTER_CLASS}.cluster_id, {REPORT}translation_key='x', fallback_name='X')", REPORTING),
                                  "builder method sensor with positional arguments (keywords only)"),
    "on a class the chain does not put on the device": (with_calls(reported("OtherCluster", "level"), REPORTING, edits=(own_cluster(cluster_id="0x0001"),)), "which the chain does not put on endpoint 1"),
    "on another endpoint than the class": (with_calls(reported(extra="endpoint_id=2, "), REPORTING), "which the chain does not put on endpoint 2"),
    "on an endpoint that is no literal": (with_calls(reported(extra="endpoint_id=TUYA_CLUSTER_ID, "), REPORTING + "from zhaquirks.tuya import TUYA_CLUSTER_ID\n"), "of an endpoint that is no integer"),
    "on a client cluster": (with_calls(reported(extra="cluster_type=1, "), REPORTING), "of a client cluster"),
    "on a class the chain puts on a client cluster": (with_calls(reported(), REPORTING, edits=((REPLACES, f"    .replaces({CLUSTER_CLASS}, cluster_type=1)\n"),)), "which the chain does not put on endpoint 1"),
    "on a class put in the place of another cluster id": (with_calls(reported(), REPORTING, edits=((REPLACES, f"    .replaces({CLUSTER_CLASS}, cluster_id=6)\n"),)), "which the chain does not put on endpoint 1"),
    "on a class put on client clusters only": (with_calls(reported(), REPORTING, edits=((REPLACES, f"    .replace_cluster_occurrences({CLUSTER_CLASS}, replace_server_instances=False)\n"),)), "which the chain does not put on endpoint 1"),
    "on a class another class of its id may replace": (with_calls(f"{reported()}\n.replaces(OtherCluster)", REPORTING, edits=(own_cluster(cluster_id="0xFCAC"),)), "while the chain may put OtherCluster in its place"),
    "on a class another class of its id may replace everywhere": (with_calls(f"{reported()}\n.replace_cluster_occurrences(OtherCluster)", REPORTING, edits=(own_cluster(cluster_id="0xFCAC"),)), "may put OtherCluster in its place"),
    "on a class another class of its id may replace on a client cluster or not": (with_calls(f"{reported()}\n.replaces(OtherCluster, cluster_type=0)", REPORTING, edits=(own_cluster(cluster_id="0xFCAC"),)), "may put OtherCluster in its place"),
    "on a class beside a class of no known cluster id": (with_calls(f"{reported()}\n.replaces(OtherCluster)", REPORTING, edits=(own_cluster(cluster_id=None),)), "may put OtherCluster in its place"),
    "on a class of a standard id beside a zigpy class (the same id or not)": (
        with_calls(f"{reported('OtherCluster', 'level')}\n.replaces(OtherCluster)\n.replaces(StdOnOff)", REPORTING + GENERAL_LEVEL, edits=(own_cluster(cluster_id="0x0006"), standard_cluster("StdOnOff", "OnOff"))),
        "may put StdOnOff in its place",
    ),
    "on a class of the id of a zhaquirks class the chain adds": (
        with_calls(f"{reported('OtherCluster', 'level')}\n.replaces(OtherCluster)\n.adds(XiaomiAqaraE1Cluster)", REPORTING + "from zhaquirks.xiaomi import XiaomiAqaraE1Cluster\n", edits=(own_cluster(cluster_id="0xFCC0"),)),
        "may put XiaomiAqaraE1Cluster in its place",
    ),
    # Periodic reports: a maximum of 0 asks for none, else at least a minute (ZHA takes the tightest of all entities).
    "with a periodic report every second": (with_sensor("reporting_config=ReportingConfig(min_interval=0, max_interval=1, reportable_change=0), ", REPORTING), "max_interval other than 0 or 60 to 65535"),
    "with a periodic report just under a minute": (with_sensor("reporting_config=ReportingConfig(min_interval=1, max_interval=59, reportable_change=1), ", REPORTING), "max_interval other than 0 or 60 to 65535"),
}


@pytest.mark.parametrize(("text", "reason"), REPORTING_REFUSED.values(), ids=REPORTING_REFUSED)
def test_a_reporting_config_is_refused_where_the_attribute_may_be_missing(text, reason):
    refused(text, reason, METER if "TuyaQuirkBuilder" in text else ZCL)


def test_the_cluster_ids_of_the_zhaquirks_classes_are_zhaquirks_own():
    """CLUSTER_IDS names, for each zhaquirks cluster class a file may import, where its cluster id comes from. zigpy's
    standard cluster classes have distinct ids below STANDARD_IDS_BELOW: two of them by different names never share
    an id and no manufacturer cluster id is one of theirs."""
    import importlib

    import zigpy.zcl

    standard = {name: cls for module in guard.CLUSTER_MODULES for name, cls in vars(importlib.import_module(module)).items()
                if isinstance(cls, type) and issubclass(cls, zigpy.zcl.Cluster) and cls.__module__ == module}
    ids = [cls.cluster_id for cls in standard.values()]
    assert len(ids) == len(set(ids)) and max(ids) < guard.STANDARD_IDS_BELOW
    assert set(guard.CLUSTER_IDS) == {name for _, name in guard.SKIP_REGISTRY}
    for module, name in guard.SKIP_REGISTRY:
        cls = getattr(importlib.import_module(module), name)
        expected = guard.CLUSTER_IDS[name]
        if expected is None:
            assert getattr(cls, "cluster_id", None) is None, name
        else:
            assert cls.cluster_id == (expected if isinstance(expected, int) else standard[expected].cluster_id), name


# Cluster classes and zigpy's registry: a class without a zhaquirks base would be built for every device's cluster of its id.
EVIL_ON_OFF = (
    'class EvilOnOff({bases}):\n    """Defined, never used by the chain."""\n\n    class AttributeDefs(BaseAttributeDefs):\n'
    '        """Attribute definitions."""\n\n        on_off = ZCLAttributeDef(id=0x0000, type=t.CharacterString)'
)


@pytest.mark.parametrize("bases", ["OnOff", "OnOff, LevelControl"])
def test_a_class_on_zigpy_cluster_classes_alone_is_refused(bases):
    refused(case(imports=GENERAL_LEVEL, edits=(before_cluster(EVIL_ON_OFF.format(bases=bases)),)), "class EvilOnOff without a zhaquirks cluster base")


@pytest.mark.parametrize("bases", ["CustomCluster, OnOff", "OnOff, CustomCluster", "LocalDataCluster, OnOff"])
def test_a_class_with_a_zhaquirks_cluster_base_passes(bases):
    imports = GENERAL_LEVEL + ("from zhaquirks import LocalDataCluster\n" if "LocalDataCluster" in bases else "")
    passes(case(imports=imports, edits=(before_cluster(EVIL_ON_OFF.format(bases=bases)),)))


def test_every_zhaquirks_cluster_base_keeps_a_class_out_of_zigpys_registry():
    """Each zhaquirks cluster class the guard accepts as a base sets _skip_registry, which zigpy reads through the MRO:
    a class on it stays out of the registry also with a zigpy class before it among its bases."""
    import importlib

    from zigpy.zcl import Cluster
    from zigpy.zcl.clusters.general import OnOff

    assert guard.SKIP_REGISTRY
    for module, name in guard.SKIP_REGISTRY:
        assert module.split(".")[0] == "zhaquirks", module
        base = getattr(importlib.import_module(module), name)
        assert base._skip_registry is True, name
    from zhaquirks.clusters import CustomCluster

    before = dict(Cluster._registry)
    cls = type("ZigpyFirst", (OnOff, CustomCluster), {})
    assert cls._skip_registry is True and Cluster._registry == before and Cluster._registry[OnOff.cluster_id] is OnOff


# Text: every literal UTF-8 (orjson, which Home Assistant stores registries with, refuses a lone surrogate), no
# control, format or separator character in a value, the free text keywords of chain calls of their kind.
LEAK = f".sensor({ATTRIBUTE}, {CLUSTER}, translation_key='leak', fallback_name='Leak')"
TEXT_REFUSED = {
    "friendly_name with a lone surrogate (an escape in an ASCII file)": (with_calls(".friendly_name(manufacturer='Bosch\\ud800', model='BSEN-W')"), "a text literal that is no UTF-8"),
    "change_entity_metadata name with a lone surrogate": (with_calls(".change_entity_metadata(endpoint_id=1, new_fallback_name='Door\\udfff')"), "new_fallback_name"),
    "change_entity_metadata placeholder with a lone surrogate": (with_calls(".change_entity_metadata(endpoint_id=1, new_translation_placeholders={'name': 'a\\ud800'})"), "a text literal that is no UTF-8"),
    "an entity's fallback name with a lone surrogate": (with_calls(LEAK.replace("'Leak'", "'Leak\\ud800'")), "fallback_name other than printable text"),
    "an entity's fallback name with a bidi override": (with_calls(LEAK.replace("'Leak'", "'Alarm‮on'")), "fallback_name other than printable text"),
    "an entity's fallback name with a NUL": (with_calls(LEAK.replace("'Leak'", "'Leak\\x00'")), "fallback_name other than printable text"),
    "an entity's fallback name with a line separator": (with_calls(LEAK.replace("'Leak'", "'Le ak'")), "fallback_name other than printable text"),
    "an entity's fallback name that is no text": (with_calls(LEAK.replace("'Leak'", "1")), "fallback_name other than printable text"),
    "an entity's fallback name too long": (with_calls(LEAK.replace("'Leak'", f"'{'L' * 129}'")), "fallback_name other than printable text"),
    "an entity's translation key with a bidi override and a path": (with_calls(LEAK.replace("'leak'", "'alarm‮../x'")), "translation_key other than a string"),
    "an entity's translation key None": (with_calls(LEAK.replace("'leak'", "None")), "translation_key other than a string"),
    "an entity's unique id suffix with a space": (with_calls(LEAK.replace("translation_key=", "unique_id_suffix='a b', translation_key=")), "unique_id_suffix other than a string"),
    "an entity's unit with a control character": (with_calls(LEAK.replace("translation_key=", "unit='\\x07', translation_key=")), "unit other than printable text"),
    "an entity's attribute name as text with a dot": (with_calls(".sensor(attribute_name='a.b', cluster_id=0x0001, translation_key='x', fallback_name='X')"), "attribute_name other than a name"),
    "an entity's command name as text with a space": (with_calls(".command_button(command_name='reset all', cluster_id=0x0006, translation_key='x', fallback_name='X')"), "command_name other than a name"),
    "an entity's placeholders with a value that is not text": (with_calls(LEAK.replace("translation_key=", "translation_placeholders={'a': 1}, translation_key=")), "translation_placeholders other than"),
    "an entity's placeholders with a bidi override": (with_calls(LEAK.replace("translation_key=", "translation_placeholders={'a': 'b‮'}, translation_key=")), "translation_placeholders other than"),
    "a Tuya entity's fallback name with a line separator": (case(METER, edits=(("fallback_name='Reverse energy'", "fallback_name='Reverse energy'"),)), "fallback_name other than printable text"),
    "a Tuya mapping's attribute name with a bidi override": (case(METER, edits=(("attribute_name='current', converter=_value_error_on_error(lambda x: _be(x, 5, 2))", "attribute_name='cur‮rent', converter=_value_error_on_error(lambda x: _be(x, 5, 2))"),)), "attribute_name other than a name"),
    "a text of a class with a bidi override": (case(edits=(("manufacturer_code=0x1209)", "manufacturer_code=0x1209, access='r‮')"),)), "text with a control, format, surrogate"),
    "a text in a converter with a lone surrogate": (with_sensor(L("{0: 'a\\ud800'}[x]")), "a text literal that is no UTF-8"),
    "a device trigger with a line separator": (with_calls(".device_automation_triggers({('remote_button_short_press', 'button '): {'command': 'on'}})"), "text with a control, format, surrogate"),
    "the docstring with a lone surrogate": (case(edits=(('"""Bosch BSEN-W:', '"""Bosch \\ud800 BSEN-W:'),)), "a text literal that is no UTF-8"),
}


@pytest.mark.parametrize(("text", "reason"), TEXT_REFUSED.values(), ids=TEXT_REFUSED)
def test_text_holds_no_character_that_breaks_home_assistant_or_spoofs_a_name(text, reason):
    refused(text, reason, METER if "TuyaQuirkBuilder" in text else ZCL)


TEXT_PASSES = {
    "a fallback name with letters beyond ASCII and punctuation": with_calls(LEAK.replace("'Leak'", "'Wassermelder — Über (1/2)'")),
    "a unit as text": with_calls(LEAK.replace("translation_key=", "unit='°C', translation_key=")),
    "a unit as an imported value": with_calls(LEAK.replace("translation_key=", "unit=PERCENTAGE, translation_key="), "from zhaquirks.builder import PERCENTAGE\n"),
    "an attribute name as text": with_calls(".sensor(attribute_name='battery_voltage', cluster_id=0x0001, translation_key='voltage', fallback_name='Voltage')"),
    "placeholders of an entity": with_calls(LEAK.replace("translation_key=", "translation_placeholders={'name': 'Leak'}, translation_key=")),
}


@pytest.mark.parametrize("text", TEXT_PASSES.values(), ids=TEXT_PASSES)
def test_text_of_quirk_files_passes(text):
    passes(text)


# Friendly names: one of the names the manifest entry gives the device. ZHA matches its per-model rules on them.
TWO_FINGERPRINTS = {"fingerprints": [[None, "RBSH-WS-ZB-EU"], ["Bosch", "RBSH-WS-ZB-EU"]]}
FRIENDLY_PASSES = {
    "the entry's vendor and model": (".friendly_name(manufacturer='Bosch', model='BSEN-W')", {}),
    "a fingerprint of the entry (heiman)": (".friendly_name(manufacturer='Bosch', model='RBSH-WS-ZB-EU')", TWO_FINGERPRINTS),
    "the entry's friendly_name (aqara: Led strip T1)": (".friendly_name(manufacturer='Bosch', model='Wassermänner Über')", {"friendly_name": ["Bosch", "Wassermänner Über"]}),
    "the entry's friendly_name in another case": (".friendly_name(manufacturer='bosch', model='WASSERMELDER')", {"friendly_name": ["Bosch", "Wassermelder"]}),
}
FRIENDLY_REFUSED = {
    "another manufacturer's model (LUMI: no availability pings)": (".friendly_name(manufacturer='LUMI', model='lumi.motion.ac02')", {}),
    "another model of the vendor": (".friendly_name(manufacturer='Bosch', model='BMCT-SLZ')", {}),
    "the entry's model under another manufacturer (sengled: light group transitions)": (".friendly_name(manufacturer='sengled', model='BSEN-W')", {}),
    "the vendor with the model of a wildcard fingerprint": (".friendly_name(manufacturer='Bosch', model='RBSH-WS-ZB-EU')", {}),
    "a friendly_name of the entry with another manufacturer": (".friendly_name(manufacturer='LUMI', model='lumi.motion.ac02')", {"friendly_name": ["LUMI", "lumi.motion.ac02"]}),
    "another name than the entry's friendly_name": (".friendly_name(manufacturer='Bosch', model='Other')", {"friendly_name": ["Bosch", "Wassermelder"]}),
    "a second call with another name": (".friendly_name(manufacturer='Bosch', model='BSEN-W')\n.friendly_name(manufacturer='Philips', model='SML001')", {}),
}


@pytest.mark.parametrize(("calls", "changes"), FRIENDLY_PASSES.values(), ids=FRIENDLY_PASSES)
def test_a_friendly_name_the_entry_gives_passes(calls, changes):
    passes(with_calls(calls), **changes)


@pytest.mark.parametrize(("calls", "changes"), FRIENDLY_REFUSED.values(), ids=FRIENDLY_REFUSED)
def test_a_friendly_name_of_another_device_is_refused(calls, changes):
    refused(with_calls(calls), "which is no name its manifest entry gives the device", **changes)


@pytest.mark.parametrize("bad", [["Bosch"], "Bosch BSEN-W", ["Bosch", 1], ["", "BSEN-W"], ["Bosch", "BSEN-W", "x"], {"manufacturer": "Bosch", "model": "BSEN-W"}])
def test_an_entry_with_a_friendly_name_in_another_shape_has_no_targets(bad):
    assert guard.Targets.of({**FROZEN[ZCL], "friendly_name": bad}) is None


def test_a_manifest_with_a_friendly_name_in_another_shape_is_bad_content():
    entry = {**FROZEN[ZCL], "friendly_name": ["Bosch", "Wassermelder"]}
    content.check_entry("bosch_bsen_w", entry)
    with pytest.raises(content.BadContent, match="friendly_name missing or malformed"):
        content.check_entry("bosch_bsen_w", {**entry, "friendly_name": ["Bosch"]})


def test_the_names_of_an_entry_are_part_of_the_cache_key():
    assert targets(ZCL).key != targets(ZCL, friendly_name=["Bosch", "Wassermelder"]).key
    assert targets(ZCL).names == {("bosch", "bsen-w")}


# Other values the platforms reject. The narrower Tuya replaces_endpoint.
OTHER_REFUSED = {
    "change_entity_metadata to the standard category (Home Assistant has none)": (with_calls(".change_entity_metadata(endpoint_id=1, new_entity_category=EntityType.STANDARD)"), "new_entity_category other than EntityType.CONFIG or EntityType.DIAGNOSTIC", ZCL),
    "replaces_endpoint with a profile and a device type": (with_calls(".replaces_endpoint(1, profile_id=0x0104, device_type=DeviceType.IAS_ZONE)", DEVICE_TYPE), "replaces_endpoint with a keyword other than device_type", ZCL),
    "Tuya replaces_endpoint with a number device type": (case(METER, "", "    .replaces_endpoint(1, device_type=0x0402)\n"), "replaces_endpoint with a keyword other than device_type", METER),
    "Tuya replaces_endpoint with the endpoint as a keyword": (case(METER, "", "    .replaces_endpoint(endpoint_id=1)\n"), "replaces_endpoint other than an endpoint integer", METER),
    "Tuya replaces_endpoint with a profile and a device type": (case(METER, DEVICE_TYPE, "    .replaces_endpoint(1, 0x0104, 0x0402)\n"), "replaces_endpoint other than an endpoint integer", METER),
}


@pytest.mark.parametrize(("text", "reason", "name"), OTHER_REFUSED.values(), ids=OTHER_REFUSED)
def test_other_values_are_refused_for_the_reason_named(text, reason, name):
    refused(text, reason, name)
    # Two different keywords are no repeated keyword.
    assert not any("repeated keyword" in problem for problem in problems(text, name))
