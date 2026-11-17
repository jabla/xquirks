"""Refuse quirk files that hold anything but the shapes a quirk file of this collection may have.

ZHA imports a quirk file into Home Assistant, so its code runs there. This is the core of the
guard: the integration runs it on every file its change list would write, before the list and
again before the write. A file passes only when each of its statements has one of these shapes:

- the module docstring;
- the fixed code (the upstream-first guard with its imports, the alias registration,
  the helpers a converter may call, the ones it stands in: CONVERTER_HELPERS), equal node for node to the source text in
  fixed_code.json;
- `from X import Y` for a name in IMPORTS or a zigpy cluster class;
- a literal assignment of FINGERPRINTS, CLAIMED_ELSEWHERE, ALIASES or ENDPOINTS, checked as data
  also where it equals a statement of the fixed code (the templates hold empty values there);
- a class with a docstring, nested AttributeDefs and ServerCommandDefs classes of attribute and
  command definitions and the fixed Aqara methods (the file imports what they read); an enum class
  holds integer members the enum can carry, named so that no member stands for anything of the enum
  type (ENUM_MEMBER, ENUM_RESERVED); any other class holds only its data in its allowed
  shape (cluster_id, STRUCT_TAGS, the battery range, _CONSTANT_ATTRIBUTES: integers and
  float factors a cluster or a fixed method computes with) and fixed class statements (STRUCT_TARGETS).
  A class on cluster classes has a zhaquirks cluster class among them (SKIP_REGISTRY), which keeps it
  out of zigpy's registry: zigpy registers any other cluster class for its cluster id and builds that
  cluster of every device from it, whatever the file targets, also when its chain never uses the class.
- `from M import C` for a pair [M, C] of its manifest entry's inherits (Targets.inherits, INHERITED): a class of
  the zhaquirks module of ZHA's own quirk the file takes over, where ZHA's class has behaviour code or class data
  no copy can express (inherit_pairs: C no builtin, no name of IMPORTS). The file imports every pair of the entry,
  each once. C stands only as the one base of one class of the file (QuirkFile.inheriting_class): `class Own(C):`
  with a docstring and at most one nested AttributeDefs, ServerCommandDefs and ClientCommandDefs, each on C's own
  (`class AttributeDefs(C.AttributeDefs):`, so ZHA's definitions stay). Their members are definitions with an
  integer literal id (INHERIT_NESTED). C stands nowhere else (QuirkFile.inheritance): not as a value, in a chain,
  a converter or a helper, not beside another base, not under two classes. Own is a cluster class of the file
  like any other (`.replaces(Own)`, `Own.cluster_id`, `Own.AttributeDefs.x.name`), no class of the file builds on
  it. The guard cannot see C. The integration imports each pair inside Home Assistant before it offers or writes
  the file (inherit.py, with what Own adds and reads: inheritances). It refuses the file unless C is a zhaquirks
  cluster class (zhaquirks' CustomCluster) kept out of zigpy's registry with a cluster id, Own's definitions take
  no name and no id of C's and every name the file reads through Own is Own's or C's. So Own counts as a class
  with a zhaquirks cluster base. Attribute names: no check of the guard looks a name up in a class body. A path
  Own.AttributeDefs.x.name reads x from Own's nested class, which Python resolves through C's at import, before
  add_to_registry (a name both lack fails there as for any class of the file, which the ReportingConfig rule
  below relies on). So an inherited attribute reads like one the file defines. What the guard reads from
  definitions stays unknown for it: Own's cluster id is unknown to the ReportingConfig rival check
  (cluster_identity), as for a zhaquirks class without an id.
- a QuirkBuilder or TuyaQuirkBuilder chain with the allowed methods (METHODS),
  with exactly one .filter(no_builtin_quirk), ending in add_to_registry(). The methods below have a
  checker of their own: every argument of the call is checked there, literals only, nothing computed
  and no name but the ones said (QuirkFile.method_checks, the keywords as zhaquirks' builder has them):
  - .prevent_default_entity_creation (both builders): the keywords endpoint_id (an integer from 1 to 254),
    cluster_id (always, an integer up to 0xFFFF or the cluster_id of a cluster class the file imports or
    defines) and unique_id_suffix (a string of letters, digits, "_" and "-"), nothing else (HIDE).
  - .removes_entity (QuirkBuilder): one EntityFilter(...) (imported from zhaquirks.builder) with those
    three keywords and entity_cls, which names IASZone (zha.application.platforms.binary_sensor, imported
    under that name, ENTITY_CLASSES) and nothing else; cluster_id stays required. This is how zhaquirks
    hides one entity class: prevent_default_entity_creation has no entity_cls parameter, so the guard
    allows none there.
  - .replaces_endpoint (both builders): an endpoint integer from 1 to 254 as the one positional argument
    and at most device_type=DeviceType.MEMBER (zigpy.profiles.zha). On the Tuya builder this is
    narrower than zhaquirks (no endpoint_id keyword, no profile_id, no integer device type), on purpose:
    every generated file writes `.replaces_endpoint(1, device_type=DeviceType.WINDOW_COVERING_DEVICE)`.
    The collection has no other files.
  - .change_entity_metadata (QuirkBuilder): keywords only, from endpoint_id, cluster_id, unique_id_suffix
    (these pick the entities, as for HIDE, at least one of them) and new_primary (False only),
    new_entity_registry_enabled_default (a bool literal), new_translation_key (a string of
    letters, digits, "_" and "-"), new_fallback_name (printable text of 1 to 128 characters),
    new_translation_placeholders (a dict of up to 8 names and such texts), new_device_class (a member of
    BinarySensorDeviceClass, NumberDeviceClass or SensorDeviceClass), new_state_class (a member of
    SensorStateClass) and new_entity_category (EntityType.CONFIG or EntityType.DIAGNOSTIC, the categories
    Home Assistant has), each enum imported from zhaquirks.builder (at least one new_* keyword). No
    function, no cluster_type, no new_unique_id: zhaquirks writes it verbatim into the entity's unique id,
    so a file could name the id of an entity of another platform, such as a ZHA group's. No
    new_primary=True: zhaquirks makes every entity the filter matches explicitly primary. ZHA asserts
    that a device has at most one such entity when it adds the device's entities, also while it loads
    its devices at start, where nothing catches it: ZHA would not start. How many entities a filter
    matches is known at runtime only (a cluster matches every entity that targets it). An entity
    method's primary=True makes one already: zhaquirks' builder refuses a second one.
    Left to the files themselves: a device class of another platform than the entity the filter matches (a
    SensorDeviceClass on a binary sensor) makes Home Assistant drop that one entity when it adds it (the
    platform's device class enum raises, Home Assistant logs it and adds the device's other entities).
    Which platform a filter matches is known at runtime only. The effect stays on the target device.
  - .friendly_name (QuirkBuilder): the keywords model and manufacturer, each a printable text literal, the
    pair one of the names its manifest entry gives the device (Targets).
  - .exposes_feature (QuirkBuilder): one string literal of lower case letters, digits, "_" and "." (FEATURE),
    no config.
  - .replace_cluster_occurrences (QuirkBuilder): one cluster class the file defines itself (an imported
    name is refused: it would replace the clusters of a zigpy class) and replace_server_instances,
    replace_client_instances as bool literals.
  - .firmware_version_filter (QuirkBuilder): the keywords min_version, max_version (integer literals from
    0 to 0xFFFFFFFF, the minimum below the maximum) and allow_missing (a bool literal), at least one of
    the versions. It is one more condition on the devices the file's fingerprints match: it only narrows
    what the file targets (a device outside the range is not matched by the file at all), so Targets
    holds as before. At most one call per chain: zhaquirks keeps only the last call's values, and the
    change list shows every call, so a narrow first call would claim a range the file does not have.
  A keyword given twice in one call is refused by every one of these checkers, and a file that passes all
  checks must also compile (compile() of its tree): a disguised first value never goes unread.
  Any other method call stays outside its builder's list (METHODS).
  The methods without a checker of their own take keywords only, except applies_to, adds, replaces, filter and
  device_automation_triggers (POSITIONAL): a positional argument would skip the check of its keyword. No call
  gives dp_handler (tuya_dp, tuya_dp_multi, tuya_dp_attribute): zhaquirks calls the method of the Tuya cluster
  that it names for each datapoint of that id, any method of the cluster. A number's mode is one of the values
  of Home Assistant's NumberMode (NUMBER_MODES).
- divisor and multiplier of a chain call (QuirkFile.scale): a divisor is an integer or a finite float literal other
  than 0, a multiplier an integer or float literal from -MAX_FACTOR to MAX_FACTOR other than 0. ZHA computes with
  them while it adds the device's entities, also at start inside load_devices, where nothing catches an error
  (`value /= divisor`, `value * multiplier`; an integer multiplier on text would repeat it).
- an entity that computes with its attribute's value there (QuirkFile.entity_value: a sensor with a divisor or a
  multiplier, a number with a multiplier, an enum entity, each also as tuya_*) reads an attribute whose type fits: a
  number type for a divisor or a multiplier (NUMBER_TYPES, the file's enum classes), for an enum entity an unsigned
  integer type of at most the bits of the enum class it names (UNSIGNED_BITS; on the Tuya builder an enum class of the
  file that wide or an enum, bitmap or Bool type only, INTEGER_ONLY: the typed Tuya cluster lets a float into a plain
  integer type and, on its own attributes, a value of the type's bits read signed, which no enum takes). A zigpy enum
  takes every integer of its width, an unknown one as an undefined member, and raises for a negative or wider one, a
  float that is no member's value and text. zigpy converts a ZCL report to the attribute's type, the typed Tuya cluster refuses text and
  keeps the values of an enum, bitmap or Bool type in range, so the cache holds values an entity that reads a type that
  fits takes: an int8s of -1 or a uint16 of 256 no t.enum8 takes, text no division takes, would stop ZHA at every
  start. Where the attribute is:
  - on the Tuya builder: an attribute the chain declares on its Tuya cluster (TUYA_DECLARES, every declaration of the
    name with the same type), read through cluster_id=TUYA_CLUSTER_ID and the name as text on endpoint 1 (or by the
    tuya_* method that declares it).
  - on QuirkBuilder: keywords only, attribute_name=Cls.AttributeDefs.<name>.name and cluster_id=Cls.cluster_id, the
    server cluster of an endpoint (an integer literal, 1 without one) where Cls certainly sits: the chain puts it there
    (as for a ReportingConfig: placed), or Cls is zigpy's own class of that cluster (a class of CLUSTER_MODULES, which
    zigpy builds the device's cluster from). Each class the chain puts there that may take Cls's place (rivals: its
    cluster id may be Cls's) is read the same way: a class of the file or an imported cluster class, whose type of the
    name fits or which lacks the name (ZHA shows no value then).
    The type comes from the nested AttributeDefs of the class (`type=` of the member) where it is the file's and
    defines the name. Else the guard cannot see it (zigpy's class of a standard cluster, a zhaquirks class, ZHA's class
    the file builds on, a class of the file that has the name from such a base) and decides nothing: it hands the type
    to the check inside Home Assistant (attribute_needs, attribute_types.py), which looks the name up in each class it
    may come from (the imported class, or the imported bases of the file's class or of its nested AttributeDefs) and
    refuses the file unless one of them defines it (a rival may lack it) and each type found fits: a subclass of int or
    float for a divisor or a multiplier, an unsigned zigpy integer type of at most the enum's bits for an enum entity.
    A class of the file the entity names that neither defines the name nor has an imported base it may come from is
    refused here.
- a TuyaQuirkBuilder chain ends in add_to_registry(replacement_cluster=Cls): Cls a class of the file on TuyaMCUCluster
  alone with a docstring and the fixed method TYPED_TUYA (fixed_code.json), nothing else (QuirkFile.typed_tuya).
  zhaquirks builds the device's Tuya cluster on it, so every datapoint passes that method before zhaquirks stores its
  values: a value for an attribute of a number type must be a number its type holds, for an enum, bitmap or Bool type
  (which ZHA looks values up in) an int that type holds, else ValueError, and zhaquirks skips the datapoint with a
  warning (the attributes keep their values, the frame's other datapoints still count). A plain integer type of
  another cluster holds its range, as zigpy stores a ZCL report: ZHA's own entities read such an attribute as its ZCL
  type, and not only with arithmetic (Illuminance raises 10 to the value, a UTCTime adds it to a date: a larger value
  raises there). On the Tuya cluster itself (the file's entities read its attributes with plain arithmetic) it holds
  what its bits hold read signed or unsigned: a VALUE datapoint is signed on the wire, a sensor typed unsigned may read
  below zero. A float type holds what a single precision float holds. A device chooses the type of each datapoint it
  sends: a STRING or RAW one where a number is meant reached the attribute cache as text, zigpy's database keeps it,
  and at the next start ZHA computes the entity states from it while it loads its devices, where nothing catches an
  error (a divisor, an enum, ZHA's own sensors of a standard cluster). The check covers every mapping of the chain, zhaquirks' own
  converters (tuya_temperature's `x * scale`) and the methods that take none (tuya_number, tuya_enum) among them, and
  turns any error of a converter into a ValueError too. The method reads zhaquirks' internals: inside Home Assistant a
  self-test runs the class on the zhaquirks ZHA loaded (typed_tuya.py), and while it fails no Tuya file is installed or
  updated.
- reporting_config of a chain call is None or ReportingConfig(min_interval=15, max_interval=300,
  reportable_change=1) (imported from zhaquirks.builder), nothing else, the latter only on an entity
  method (binary_sensor, enum, number, sensor, switch) of a QuirkBuilder chain: the three keywords, the
  intervals integer literals from 0 to 0xFFFF, the maximum 0 (no periodic report) or at least
  MIN_MAX_INTERVAL seconds (ZHA configures the tightest reporting of all entities of an attribute, its own
  among them, so a report every second would load the mesh for good), the minimum not above the maximum
  unless that is 0, the change an integer or float literal from 0 to 0xFFFFFFFF. And only where the
  attribute certainly exists on the cluster the entity reads: ZHA looks its name up there when it
  configures the device. An unknown name stops the device's configuration when it joins. So the
  entity method takes keywords only, attribute_name=Cls.AttributeDefs.<name>.name and cluster_id=Cls.cluster_id
  of one cluster class Cls the file defines (a name the class lacks fails at import, before
  add_to_registry), no cluster_type (the server cluster) and endpoint_id an integer literal or none (1).
  The same chain puts Cls there: replaces(Cls) or adds(Cls) with at most that endpoint_id or
  replace_cluster_occurrences(Cls) for server instances. No other class the chain puts on that endpoint
  may have Cls's cluster id: each one's id must certainly differ (QuirkFile.cluster_identity: a cluster_id
  literal or the zigpy class or zhaquirks class it has its id from, CLUSTER_IDS). Not on the Tuya builder:
  it adds clusters of its own when it registers, which the file does not show. No quirk of ZHA's Tuya
  builder sets a reporting config. ReportingConfig stands nowhere else.

No class or import takes a name the fixed code binds or a builtin name (BUILTIN_NAMES): the fixed code reads
builtins as module globals, a class Exception of the file would replace the one its `except Exception` catches and
switch off every fixed helper. Nothing but further chains follows the first
chain: add_to_registry() ran there, so a name bound below it would be unbound when ZHA calls the
quirk if a statement in between failed at import. A file with a chain binds every name the fixed
filter reads when ZHA calls it (FILTER_NEEDS): one missing would raise there, where nothing
catches it. So these names come only from the fixed code and the literals
checked as data, ENDPOINTS among them: every file with a chain has a runtime check.

What a file targets stays inside the manifest entry it is offered for (Targets): the builder's
fingerprints, applies_to, FINGERPRINTS and the fingerprints of ALIASES are literals among the
entry's fingerprints (a manufacturer wildcard only where the entry has it), each ALIASES group
lies inside one alias group of the entry, CLAIMED_ELSEWHERE (empty without one) equals the entry's
claimed_elsewhere, ENDPOINTS (the endpoints the runtime check needs, absent from an alias file)
equals the entry's endpoints. The ZHA classes a file builds on are exactly the entry's inherits (an entry
with over_zha only). The filter variant that keeps the file for its exact names over ZHA's own quirk
(fixed code "take_over") stands in a file exactly when its entry has over_zha. The integration
keeps the entry's names as what an installed file claims: they cover what the file registers.
A friendly_name is one of the names the entry gives the device, ignoring case (Targets.names): the
entry's vendor and model, one of its fingerprints with a manufacturer or its friendly_name (the
[manufacturer, model] ZHA's own quirk shows, which the entry holds where that name
is none of the others, with the vendor or a fingerprint's manufacturer). ZHA matches its own
per-model entity rules and behaviour on the names a quirk gives (no availability pings for "LUMI",
the transition of every light group a "sengled" device is in). The integration reads the device
registry's names when ZHA has no device for it. The entry comes with the file, so this rule keeps a
publishing mistake out, not a hostile entry: its vendor is free text. What ZHA does for a name on the
device itself stays on the devices the file targets, which the file acts on anyway. The one rule ZHA
applies to other devices by a name is closed on its own (GROUP_NAMES): a friendly_name with such a
manufacturer stands only in a file whose every fingerprint has that manufacturer, so the name changes
nothing. Without an entry a file is refused.

Every string literal of the file encodes as UTF-8. Home Assistant stores names with orjson, which
refuses a lone surrogate (Unicode category Cs): a registry that holds one would never save again.
The text values of classes and chains hold no control, format, surrogate, line separator or paragraph
separator character (Unicode categories Cc, Cf, Cs, Zl, Zp: no bidi override, no U+2028, no U+0085).
The free text keywords of chain calls (TEXT_KEYWORDS) take text literals of their kind. fallback_name
is printable text of 1 to 128 characters (TEXT). translation_key and unique_id_suffix are letters,
digits, "_" and "-" (SUFFIX). These three take nothing but such a literal (TEXT_ONLY).
translation_placeholders is a dict of up to 8 such names with such texts. A text literal is TEXT for
unit and a name (NAME) for attribute_name, command_name, ep_attribute and invert_attribute_name, which
also take what a value may be (below).
Values in classes and chains are literals, the names the file imports or defines and the
allowed attribute paths: cluster ids, attribute and command names, enum members,
zigpy types. Converters are `lambda x:` over arithmetic, comparisons and calls of a short list
(bool, int, str, t.Bool, the file's enum classes and helpers, TuyaCoverControl); a product is x
times a number literal, an integer one of at most MAX_FACTOR, one product per Tuya converter
(`converter=`) at most and none in an attribute_converter or dp_converter; other arithmetic has a
number literal on one side; a mask is `x & <integer literal>` (BitAnd only, x on the left, the literal
from 0 to 0xFFFFFFFF, MASK_RANGE; never `|`, `^` or a shift); a converter holds no list, tuple or set and x
stands in a dict only as the key looked up.
Two converters stand inside a fixed helper each (CONVERTER_HELPERS). An attribute_converter stands in SAFE,
`_none_on_error(lambda x: ...)`, which returns None where the lambda raises. ZHA computes an entity's state with it
while it adds the device's entities, also while it loads its devices at start, where nothing catches an error: one
value the converter cannot take would stop ZHA for every device. A Tuya converter (`converter=`) stands in
VALUE_ERROR, `_value_error_on_error(lambda x: ...)`, which raises ValueError (from the error) where the lambda
raises. zhaquirks catches only AttributeError, KeyError and ValueError of a datapoint. On a ValueError it logs a
warning, the attribute keeps its value and the frame's other datapoints still count. Any other error would drop
them and reach the radio library. Each helper is refused in the other's place. A dp_converter stays a plain lambda:
zigpy's listener_event catches its errors. A file that wraps a converter defines its helper by its fixed code. The
helpers stand nowhere else, not inside a converter either (it would return a function as the value).

Types: DataTypeId (zigpy.zcl.foundation) stands only as `zcl_type=DataTypeId.member` of a ZCLAttributeDef,
nowhere else, the member one of DATA_TYPES (zigpy's list). An enum class may have t.enum8, t.enum16 or t.enum32 as
its base, t.enum32 is also a type name, and its members are integers up to 0xFFFFFFFF (ENUM_BITS).
Nothing else: no other statement, import, call, builtin or attribute, no dunder
name, no name bound twice, no coding declaration.

Standard library only, no Home Assistant import: it can be loaded by its path. fixed_code.json
ships with the integration, is parsed here with
the running Python (ast.dump differs between Python versions) and never comes from a content tag.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import json
import math
import pathlib
import re
import unicodedata

HERE = pathlib.Path(__file__).resolve().parent
FIXED_CODE = "fixed_code.json"
# Reasons kept per refused file.
MAX_PROBLEMS = 20
# The largest integer a converter multiplies x by. A Tuya string or raw value has at most 255 bytes
# (zhaquirks reads its length as one byte), so a device that sends one where a number is meant makes
# a Tuya converter repeat it to at most 255 kB: it holds one product at most, no other arithmetic
# joins two such values and no container holds one (QuirkFile.converter, QuirkFile.expr). Only the
# Tuya converters (`converter=`) get such a value: an attribute_converter gets a ZCL attribute, which
# a device may send as a long string, a dp_converter the value written. Neither may hold a product.
# Quirk files stay below the limit: a larger integer scale is a float there (a string times a float
# raises); a climate datapoint that would need a larger factor is left out.
MAX_FACTOR = 1000

# What a quirk may do with a name it imports.
BUILDER = "builder"  # start a chain: QuirkBuilder(...)
DEFINITION = "definition"  # call it in a class body: ZCLAttributeDef(...)
MAPPING = "mapping"  # call it in a chain argument: DPToAttributeMapping(...)
CONVERTER = "converter"  # call it in a converter: TuyaCoverControl(...)
ENUM = "enum"  # read its members: EntityType.CONFIG
VALUE = "value"  # use the name itself: TUYA_CLUSTER_ID
CLUSTER = "cluster"  # a cluster class: base, added cluster, its ids and attribute names
ATTRIBUTE_DEFS = "attribute defs"  # the base of a nested AttributeDefs class
COMMAND_DEFS = "command defs"  # the base of a nested ServerCommandDefs class
FOUNDATION = "foundation"  # zigpy's foundation module: foundation.ZCLAttributeAccess.Read
REPORTING = "reporting"  # ReportingConfig(...) as the reporting_config of an entity method of a chain
DATA_TYPE = "data type"  # DataTypeId.uint8 as the zcl_type of a ZCLAttributeDef
ENTITY_CLASS = "entity class"  # zha's entity class an EntityFilter names: entity_cls=IASZone
ENTITY_FILTER = "entity filter"  # EntityFilter(...) as the one argument of removes_entity
INHERITED = "inherited"  # a class of ZHA's own quirk the entry names (Targets.inherits): the one base of one class

# The `from X import Y` a quirk file may hold, with what it may do with Y.
IMPORTS: dict[str, dict[str, str]] = {
    "zhaquirks.builder": {
        "QuirkBuilder": BUILDER,
        **dict.fromkeys(("EntityPlatform", "EntityType", "SensorStateClass", "SensorDeviceClass", "BinarySensorDeviceClass", "NumberDeviceClass"), ENUM),
        "ReportingConfig": REPORTING,
        "EntityFilter": ENTITY_FILTER,
        **dict.fromkeys(
            (
                "UnitOfElectricCurrent", "UnitOfElectricPotential", "UnitOfEnergy", "UnitOfFrequency", "UnitOfLength", "UnitOfPower",
                "UnitOfPressure", "UnitOfSoundPressure", "UnitOfTemperature", "UnitOfTime", "UnitOfVolume",
            ),
            ENUM,
        ),
        **dict.fromkeys(
            (
                "PERCENTAGE", "LIGHT_LUX", "SIGNAL_STRENGTH_DECIBELS", "CONCENTRATION_PARTS_PER_MILLION", "CONCENTRATION_PARTS_PER_BILLION",
                "CONCENTRATION_MICROGRAMS_PER_CUBIC_METER", "CONCENTRATION_MILLIGRAMS_PER_CUBIC_METER",
            ),
            VALUE,
        ),
    },
    "zhaquirks.tuya.builder": {"TuyaQuirkBuilder": BUILDER},
    "zhaquirks.tuya": {"TUYA_CLUSTER_ID": VALUE},
    "zhaquirks.tuya.mcu": {"DPToAttributeMapping": MAPPING, "TuyaCoverControl": CONVERTER, "TuyaMCUCluster": CLUSTER, "TuyaWindowCovering": CLUSTER},
    "zhaquirks.tuya.tuya_motion": {"TuyaOccupancySensing": CLUSTER},
    "zhaquirks.tuya.tuya_trv": {"TuyaThermostatV2": CLUSTER},
    "zhaquirks": {"LocalDataCluster": CLUSTER},
    "zhaquirks.clusters": {"CustomCluster": CLUSTER},
    "zhaquirks.const": dict.fromkeys(
        (*(f"BUTTON_{n}" for n in range(1, 9)), "COMMAND", "COMMAND_HOLD", "COMMAND_SINGLE", "COMMAND_DOUBLE", "COMMAND_TRIPLE", "COMMAND_RELEASE"), VALUE
    ),
    "zhaquirks.xiaomi": dict.fromkeys(("XiaomiAqaraE1Cluster", "DeviceTemperatureCluster", "XiaomiPowerConfiguration", "ElectricalMeasurementCluster", "MeteringCluster"), CLUSTER),
    "zhaquirks.xiaomi.aqara.opple_remote": {"MultistateInputCluster": CLUSTER},
    "zigpy.profiles.zha": {"DeviceType": ENUM},
    "zigpy.zcl": {"foundation": FOUNDATION},
    "zigpy.zcl.foundation": {
        "ZCLAttributeDef": DEFINITION, "ZCLCommandDef": DEFINITION, "BaseAttributeDefs": ATTRIBUTE_DEFS, "BaseCommandDefs": COMMAND_DEFS, "Direction": ENUM,
        "DataTypeId": DATA_TYPE,
    },
    # The entity classes an EntityFilter may name (ENTITY_CLASSES): zha already runs when a quirk file loads.
    "zha.application.platforms.binary_sensor": {"IASZone": ENTITY_CLASS},
    "zigpy.zcl.clusters.hvac": {"RunningState": ENUM},
}
# Any class of these modules is a cluster (gen_zcl imports zigpy's class of a standard cluster).
CLUSTER_MODULES = frozenset(
    f"zigpy.zcl.clusters.{name}"
    for name in ("closures", "general", "greenpower", "homeautomation", "hvac", "lighting", "lightlink", "measurement", "protocol", "security", "smartenergy")
)
# The zhaquirks cluster classes (the CLUSTER imports of IMPORTS): each sets _skip_registry (zhaquirks' CustomCluster),
# so zigpy keeps a class on one of them out of its registry of cluster classes. A class on zigpy's classes alone
# would take that place: zigpy builds every device's cluster of its id from the registry (a test keeps the flag).
SKIP_REGISTRY = frozenset((module, name) for module, names in IMPORTS.items() for name, kind in names.items() if kind == CLUSTER)
# Where each of them has its cluster id from: an integer, the zigpy cluster class of the same id (by name, as a
# class of CLUSTER_MODULES) or None for none (a class on it names its id). A test keeps it equal to zhaquirks.
CLUSTER_IDS: dict[str, int | str | None] = {
    "CustomCluster": None, "LocalDataCluster": None, "TuyaMCUCluster": 0xEF00, "TuyaWindowCovering": "WindowCovering",
    "TuyaOccupancySensing": "OccupancySensing", "TuyaThermostatV2": "Thermostat", "XiaomiAqaraE1Cluster": 0xFCC0,
    "DeviceTemperatureCluster": "DeviceTemperature", "XiaomiPowerConfiguration": "PowerConfiguration",
    "ElectricalMeasurementCluster": "ElectricalMeasurement", "MeteringCluster": "Metering", "MultistateInputCluster": "MultistateInput",
}
# The module of a pair a manifest entry's inherits names: a module of zhaquirks (the one of ZHA's quirk the file takes over),
# each part a name of letters, digits and "_" that starts with a letter, as zhaquirks names its modules (cl_3460L,
# tintE14rgbcct, VZM31SN; a test walks the package). No private module, no dunder name.
INHERIT_MODULE = re.compile(r"zhaquirks(?:\.[A-Za-z][A-Za-z0-9_]*)+")
# The nested classes of a class on an inherited one (QuirkFile.inheriting_class), each with the definition its members
# are and the range of their integer literal ids: the integration compares names and ids with ZHA's class.
INHERIT_NESTED = {"AttributeDefs": ("ZCLAttributeDef", range(0x10000)), "ServerCommandDefs": ("ZCLCommandDef", range(0x100)),
                  "ClientCommandDefs": ("ZCLCommandDef", range(0x100))}
# zigpy's cluster classes of CLUSTER_MODULES have distinct cluster ids, all below this one (a test keeps both true):
# two of them by different names never share an id. A manufacturer's cluster id is none of theirs.
STANDARD_IDS_BELOW = 0x1001
# The builder method that keeps ZHA from creating its own entities: on a local stand-in cluster ZHA
# shows settings of the real cluster that never reach the device. Only the keywords of HIDE_KEYWORDS,
# each a literal of its kind, the cluster always named: no filter function, no cluster type.
HIDE = "prevent_default_entity_creation"
HIDE_KEYWORDS = frozenset({"endpoint_id", "cluster_id", "unique_id_suffix"})
# The same with the entity class it hides: removes_entity(EntityFilter(entity_cls=IASZone, ...)), the form
# zhaquirks has for it (prevent_default_entity_creation takes no entity_cls). Only entity classes a
# quirk file may name, each imported under its own name.
REMOVE = "removes_entity"
FILTER_KEYWORDS = HIDE_KEYWORDS | {"entity_cls"}
# The entity class to hide by the class when the cluster of its endpoint also holds the file's own entity
# (an IAS zone sensor that replaces the default one under the same unique id suffix).
# Each one is an import of its module in IMPORTS (kind ENTITY_CLASS).
ENTITY_CLASSES = frozenset({"IASZone"})
# A unique id suffix as ZHA writes one (pir_o_to_u_delay): letters, digits, "_" and "-".
SUFFIX = re.compile(r"[A-Za-z0-9_-]{1,64}")
# The builder methods a chain may call, by builder (every chain also calls the filter).
_SHARED = ("applies_to", "adds", "binary_sensor", "enum", "filter", "sensor", "switch", "add_to_registry")
# Methods of QuirkBuilder with a checker of their own (QuirkFile.method_checks): they take literals of
# the kinds their checker names, every one of their arguments is checked there.
REPLACES_ENDPOINT = "replaces_endpoint"
CHANGE = "change_entity_metadata"
METHODS = {
    "QuirkBuilder": frozenset(
        (
            *_SHARED, "replaces", "number", "write_attr_button", "command_button", "device_automation_triggers",
            HIDE, REMOVE, REPLACES_ENDPOINT, CHANGE, "friendly_name", "exposes_feature", "replace_cluster_occurrences", "firmware_version_filter",
        )
    ),
    "TuyaQuirkBuilder": frozenset(
        (
            # HIDE: the Tuya occupancy stand-in hides the PIR delay numbers of the cluster it replaces.
            *_SHARED, HIDE, REPLACES_ENDPOINT, "skip_configuration", "tuya_attribute", "tuya_binary_sensor", "tuya_cover", "tuya_dp", "tuya_dp_attribute",
            "tuya_dp_multi", "tuya_enum", "tuya_illuminance", "tuya_number", "tuya_onoff", "tuya_sensor", "tuya_switch",
            # gen_tuya.STANDARD_NUMERIC
            "tuya_battery", "tuya_co2", "tuya_humidity", "tuya_soil_moisture", "tuya_temperature",
        )
    ),
}
# The entity methods of a chain that take reporting_config=ReportingConfig(...) (zhaquirks' signatures).
REPORTING_METHODS = frozenset({"binary_sensor", "enum", "number", "sensor", "switch"})
REPORTING_KEYWORDS = ("min_interval", "max_interval", "reportable_change")
# The largest reportable change a ReportingConfig names (intervals: ZCL's 16 bit seconds).
MAX_CHANGE = 0xFFFFFFFF
# The shortest periodic report a ReportingConfig asks for, in seconds (a maximum interval of 0 asks for none). ZHA's
# own quirks use 120 at least. ZHA configures the tightest reporting of all entities of an attribute.
MIN_MAX_INTERVAL = 60
# The builder methods that put a cluster class on a device, with the keyword each takes the class as.
PLACEMENTS = {"adds": "cluster", "replaces": "replacement_cluster_class", "replace_cluster_occurrences": "replacement_cluster_class"}
# A feature name as zha writes one: "siren_basic", "bega.light_switchable_white".
FEATURE = re.compile(r"[a-z0-9][a-z0-9_.]{0,63}")
# A device name (friendly_name, a fallback name): printable text, no longer than any real one.
TEXT = re.compile(r"[^\x00-\x1f\x7f]{1,128}")
# Unicode categories no text value may hold (QuirkFile.text, QuirkFile.expr): control, format (bidi overrides, zero
# width characters), surrogates, line and paragraph separators (U+2028, U+2029). A name that shows other than
# what it says or breaks a line is display spoofing. A lone surrogate is no UTF-8: orjson, with which Home
# Assistant stores its registries, refuses it. The registry that holds such a name would never save again.
FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
# The keywords of change_entity_metadata, each with the kind its checker reads: the entities it
# matches (the first three, at least one) and what it changes to (new_*, at least one). Enum members
# only of the enums named here, which the file imports: a device class, a state class, an entity category.
CHANGE_FILTERS = frozenset({"endpoint_id", "cluster_id", "unique_id_suffix"})
CHANGE_ENUMS = {
    "new_device_class": ("BinarySensorDeviceClass", "NumberDeviceClass", "SensorDeviceClass"),
    "new_state_class": ("SensorStateClass",),
    "new_entity_category": ("EntityType",),
}
# The members an enum keyword is limited to: Home Assistant's entity categories (EntityType.STANDARD is none of
# them: Home Assistant drops an entity with it when it adds the entity).
CHANGE_MEMBERS = {"new_entity_category": frozenset({"CONFIG", "DIAGNOSTIC"})}
# What a change_entity_metadata value must be, for the message of a refusal.
CHANGE_KINDS = {
    "endpoint_id": "an integer from 1 to 254", "cluster_id": "an integer from 0 to 0xFFFF or a cluster class's cluster_id",
    "unique_id_suffix": "a string of letters, digits, _ and -",
    "new_translation_key": "a string of letters, digits, _ and -",
    "new_primary": "False (a second explicitly primary entity stops ZHA)", "new_entity_registry_enabled_default": "a bool literal",
    "new_fallback_name": "a text literal", "new_translation_placeholders": "a dict of up to 8 names and text literals",
    "new_device_class": "a member of a device class enum", "new_state_class": "a member of SensorStateClass",
    "new_entity_category": "EntityType.CONFIG or EntityType.DIAGNOSTIC",
}
CHANGE_TEXT = frozenset({"new_translation_key"})
CHANGE_BOOL = frozenset({"new_entity_registry_enabled_default"})
CHANGE_KEYWORDS = CHANGE_FILTERS | CHANGE_TEXT | CHANGE_BOOL | {"new_primary", "new_fallback_name", "new_translation_placeholders", *CHANGE_ENUMS}
# The profile device type of replaces_endpoint: DeviceType.DIMMABLE_LIGHT (zigpy.profiles.zha).
DEVICE_TYPE = "DeviceType"
# The keywords of replace_cluster_occurrences and firmware_version_filter.
REPLACE_KEYWORDS = frozenset({"replace_server_instances", "replace_client_instances"})
FIRMWARE_VERSIONS = ("min_version", "max_version")
FIRMWARE_RANGE = range(0x100000000)
# The members of DataTypeId (zigpy.zcl.foundation): DataTypeId.uint8, DataTypeId.bool_. A test keeps the
# list equal to zigpy's.
DATA_TYPES = frozenset({
    "unk", "nodata", "data8", "data16", "data24", "data32", "data40", "data48", "data56", "data64", "bool_", "map8", "map16", "map24", "map32", "map40",
    "map48", "map56", "map64", "uint8", "uint16", "uint24", "uint32", "uint40", "uint48", "uint56", "uint64", "int8", "int16", "int24", "int32", "int40",
    "int48", "int56", "int64", "enum8", "enum16", "semi", "single", "double", "octstr", "string", "octstr16", "string16", "array", "struct", "set", "bag",
    "ToD", "date", "UTC", "clusterId", "attribId", "bacOID", "EUI64", "key128",
})
# The entity methods that compute with their attribute's value while ZHA adds the device's entities (QuirkFile.entity_value):
# a sensor's divisor and multiplier, a number's multiplier (by method: the keywords that scale), an enum entity's class.
SCALED = {"sensor": ("divisor", "multiplier"), "tuya_sensor": ("divisor", "multiplier"), "number": ("multiplier",), "tuya_number": ("multiplier",)}
ENUM_ENTITIES = frozenset({"enum", "tuya_enum"})
SCALES = frozenset({"divisor", "multiplier"})
# The zigpy types (t.X of TYPE_NAMES) whose values are numbers.
NUMBER_TYPES = frozenset({"Bool", "uint8_t", "uint16_t", "uint24_t", "uint32_t", "uint48_t", "int8s", "int16s", "int24s", "int32s", "enum8", "enum16", "enum32",
                          "bitmap8", "bitmap16", "Single"})
# Those of them that hold unsigned integers only, with their bits: every value fits an enum class at least that wide. A test
# keeps both lists equal to zigpy.
UNSIGNED_BITS = {"Bool": 8, "uint8_t": 8, "enum8": 8, "bitmap8": 8, "uint16_t": 16, "enum16": 16, "bitmap16": 16, "uint24_t": 24, "uint32_t": 32,
                 "enum32": 32, "uint48_t": 48}
# The ones of them the typed Tuya cluster keeps integers of their range (enums, bitmaps, Bool): it lets a float into a plain
# integer type and, on its own attributes, a value of the type's bits read signed (a negative one into an unsigned type).
INTEGER_ONLY = frozenset({"Bool", "enum8", "enum16", "enum32", "bitmap8", "bitmap16"})
# The attribute each Tuya method declares on the chain's Tuya cluster: (the keyword of its name, its default, the keyword
# of its type, the default type) as zhaquirks' TuyaQuirkBuilder has them (a test keeps them equal).
TUYA_DECLARES = {
    "tuya_attribute": ("attribute_name", None, "type", "t.uint16_t"),
    "tuya_dp_attribute": ("attribute_name", None, "type", "t.uint16_t"),
    "tuya_sensor": ("attribute_name", None, "type", None),
    "tuya_number": ("attribute_name", None, "type", None),
    "tuya_enum": ("attribute_name", None, "enum_class", None),
    "tuya_switch": ("attribute_name", "on_off", None, "t.Bool"),
    "tuya_binary_sensor": ("attribute_name", None, None, "t.Bool"),
}
# The fixed method of the class a Tuya chain registers its Tuya cluster on (add_to_registry(replacement_cluster=...),
# QuirkFile.typed_tuya): zhaquirks calls it for each datapoint, it checks every value against its attribute's type.
TYPED_TUYA = "_dp_2_attr_update"
TUYA_MCU = ("zhaquirks.tuya.mcu", "TuyaMCUCluster")
# The keywords whose value is a converter lambda (and nothing else).
CONVERTER_KEYWORDS = frozenset({"converter", "dp_converter", "attribute_converter"})
# The fixed helpers (fixed_code.json) the converters that ZHA and zhaquirks call where not every error is caught stand in,
# by keyword. SAFE(lambda x: ...) returns None where the lambda raises: ZHA computes an entity's state with its
# attribute_converter while it adds the device's entities (zha/zigbee/device.py, at start inside Gateway.load_devices),
# where an error stops ZHA for every device. VALUE_ERROR(lambda x: ...) raises ValueError from the lambda's error: zhaquirks
# calls a Tuya converter (`converter=`) for each datapoint inside TuyaNewManufCluster.handle_get_data, which catches only
# AttributeError, KeyError and ValueError. A ValueError skips that datapoint with a warning (the attribute keeps its
# value), any other error drops the frame's other datapoints and reaches the radio library. A dp_converter needs none:
# zigpy's listener_event, through which zhaquirks' MCU command bus calls it, catches every error.
SAFE = "_none_on_error"
VALUE_ERROR = "_value_error_on_error"
CONVERTER_HELPERS = {"attribute_converter": SAFE, "converter": VALUE_ERROR}
# The chain methods (without a checker of their own) that take positional arguments, as zhaquirks names them. Every
# other method takes keywords only: a positional argument would skip the check of its keyword (dp_handler, mode, the
# text keywords). Generated files pass positional arguments to no other method.
POSITIONAL = frozenset({"applies_to", "adds", "replaces", "filter", "device_automation_triggers"})
# The keyword of tuya_dp, tuya_dp_multi and tuya_dp_attribute that names the method of the Tuya cluster zhaquirks calls
# for each datapoint of that id (getattr(cluster, dp_handler)(record)): any method of the cluster. No file gives it.
DP_HANDLER = "dp_handler"
# A number entity's mode: the values of Home Assistant's NumberMode (zha's number platform).
NUMBER_MODES = frozenset({"auto", "box", "slider"})
# zigpy types a quirk names as t.X (gen_zcl.ZCL_TYPES and COMMAND_TYPES, gen_tuya's attribute types).
TYPE_NAMES = frozenset(
    {"Bool", "uint8_t", "uint16_t", "uint24_t", "uint32_t", "uint48_t", "int8s", "int16s", "int24s", "int32s", "enum8", "enum16", "enum32", "bitmap8", "bitmap16", "Single",
     "CharacterString", "LVBytes"}
)
ENUM_BASES = ("enum8", "enum16", "enum32")
# Bits an enum base holds in its members.
ENUM_BITS = {"enum8": 8, "enum16": 16, "enum32": 32}
# t.List[t.uint8_t]: herdsman's LIST_UINT8 command parameter.
TYPE_LISTS = ("List",)
# Enums nested in a cluster class: Thermostat.SystemMode.Heat.
CLUSTER_ENUMS = frozenset({"SystemMode"})
# The cluster attribute a dict may spread: {**TuyaThermostatV2._CONSTANT_ATTRIBUTES, ...}.
SPREAD = "_CONSTANT_ATTRIBUTES"
# The data a cluster class holds besides the fixed class statements (STRUCT_TARGETS, the outage
# cluster's), each in its one allowed shape: the cluster or a fixed method computes
# with it and a device value, so a string or a sequence there would repeat a report's value, an
# empty battery range would divide by zero. The Aqara struct tags with the key the Xiaomi base
# routes each one under (gen_zcl.METER_TAGS), each with a float factor (a sequence times a float raises).
STRUCT_TAG_KEYS = {149: "consumption", 150: "voltage", 151: "current", 152: "power"}
# The battery range of zhaquirks' XiaomiPowerConfiguration in mV (gen_zcl: z2m's range).
VOLTS = ("MIN_VOLTS_MV", "MAX_VOLTS_MV")
VOLTS_RANGE = range(1000, 5001)
# The values of _CONSTANT_ATTRIBUTES: setpoint limits in 0.01 °C, a current multiplier and divisor.
CONSTANT_RANGE = range(-0x8000, 0x10000)
# Names an enum member must not take: what zigpy's t.enum8, t.enum16 and t.enum32 (and an Enum member) have,
# which zigpy and the converters call (a test keeps it complete). Names that start
# with an underscore (_missing_, _order_) are refused anyway.
ENUM_RESERVED = frozenset({
    "as_integer_ratio", "bit_count", "bit_length", "bits", "conjugate", "denominator", "deserialize", "from_bits", "from_bytes", "imag",
    "is_integer", "max_value", "min_value", "mro", "name", "numerator", "real", "serialize", "to_bytes", "value",
})
BUILTINS = frozenset({"bool", "int", "str"})
TYPE_CALLS = frozenset({"Bool"})
NESTED = {"AttributeDefs": ATTRIBUTE_DEFS, "ServerCommandDefs": COMMAND_DEFS}
# The fixed function every chain filters with, exactly once.
FILTER = "no_builtin_quirk"
# What the fixed filter and its helpers read (and their module statements bind): a file with a chain
# binds each of them, so the filter cannot raise NameError inside ZHA's device lookup.
FILTER_NEEDS = frozenset({
    "functools", "logging", "pathlib", "zhaquirks", "DEVICE_REGISTRY", "DeviceMatch", "_LOGGER", "_SHIPPED", "_STEPPED_BACK",
    "FINGERPRINTS", "CLAIMED_ELSEWHERE", "ENDPOINTS", "_is_shipped", "_shipped_quirk", "_misfit", "_upstream_first", FILTER,
})
# The fingerprints a builder takes when it starts its chain (gen_zcl: QuirkBuilder(manufacturer, model)).
ROOT_ARGS = {"QuirkBuilder": 2, "TuyaQuirkBuilder": 0}
# The module-level literals the templates fill with data.
LITERALS = frozenset({"FINGERPRINTS", "CLAIMED_ELSEWHERE", "ALIASES", "ENDPOINTS"})
# The manufacturer names ZHA acts on beyond the device that reports one: each light group with a member of such a name
# gets its minimum transition (zha's DEFAULT_MIN_TRANSITION_MANUFACTURERS, a test keeps it equal). Casefolded.
GROUP_NAMES = frozenset({"sengled"})
# The builtins of the running Python, which ZHA imports the file with. The fixed code reads them as module globals
# (Exception, ValueError, isinstance): a class or an import of the file under one of these names would replace it there.
BUILTIN_NAMES = frozenset(dir(builtins))

# Where a value stands; the imported callables each place may call.
CHAIN, LAMBDA, CLASS = "chain", "lambda", "class"
CALLS = {CHAIN: MAPPING, LAMBDA: CONVERTER, CLASS: DEFINITION}
# BitAnd in a converter only as `x & <integer literal>` (QuirkFile.expr), never BitOr, BitXor or a shift.
OPERATORS = {CHAIN: (ast.BitOr,), LAMBDA: (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.BitAnd), CLASS: ()}
# The integer a converter masks x with.
MASK_RANGE = range(0x100000000)
CMPOPS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)

MEMBER = re.compile(r"[A-Z][A-Za-z0-9_]*")
# An enum member's name in a class of the file: a letter first, or (as ZHA's own quirks name an option
# that starts with a digit) an underscore and a digit, never an Enum hook such as _missing_ or _order_.
ENUM_MEMBER = re.compile(r"[A-Za-z][A-Za-z0-9_]*|_[0-9](?:[A-Za-z0-9_]*[A-Za-z0-9])?")
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
CLASS_NAME = re.compile(r"[A-Z][A-Za-z0-9_]*")
# The free text keywords of chain calls (entity methods, Tuya methods, DPToAttributeMapping) with the pattern a text
# literal there matches (QuirkFile.text). Those of TEXT_ONLY take nothing but such a literal. The others also take
# what QuirkFile.expr allows (an enum member, an imported value, an attribute path). The methods with a checker
# of their own check their keywords there.
TEXT_KEYWORDS = {
    "fallback_name": TEXT, "translation_key": SUFFIX, "unique_id_suffix": SUFFIX, "unit": TEXT,
    "attribute_name": NAME, "command_name": NAME, "ep_attribute": NAME, "invert_attribute_name": NAME,
}
TEXT_ONLY = frozenset({"fallback_name", "translation_key", "unique_id_suffix"})
TEXT_KINDS = {TEXT: "printable text of 1 to 128 characters", SUFFIX: "a string of letters, digits, _ and -", NAME: "a name of letters, digits and _"}
PLACEHOLDERS = "translation_placeholders"
# A coding declaration (PEP 263) on one of the first two lines.
CODING = re.compile(rb"^[ \t\f]*#.*?coding[:=]")
LINE_BREAK = re.compile(rb"\r\n|\r|\n")


class Refused(Exception):
    def __init__(self, node: ast.AST, reason: str) -> None:
        super().__init__(reason)
        self.node = node
        self.reason = reason


# The layout of a content tag this release reads: manifest.json, hacs.json and the file layout.
# Raise it when one of them changes in a way an older release would misread: it is part of the
# guard identity, so older releases skip the tags of the new layout. The fields of
# data/withdrawn.toml (file, revision, reason) never change: every release reads them from a newer tag.
FORMAT = 1


def identity(guard_source: bytes, fixed_source: bytes) -> str:
    """The guard a content tag names in its content.json: the sha256 of FORMAT, this file and
    fixed_code.json as the release ships them. Another guard reads the tag's files otherwise."""
    return hashlib.sha256(f"{FORMAT}\n".encode() + hashlib.sha256(guard_source).digest() + hashlib.sha256(fixed_source).digest()).hexdigest()


def bound_names(stmt: ast.stmt) -> list[str]:
    """The module-level names a statement binds: imports, classes, functions, assignments."""
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return [a.asname or a.name.partition(".")[0] if isinstance(stmt, ast.Import) else a.asname or a.name for a in stmt.names]
    if isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return [stmt.name]
    if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        return [n.id for t in targets for n in (t.elts if isinstance(t, ast.Tuple) else [t]) if isinstance(n, ast.Name)]
    return []


def free_names(function: ast.FunctionDef) -> frozenset[str]:
    """The module-level names a function reads: loaded in it, bound nowhere in it, no builtin."""
    bound = {node.arg for node in ast.walk(function) if isinstance(node, ast.arg)}
    bound |= {node.id for node in ast.walk(function) if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load)}
    bound |= {node.name for node in ast.walk(function) if isinstance(node, ast.ExceptHandler) and node.name}
    loaded = {node.id for node in ast.walk(function) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    return frozenset(loaded - bound - BUILTIN_NAMES)


def _int_in(node: ast.expr, allowed: range) -> bool:
    """An integer literal (a negative one too, no bool) inside the range."""
    negative = isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
    if negative:
        node = node.operand
    if not (isinstance(node, ast.Constant) and type(node.value) is int):
        return False
    return (-node.value if negative else node.value) in allowed


def _utf8(text: str) -> bool:
    """Text that encodes as UTF-8: no lone surrogate."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _struct_tag(key: ast.expr | None, value: ast.expr) -> bool:
    """149: ('consumption', 1.0): a tag of STRUCT_TAG_KEYS, the key routed for it and a finite float
    factor above 0 and at most MAX_FACTOR."""
    if not (isinstance(key, ast.Constant) and type(key.value) is int and key.value in STRUCT_TAG_KEYS):
        return False
    if not (isinstance(value, ast.Tuple) and len(value.elts) == 2):
        return False
    routed, factor_node = value.elts
    return (isinstance(routed, ast.Constant) and routed.value == STRUCT_TAG_KEYS[key.value] and isinstance(factor_node, ast.Constant)
            and type(factor_node.value) is float and math.isfinite(factor_node.value) and 0 < factor_node.value <= MAX_FACTOR)


class Fixed:
    """The fixed code, parsed with the running Python from its source text: module
    statements, the methods of the Aqara cluster class, the helpers a converter may call."""

    def __init__(self, sources: object) -> None:
        if not isinstance(sources, dict) or not all(isinstance(sources.get(key), list) and all(isinstance(text, str) for text in sources[key]) for key in ("module", "helpers", "class", "take_over")):
            raise ValueError("fixed code: module, helpers, class and take_over must be lists of source texts")
        helpers = [stmt for text in sources["helpers"] for stmt in ast.parse(text).body]
        statements = [stmt for key in ("module", "take_over") for text in sources[key] for stmt in ast.parse(text).body] + helpers
        module = frozenset(ast.dump(stmt) for text in sources["module"] for stmt in ast.parse(text).body) | {ast.dump(stmt) for stmt in helpers}
        # The statements only a file that takes devices over from ZHA's own quirk holds.
        self.take_over = frozenset(ast.dump(stmt) for text in sources["take_over"] for stmt in ast.parse(text).body) - module
        self.module = module | self.take_over
        class_statements = [stmt for text in sources["class"] for stmt in ast.parse(text).body[0].body]
        self.methods = frozenset(ast.dump(stmt) for stmt in class_statements)
        # The method the class of a Tuya chain's cluster holds (TYPED_TUYA), None in fixed code without it.
        self.typed_tuya = next((ast.dump(stmt) for stmt in class_statements if isinstance(stmt, ast.FunctionDef) and stmt.name == TYPED_TUYA), None)
        # Its class text, which the self-test inside Home Assistant runs on the zhaquirks ZHA loaded (typed_tuya.py).
        self.typed_tuya_source = next((text for text in sources["class"] if any(isinstance(stmt, ast.FunctionDef) and stmt.name == TYPED_TUYA
                                                                                for stmt in ast.parse(text).body[0].body)), None)
        # What each fixed method reads from the module: a file that holds the method imports each one.
        self.reads = {ast.dump(stmt): free_names(stmt) for stmt in class_statements if isinstance(stmt, ast.FunctionDef)}
        self.helpers = frozenset(stmt.name for stmt in helpers if isinstance(stmt, ast.FunctionDef))
        # The module-level names the fixed code binds: no class or import of a file takes one.
        self.names = frozenset(name for stmt in statements for name in bound_names(stmt))


def _pair(value: object) -> tuple[str | None, str] | None:
    """A fingerprint (manufacturer or None, model) as the manifest holds it, None for anything else."""
    if isinstance(value, (list, tuple)) and len(value) == 2 and (value[0] is None or isinstance(value[0], str)) and isinstance(value[1], str):
        return (value[0], value[1])
    return None


def _pairs(value: object) -> frozenset[tuple[str | None, str]] | None:
    pairs = [_pair(item) for item in value] if isinstance(value, list) else [None]
    return None if None in pairs else frozenset(pairs)


def friendly_pair(value: object) -> tuple[str, str] | None:
    """An entry's friendly_name as the manifest holds it: [manufacturer, model] as two texts, None for anything else."""
    if isinstance(value, list) and len(value) == 2 and all(isinstance(text, str) and text for text in value):
        return (value[0], value[1])
    return None


def inherit_pairs(value: object) -> frozenset[tuple[str, str]] | None:
    """An entry's inherits as the manifest holds it: [[module, class], ...] sorted, no pair twice, each module a zhaquirks
    module (INHERIT_MODULE), each class name CLASS_NAME, no builtin (a module-level import of that name would hide it from
    the fixed code) and none a quirk file imports anyway (IMPORTS: no inheritance). None for anything else."""
    if not isinstance(value, list) or not all(isinstance(pair, list) and len(pair) == 2 and all(isinstance(text, str) for text in pair) for pair in value):
        return None
    pairs = [tuple(pair) for pair in value]
    if pairs != sorted(set(pairs)):
        return None
    if not all(INHERIT_MODULE.fullmatch(module) and CLASS_NAME.fullmatch(name) and name not in BUILTIN_NAMES and not kind_of(module, name) for module, name in pairs):
        return None
    return frozenset(pairs)


def _uint(value: object, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def endpoint_map(value: object, keys: type) -> dict[int, tuple[int, ...]] | None:
    """Endpoint id (1 to 254) -> sorted distinct input cluster ids, as the manifest holds them: keys
    of type int in ascending order in a file's ENDPOINTS (the runtime check reports the first misfit
    in that order), decimal strings in a manifest entry's endpoints; None for anything else."""
    if not isinstance(value, dict) or not value:
        return None
    if keys is int and (not all(_uint(key, 1, 254) for key in value) or list(value) != sorted(value)):
        return None
    out = {}
    for key, ids in value.items():
        if keys is str:
            if not (isinstance(key, str) and key.isascii() and key.isdigit() and str(int(key)) == key):
                return None
            key = int(key)
        if not _uint(key, 1, 254) or not isinstance(ids, (tuple if keys is int else list)):
            return None
        if not all(_uint(i, 0, 0xFFFF) for i in ids) or list(ids) != sorted(set(ids)):
            return None
        out[key] = tuple(ids)
    return out


class Targets:
    """What a manifest entry lets its file target: the entry's fingerprints, the names it leaves to
    other devices (claimed_elsewhere), its alias groups (shipped targets with the fingerprints
    that get them), whether it takes its exact names over from ZHA's own quirk (over_zha), the
    names a friendly_name may give the device (casefolded pairs: names) and the (module, class)
    pairs of ZHA's classes its file builds on (inherits)."""

    def __init__(self, fingerprints: frozenset, groups: tuple[tuple[frozenset, frozenset], ...], claimed: frozenset = frozenset(), takes_over: bool = False,
                 endpoints: dict[int, tuple[int, ...]] | None = None, names: frozenset[tuple[str, str]] = frozenset(),
                 inherits: frozenset[tuple[str, str]] = frozenset()) -> None:
        self.fingerprints = fingerprints
        self.groups = groups
        self.claimed = claimed
        self.takes_over = takes_over
        self.endpoints = endpoints
        self.names = names
        self.inherits = inherits
        # For caches: verdicts depend on the content and on these.
        self.key = repr((sorted(fingerprints, key=repr), [(sorted(t, key=repr), sorted(f, key=repr)) for t, f in groups], sorted(claimed, key=repr), takes_over,
                         sorted((endpoints or {}).items()), sorted(names), sorted(inherits)))

    @staticmethod
    def names_of(entry: dict, fingerprints: frozenset) -> frozenset[tuple[str, str]] | None:
        """The (manufacturer, model) pairs a friendly_name may set, casefolded: the entry's vendor and model, its
        fingerprints with a manufacturer and its friendly_name when its manufacturer is one of those. None when
        friendly_name is not two texts."""
        names = {pair for pair in fingerprints if pair[0] is not None}
        if isinstance(entry.get("vendor"), str) and isinstance(entry.get("model"), str):
            names.add((entry["vendor"], entry["model"]))
        if entry.get("friendly_name") is not None:
            if (friendly := friendly_pair(entry["friendly_name"])) is None:
                return None
            if friendly[0].casefold() in {manufacturer.casefold() for manufacturer, _ in names}:
                names.add(friendly)
        return frozenset((manufacturer.casefold(), model.casefold()) for manufacturer, model in names)

    @classmethod
    def of(cls, entry: object) -> Targets | None:
        """The targets of a manifest entry. None without an entry or when its fingerprints, target,
        alias groups, friendly_name or inherits are not in the shape the manifest holds (inherits on an
        entry with over_zha only: a file builds on ZHA's class only where it takes ZHA's quirk over)."""
        if not isinstance(entry, dict) or (fingerprints := _pairs(entry.get("fingerprints"))) is None:
            return None
        if (claimed := _pairs(entry.get("claimed_elsewhere", []))) is None:
            return None
        endpoints = None
        if entry.get("endpoints") is not None and (endpoints := endpoint_map(entry["endpoints"], str)) is None:
            return None
        over_zha = entry.get("over_zha")
        if over_zha is not None and not (
            isinstance(over_zha, dict) and set(over_zha) == {"adds", "zha_only"}
            and all(isinstance(names, list) and all(isinstance(name, str) for name in names) for names in over_zha.values())
        ):
            return None
        groups = []
        if entry.get("aliases") is not None:
            if not isinstance(entry["aliases"], list):
                return None
            for group in entry["aliases"]:
                if not isinstance(group, dict) or (targets := _pairs(group.get("targets"))) is None or (members := _pairs(group.get("fingerprints"))) is None:
                    return None
                groups.append((targets, members))
        elif entry.get("target") is not None:
            if (target := _pair(entry["target"])) is None:
                return None
            groups.append((frozenset({target}), fingerprints))
        if (names := cls.names_of(entry, fingerprints)) is None:
            return None
        inherits = frozenset()
        if entry.get("inherits") is not None and ((inherits := inherit_pairs(entry["inherits"])) is None or (inherits and over_zha is None)):
            return None
        return cls(fingerprints, tuple(groups), claimed, over_zha is not None, endpoints, names, inherits)


class Guard:
    """The guard as a release ships it: its identity and its fixed code."""

    def __init__(self, guard_source: bytes, fixed_source: bytes) -> None:
        self.id = identity(guard_source, fixed_source)
        self.fixed = Fixed(json.loads(fixed_source))

    @classmethod
    def load(cls, folder: pathlib.Path = HERE) -> Guard:
        """This file and fixed_code.json next to it (blocking I/O)."""
        return cls((folder / pathlib.Path(__file__).name).read_bytes(), (folder / FIXED_CODE).read_bytes())

    def problems(self, content: bytes, targets: Targets | None = None) -> tuple[str, ...]:
        """Why the guard refuses a quirk file offered with these targets ("line N: reason", at most
        MAX_PROBLEMS); empty when it passes. Without targets it is refused."""
        return tuple(f"line {line}: {reason}" for line, reason in check_quirk(content, self.fixed, targets)[:MAX_PROBLEMS])


def docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


def literal(node: ast.expr) -> bool:
    """A literal: constants and the containers of constants that ast.literal_eval reads."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return isinstance(node.operand, ast.Constant) and type(node.operand.value) in (int, float)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(literal(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and literal(k) for k in node.keys) and all(literal(v) for v in node.values)
    return False


def number(node: ast.expr) -> bool:
    """A number literal: 10, 0.5, -3."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        node = node.operand
    return isinstance(node, ast.Constant) and type(node.value) in (int, float)


def factor(node: ast.expr) -> bool:
    """A number literal a converter may multiply x by: any float (a string times a float raises) or
    an integer of at most MAX_FACTOR."""
    if not number(node):
        return False
    value = (node.operand if isinstance(node, ast.UnaryOp) else node).value
    return type(value) is float or abs(value) <= MAX_FACTOR


def literal_assignment(stmt: ast.stmt) -> bool:
    """NAME = literal, or NAME, NAME = literal."""
    if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
        return False
    target = stmt.targets[0]
    names = target.elts if isinstance(target, ast.Tuple) else [target]
    return all(isinstance(n, ast.Name) and not n.id.startswith("__") for n in names) and literal(stmt.value)


def dotted(node: ast.expr) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return None


def _short(node: ast.AST) -> str:
    text = ast.unparse(node)
    return text if len(text) <= 80 else text[:77] + "..."


def kind_of(module: str, name: str) -> str:
    """What a quirk may do with `from module import name`; "" for a name it may not import."""
    found = IMPORTS.get(module, {}).get(name)
    if found:
        return found
    return CLUSTER if module in CLUSTER_MODULES and CLASS_NAME.fullmatch(name) else ""


class QuirkFile:
    """The allowlist applied to one parsed quirk file."""

    def __init__(self, tree: ast.Module, fixed: Fixed, inherits: frozenset[tuple[str, str]] = frozenset()) -> None:
        self.tree = tree
        self.fixed = fixed
        # The (module, class) pairs of ZHA's classes the entry lets the file build on (Targets.inherits); per imported
        # one the class of the file on it, and the nodes where its name stands as that class allows (inheriting_class).
        self.inherits = inherits
        self.inheritors: dict[str, str] = {}
        self.inherited_uses: set[int] = set()
        # What the file targets, as read: (node, fingerprint) of builders, applies_to, FINGERPRINTS
        # and ALIASES; (node, targets, fingerprints) of each ALIASES group.
        self.aims: list[tuple[ast.AST, tuple[str | None, str]]] = []
        self.alias_groups: list[tuple[ast.AST, frozenset, frozenset]] = []
        # CLAIMED_ELSEWHERE as read (empty without one): the names the file leaves to other devices.
        self.claimed: frozenset = frozenset()
        # ENDPOINTS as read (None without one): what the runtime check needs on the device.
        self.endpoints: dict[int, tuple[int, ...]] | None = None
        # A builder chain with the filter: the file must bind FILTER_NEEDS before its first chain.
        self.filtered = False
        self.first_chain: int | None = None  # the index of the first chain statement
        # The filter variant that keeps the file for its exact names over ZHA's own quirk.
        self.takes_over = False
        # (node, (manufacturer, model)) of each friendly_name: names its entry must give the device.
        self.friendly_names: list[tuple[ast.AST, tuple[str, str]]] = []
        # The chain being checked: its builder and its method calls (what a ReportingConfig's entity needs of it).
        self.builder = ""
        self.calls: list[ast.Call] = []
        # What the integration looks up inside Home Assistant: the attribute types the guard cannot see (entity_value).
        self.needs: list[AttributeNeed] = []
        self.imported: dict[str, str] = {}  # from-imported name -> its kind ("" when only the fixed code uses it)
        self.import_modules: dict[str, str] = {}  # from-imported name -> its module
        self.modules: dict[str, str] = {}  # name of a plain import -> module
        self.classes: dict[str, ast.ClassDef] = {}
        # The file's enum classes (bases of ENUM_BASES) -> their members.
        self.enums: dict[str, set[str]] = {}
        self.functions: set[str] = set()
        self.values: set[str] = set()
        # Each name is bound once: a class must not stand in for an imported QuirkBuilder.
        self.twice: list[tuple[ast.stmt, str]] = []
        # Name -> the index of the statement that binds it.
        self.where: dict[str, int] = {}
        for index, stmt in enumerate(tree.body):
            names = bound_names(stmt)
            if isinstance(stmt, ast.ImportFrom):
                self.imported.update({a.asname or a.name: "" if a.asname else self.kind(stmt.module or "", a.name) for a in stmt.names})
                self.import_modules.update({a.asname or a.name: stmt.module or "" for a in stmt.names})
            elif isinstance(stmt, ast.Import):
                self.modules.update({a.asname or a.name: a.name for a in stmt.names})
            elif isinstance(stmt, ast.ClassDef):
                self.classes[stmt.name] = stmt
            elif isinstance(stmt, ast.FunctionDef):
                self.functions.add(stmt.name)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                self.values.update(names)
            self.twice += [(stmt, name) for name in names if name in self.where]
            for name in names:
                self.where.setdefault(name, index)
        self.bound = frozenset(self.where)
        for name, node in self.classes.items():
            if node.bases and all(self.zigpy_type(base, ENUM_BASES) for base in node.bases):
                self.enums[name] = {s.targets[0].id for s in node.body if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name)}

    def kind(self, module: str, name: str) -> str:
        """kind_of, INHERITED for a pair of the entry's inherits."""
        return INHERITED if (module, name) in self.inherits else kind_of(module, name)

    def problems(self, targets: Targets | None) -> list[tuple[ast.AST, str]]:
        out = [(stmt, f"name {name} bound twice") for stmt, name in self.twice]
        # Every text literal, the docstrings among them, is UTF-8: a lone surrogate is not (orjson refuses it).
        out += [(node, "a text literal that is no UTF-8 (a lone surrogate)") for node in ast.walk(self.tree)
                if isinstance(node, ast.Constant) and type(node.value) is str and not _utf8(node.value)]
        for index, stmt in enumerate(self.tree.body):
            try:
                self.statement(stmt, index)
            except Refused as refused:
                out.append((refused.node, refused.reason))
        if self.filtered:
            if missing := FILTER_NEEDS - self.bound:
                out.append((self.tree, f"the filter needs {', '.join(sorted(missing))}, which the file does not define"))
            # add_to_registry() ran once the first chain did: a statement after it that fails at import
            # would leave a name bound below it unbound when ZHA calls the filter.
            if late := sorted(name for name in FILTER_NEEDS & self.bound if self.where[name] > self.first_chain):
                out.append((self.tree, f"the filter needs {', '.join(late)} bound before the first builder chain"))
        return out + self.inheritance() + self.targeting(targets)

    def targeting(self, targets: Targets | None) -> list[tuple[ast.AST, str]]:
        """What the file targets outside the manifest entry it is offered for."""
        if targets is None:
            return [(self.tree, "no manifest entry to compare its targets with")]
        out = []
        for node, pair in self.aims:
            if pair not in targets.fingerprints:
                wildcard = " (a manufacturer wildcard)" if pair[0] is None else ""
                out.append((node, f"targets {_quoted(pair)}{wildcard}, which its manifest entry does not list"))
        for node, (manufacturer, model) in self.friendly_names:
            # ZHA matches its per-model rules on these names: only one its entry gives the device.
            if (manufacturer.casefold(), model.casefold()) not in targets.names:
                out.append((node, f"friendly_name {_quoted((manufacturer, model))}, which is no name its manifest entry gives the device"))
            # A name ZHA acts on for other devices too: only where every device the file targets has it already.
            elif manufacturer.casefold() in GROUP_NAMES and not all(pair[0] is not None and pair[0].casefold() == manufacturer.casefold() for _node, pair in self.aims):
                out.append((node, f"friendly_name with the manufacturer {manufacturer!r}, which ZHA applies to every light group of the device, "
                                  "for a device the file targets under another manufacturer"))
        for node, group_targets, members in self.alias_groups:
            if not any(group_targets <= allowed and members <= names for allowed, names in targets.groups):
                out.append((node, "an alias group outside the targets and fingerprints of its manifest entry"))
        if self.claimed != targets.claimed:
            # The integration reads what an installed file claims from the entry it was written for.
            out.append((self.tree, "CLAIMED_ELSEWHERE other than its manifest entry's claimed_elsewhere"))
        if self.endpoints != targets.endpoints:
            # The integration offers a file only to devices that fit its entry's endpoints.
            out.append((self.tree, "ENDPOINTS other than its manifest entry's endpoints"))
        if self.takes_over != targets.takes_over:
            # The integration offers a file over ZHA's own quirk only where its entry says so.
            out.append((self.tree, "takes devices over from ZHA's own quirk, which its manifest entry does not say" if self.takes_over
                        else "its manifest entry says it takes devices over from ZHA's own quirk, its filter does not"))
        return out

    def aim(self, node: ast.AST, values: list[ast.expr]) -> None:
        """A fingerprint written as two arguments: literal (manufacturer or None, model)."""
        if len(values) != 2 or not all(isinstance(v, ast.Constant) for v in values) or (pair := _pair([v.value for v in values])) is None:
            raise Refused(node, "a target that is no literal (manufacturer, model)")
        self.aims.append((node, pair))

    def literal_targets(self, stmt: ast.Assign) -> None:
        """FINGERPRINTS, CLAIMED_ELSEWHERE, ENDPOINTS and ALIASES as the templates write them."""
        try:
            name, value = stmt.targets[0].id, ast.literal_eval(stmt.value)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as error:
            raise Refused(stmt, f"a literal that does not evaluate ({type(error).__name__})") from None
        if name == "FINGERPRINTS":
            if not isinstance(value, list) or not all(isinstance(item, tuple) and _pair(item) for item in value):
                raise Refused(stmt, "FINGERPRINTS other than a list of (manufacturer, model)")
            self.aims += [(stmt, _pair(item)) for item in value]
        elif name == "CLAIMED_ELSEWHERE":
            if not isinstance(value, (set, list)) or not all(isinstance(item, tuple) and _pair(item) and item[0] is not None for item in value):
                raise Refused(stmt, "CLAIMED_ELSEWHERE other than a set of (manufacturer, model)")
            self.claimed = frozenset(_pair(item) for item in value)
        elif name == "ENDPOINTS":
            if (endpoints := endpoint_map(value, int)) is None:
                raise Refused(stmt, "ENDPOINTS other than ascending endpoint ids with sorted tuples of cluster ids")
            self.endpoints = endpoints
        elif name == "ALIASES":
            if not isinstance(value, list) or not all(isinstance(group, dict) and set(group) == {"targets", "fingerprints"} for group in value):
                raise Refused(stmt, "ALIASES other than a list of {targets, fingerprints}")
            for group in value:
                if not all(isinstance(items, list) and all(isinstance(item, tuple) for item in items) for items in group.values()):
                    raise Refused(stmt, "an ALIASES group other than lists of (manufacturer, model)")
                group_targets, members = _pairs(group["targets"]), _pairs(group["fingerprints"])
                if group_targets is None or members is None:
                    raise Refused(stmt, "an ALIASES group other than lists of (manufacturer, model)")
                self.alias_groups.append((stmt, group_targets, members))
                self.aims += [(stmt, pair) for pair in members]

    def zigpy_type(self, node: ast.expr, names) -> bool:
        """t.X with X in names, t being zigpy.types."""
        parts = (dotted(node) or "").split(".")
        return len(parts) == 2 and parts[0] == "t" and self.modules.get("t") == "zigpy.types" and parts[1] in names

    def statement(self, stmt: ast.stmt, index: int) -> None:
        if index == 0 and docstring(stmt):
            return
        # add_to_registry() ran once the first chain did: a statement after it that fails at import
        # (an import of a name the module lacks) would leave what it binds unbound when ZHA calls the
        # quirk (the filter, a fixed method, a converter's helper). Chains come last.
        if self.first_chain is not None and not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
            raise Refused(stmt, f"{type(stmt).__name__} statement after the first builder chain")
        if literal_assignment(stmt) and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id in LITERALS:
            # Data, also where it equals a template statement: the filter reads these values.
            return self.literal_targets(stmt)
        if (dumped := ast.dump(stmt)) in self.fixed.module:
            self.takes_over |= dumped in self.fixed.take_over
            return  # the fixed code
        if isinstance(stmt, ast.Import):
            raise Refused(stmt, "import " + ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in stmt.names))
        if isinstance(stmt, ast.ImportFrom):
            if stmt.level or not stmt.module:
                raise Refused(stmt, f"import from {'.' * stmt.level}{stmt.module or ''}")
            for alias in stmt.names:
                if alias.asname or not self.kind(stmt.module, alias.name):
                    raise Refused(stmt, f"import of {alias.name}{' as ' + alias.asname if alias.asname else ''} from {stmt.module}")
                if alias.name in self.reserved:
                    raise Refused(stmt, f"import of {alias.name}, a name the fixed code binds or a builtin")
        elif isinstance(stmt, ast.ClassDef):
            self.class_def(stmt)
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            if self.first_chain is None:
                self.first_chain = index
            self.chain(stmt.value)
        else:
            raise Refused(stmt, f"{type(stmt).__name__} statement")

    @property
    def reserved(self) -> frozenset[str]:
        """The names only the fixed code and the literals checked as data bind, and the builtins the fixed code reads."""
        return self.fixed.names | FILTER_NEEDS | LITERALS | BUILTIN_NAMES

    def class_def(self, node: ast.ClassDef, nested: bool = False) -> None:
        if not nested and any(isinstance(base, ast.Name) and self.imported.get(base.id) == INHERITED for base in node.bases):
            return self.inheriting_class(node)
        if node.decorator_list or node.keywords or getattr(node, "type_params", None):
            raise Refused(node, f"class {node.name} with decorators, keywords or type parameters")
        if not CLASS_NAME.fullmatch(node.name):
            raise Refused(node, f"class name {node.name}")
        if not nested and node.name in self.reserved:
            raise Refused(node, f"class {node.name}, a name the fixed code binds or a builtin")
        if nested and node.name not in NESTED:
            raise Refused(node, f"nested class {node.name}")
        if not node.bases:
            raise Refused(node, f"class {node.name} without a base")
        for base in node.bases:
            if not (self.nested_base(base, node.name) if nested else self.base(base)):
                raise Refused(base, f"base class {_short(base)}")
        # zigpy registers a cluster class for its cluster id unless a base sets _skip_registry (SKIP_REGISTRY): the
        # class would be the cluster of that id of every device, whatever the file targets or its chain uses.
        if not nested and node.name not in self.enums and not any(self.skips_registry(base) for base in node.bases):
            raise Refused(node, f"class {node.name} without a zhaquirks cluster base: zigpy would build every device's cluster of its id from it")
        values: dict[str, ast.Assign] = {}
        for index, stmt in enumerate(node.body):
            if index == 0 and docstring(stmt):
                continue
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) and not stmt.targets[0].id.startswith("__"):
                if nested:
                    # A member of AttributeDefs or ServerCommandDefs: a definition.
                    if not isinstance(stmt.value, ast.Call):
                        raise Refused(stmt, f"{stmt.targets[0].id} in {node.name} other than a definition")
                    self.expr(stmt.value, CLASS)
                elif node.name in self.enums:
                    self.enum_member(stmt, node)
                else:
                    self.class_value(stmt, node)
                    values[stmt.targets[0].id] = stmt
            elif isinstance(stmt, ast.ClassDef) and not nested:
                self.class_def(stmt, nested=True)
            elif isinstance(stmt, ast.FunctionDef) and not nested and (dumped := ast.dump(stmt)) in self.fixed.methods:
                # The Aqara helpers: what one reads from the module (ElectricalMeasurement), the file imports.
                if missing := sorted(name for name in self.fixed.reads.get(dumped, ()) if not self.imported.get(name)):
                    raise Refused(stmt, f"method {stmt.name} reads {', '.join(missing)}, which the file does not import")
            else:
                raise Refused(stmt, f"{type(stmt).__name__} statement in class {node.name}")
        if set(VOLTS) & set(values):
            low, high = (values.get(name) for name in VOLTS)
            if low is None or high is None or not low.value.value < high.value.value:
                raise Refused(node, f"class {node.name}: {VOLTS[0]} below {VOLTS[1]}, both or neither")

    def enum_member(self, stmt: ast.Assign, node: ast.ClassDef) -> None:
        """An enum member: a name ENUM_MEMBER allows that stands for nothing of the enum type
        (ENUM_RESERVED: _missing_ or serialize would replace what zigpy calls), an integer the enum
        holds (t.enum8: 0 to 255, t.enum16: 0 to 65535, t.enum32: 0 to 4294967295)."""
        name = stmt.targets[0].id
        if not ENUM_MEMBER.fullmatch(name) or name in ENUM_RESERVED:
            raise Refused(stmt, f"enum member name {name}")
        bits = next(ENUM_BITS[base] for base in ENUM_BASES if any(self.zigpy_type(b, (base,)) for b in node.bases))
        if not _int_in(stmt.value, range(1 << bits)):
            raise Refused(stmt, f"enum member {name} other than an integer from 0 to {(1 << bits) - 1}")

    def class_value(self, stmt: ast.Assign, node: ast.ClassDef) -> None:
        """A value of a cluster class: data in its allowed shape, else a fixed class
        statement (STRUCT_TARGETS, the outage cluster's ep_attribute and _VALID_ATTRIBUTES)."""
        name, value = stmt.targets[0].id, stmt.value
        if name == "cluster_id":
            if not _int_in(value, range(0x10000)):
                raise Refused(stmt, "cluster_id other than an integer from 0 to 0xFFFF")
        elif name == "STRUCT_TAGS":
            if not (isinstance(value, ast.Dict) and value.keys and all(_struct_tag(k, v) for k, v in zip(value.keys, value.values, strict=True))):
                raise Refused(stmt, "STRUCT_TAGS other than Aqara struct tags with their key and a float factor")
        elif name in VOLTS:
            if not _int_in(value, VOLTS_RANGE):
                raise Refused(stmt, f"{name} other than an integer from {VOLTS_RANGE.start} to {VOLTS_RANGE.stop - 1}")
        elif name == SPREAD:
            self.constant_attributes(stmt)
        elif ast.dump(stmt) not in self.fixed.methods:
            raise Refused(stmt, f"{name} in class {node.name}")

    def constant_attributes(self, stmt: ast.Assign) -> None:
        """{**Cluster._CONSTANT_ATTRIBUTES, Cluster.AttributeDefs.name.id: 1000}: integer values."""
        if not isinstance(stmt.value, ast.Dict):
            raise Refused(stmt, f"{SPREAD} other than a dict")
        for key, item in zip(stmt.value.keys, stmt.value.values, strict=True):
            if key is None:
                if not isinstance(item, ast.Attribute):
                    raise Refused(item, f"** other than a cluster's {SPREAD}")
                self.attribute(item, spread=True)
                continue
            if not _int_in(key, range(0x10000)):
                if not (isinstance(key, ast.Attribute) and key.attr == "id"):
                    raise Refused(key, f"{SPREAD} key other than an attribute id")
                self.attribute(key)
            if not _int_in(item, CONSTANT_RANGE):
                raise Refused(item, f"{SPREAD} value other than an integer from {CONSTANT_RANGE.start} to {CONSTANT_RANGE.stop - 1}")

    def base(self, node: ast.expr) -> bool:
        """The base of a class: an imported cluster class or t.enum8, t.enum16, t.enum32."""
        if isinstance(node, ast.Name):
            return self.imported.get(node.id) == CLUSTER
        return self.zigpy_type(node, ENUM_BASES)

    def skips_registry(self, node: ast.expr) -> bool:
        """A base that keeps a class out of zigpy's registry: an imported zhaquirks cluster class (SKIP_REGISTRY)."""
        return isinstance(node, ast.Name) and self.imported.get(node.id) == CLUSTER and (self.import_modules.get(node.id), node.id) in SKIP_REGISTRY

    def nested_base(self, node: ast.expr, nested: str) -> bool:
        """The base of a nested AttributeDefs or ServerCommandDefs: zigpy's base or that of an imported cluster class."""
        if isinstance(node, ast.Name):
            return self.imported.get(node.id) == NESTED[nested]
        parts = (dotted(node) or "").split(".")
        return len(parts) == 2 and parts[1] == nested and self.imported.get(parts[0]) == CLUSTER

    # Classes on one of ZHA's classes (Targets.inherits, INHERITED).

    def inheriting_class(self, node: ast.ClassDef) -> None:
        """class Own(C): C a pair of the entry's inherits and its only base, a docstring, then at most one nested
        AttributeDefs, ServerCommandDefs and ClientCommandDefs each (inherited_nested). Nothing else: no cluster_id (it
        would move ZHA's code onto another cluster), no other data, no method. The nodes where C stands so are kept
        (inherited_uses): C stands nowhere else (inheritance)."""
        if node.decorator_list or node.keywords or getattr(node, "type_params", None):
            raise Refused(node, f"class {node.name} with decorators, keywords or type parameters")
        if not CLASS_NAME.fullmatch(node.name) or node.name in self.reserved:
            raise Refused(node, f"class name {node.name}")
        base = node.bases[0] if len(node.bases) == 1 else None
        if not (isinstance(base, ast.Name) and self.imported.get(base.id) == INHERITED):
            raise Refused(node, f"class {node.name} on one of ZHA's classes beside another base (ZHA's class is its only base)")
        if (other := self.inheritors.get(base.id)) is not None:
            raise Refused(node, f"class {node.name} on ZHA's class {base.id}, which class {other} builds on already")
        self.inheritors[base.id] = node.name
        self.inherited_uses.add(id(base))
        if not (node.body and docstring(node.body[0])):
            raise Refused(node, f"class {node.name} on ZHA's class {base.id} without a docstring")
        seen: set[str] = set()
        for stmt in node.body[1:]:
            if not (isinstance(stmt, ast.ClassDef) and stmt.name in INHERIT_NESTED):
                raise Refused(stmt, f"{type(stmt).__name__} statement in class {node.name} on ZHA's class {base.id} (only nested {', '.join(INHERIT_NESTED)})")
            if stmt.name in seen:
                raise Refused(stmt, f"nested class {stmt.name} twice in class {node.name}")
            seen.add(stmt.name)
            self.inherited_nested(stmt, base.id)

    def inherited_nested(self, node: ast.ClassDef, inherited: str) -> None:
        """A nested class of a class on ZHA's class C: on C's own class of that name (C.AttributeDefs: on another base
        ZHA's definitions, which ZHA's code reads, would be lost), a docstring, members `name = ZCLAttributeDef(id=0x10, ...)`
        (ZCLCommandDef in the command classes) with an integer literal id in its range (INHERIT_NESTED), so the
        integration can compare names and ids with C's. The rest of a definition is checked as in any class (expr)."""
        if node.decorator_list or node.keywords or getattr(node, "type_params", None):
            raise Refused(node, f"nested class {node.name} with decorators, keywords or type parameters")
        if len(node.bases) != 1 or dotted(node.bases[0]) != f"{inherited}.{node.name}":
            raise Refused(node, f"nested class {node.name} on other than {inherited}.{node.name} (ZHA's definitions would be lost)")
        self.inherited_uses.add(id(node.bases[0].value))
        definition, ids = INHERIT_NESTED[node.name]
        for index, stmt in enumerate(node.body):
            if index == 0 and docstring(stmt):
                continue
            if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) and NAME.fullmatch(stmt.targets[0].id)
                    and isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Name) and stmt.value.func.id == definition
                    and self.imported.get(definition) == DEFINITION and not stmt.value.args):
                raise Refused(stmt, f"{type(stmt).__name__} statement in {node.name} of a class on ZHA's class {inherited} (only name = {definition}(id=..., ...))")
            identifier = [keyword.value for keyword in stmt.value.keywords if keyword.arg == "id"]
            if len(identifier) != 1 or not (isinstance(identifier[0], ast.Constant) and _int_in(identifier[0], ids)):
                raise Refused(stmt, f"{stmt.targets[0].id} in {node.name} of a class on ZHA's class {inherited} with an id other than an integer literal from 0 to {ids.stop - 1}")
            self.expr(stmt.value, CLASS)

    def inheritance(self) -> list[tuple[ast.AST, str]]:
        """ZHA's classes the file builds on against the entry's inherits: it imports every pair, each one the base of a
        class of the file (inheriting_class). Its name stands nowhere but there (inherited_uses): not as a value, in a
        chain, a converter or a fixed helper or method, not beside another base, not under a second class."""
        imported = {name: (self.import_modules[name], name) for name, kind in self.imported.items() if kind == INHERITED}
        out = [(self.tree, f"its manifest entry builds on ZHA's class {name} ({module}), which the file does not import")
               for module, name in sorted(self.inherits - set(imported.values()))]
        out += [(self.tree, f"ZHA's class {name} imported, no class of the file builds on it") for name in sorted(imported) if name not in self.inheritors]
        out += [(node, f"ZHA's class {node.id} other than as the one base of a class of the file") for node in ast.walk(self.tree)
                if isinstance(node, ast.Name) and node.id in imported and id(node) not in self.inherited_uses]
        return out

    def chain(self, node: ast.Call) -> None:
        """QuirkBuilder(...).method(...)....add_to_registry()."""
        calls = []
        while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            calls.append(node)
            node = node.func.value
        root = node
        if not (isinstance(root, ast.Call) and isinstance(root.func, ast.Name) and self.imported.get(root.func.id) == BUILDER):
            raise Refused(root, "a module-level call that is no builder chain")
        tuya = root.func.id == "TuyaQuirkBuilder"
        if not calls or calls[0].func.attr != "add_to_registry" or calls[0].args or (calls[0].keywords and not tuya):
            raise Refused(root, "a builder chain that does not end in add_to_registry()")
        if tuya:
            self.typed_tuya(calls[0])
        # What an entity with a ReportingConfig needs of its chain (reported_attribute) is read from these.
        self.builder, self.calls = root.func.id, calls
        for call in calls:
            if call.func.attr not in METHODS[root.func.id]:
                raise Refused(call, f"builder method {call.func.attr}")
            if call.func.attr == "filter" and not (
                len(call.args) == 1 and not call.keywords and isinstance(call.args[0], ast.Name) and call.args[0].id == FILTER and FILTER in self.functions
            ):
                raise Refused(call, f"filter other than {FILTER}")
            if check := self.method_checks.get(call.func.attr):
                check(call)
            elif call.args and call.func.attr not in POSITIONAL:
                # A positional argument skips the check of its keyword: tuya_dp_multi's third one is dp_handler.
                raise Refused(call, f"builder method {call.func.attr} with positional arguments (keywords only)")
        # zhaquirks keeps the last firmware range of a chain only: one call, so the change list shows the range there is.
        if sum(call.func.attr == "firmware_version_filter" for call in calls) > 1:
            raise Refused(root, "a builder chain with more than one firmware_version_filter (zhaquirks keeps only the last one)")
        # Upstream first: every chain has the filter that steps back for a shipped quirk.
        if sum(call.func.attr == "filter" for call in calls) != 1:
            raise Refused(root, f"a builder chain without exactly one .filter({FILTER})")
        self.filtered = True
        # Its fingerprints: the builder's own (QuirkBuilder only) and each applies_to.
        if root.keywords or len(root.args) != ROOT_ARGS[root.func.id]:
            raise Refused(root, f"{root.func.id} with other arguments than allowed")
        if root.args:
            self.aim(root, root.args)
        for call in calls:
            if call.func.attr == "applies_to":
                if call.keywords:
                    raise Refused(call, "a target that is no literal (manufacturer, model)")
                self.aim(call, call.args)
        for call in [root, *calls]:
            # What a method with a checker of its own takes is checked there, nothing of it reaches expr.
            if call is root or call.func.attr not in self.method_checks:
                self.arguments(call, CHAIN)
            if call is not root:
                self.entity_value(call)

    def entity_value(self, call: ast.Call) -> None:
        """An entity that computes with its attribute's value while ZHA adds it (SCALED with a scaling keyword, ENUM_ENTITIES)
        reads an attribute whose type fits: a number type, or for an enum entity an enum type of the file at most as wide
        or an unsigned integer type that fits it (fits). See the docstring for where the attribute may be."""
        method = call.func.attr
        values = {keyword.arg: keyword.value for keyword in call.keywords}
        if method in ENUM_ENTITIES:
            enum = values.get("enum_class")
            if not (isinstance(enum, ast.Name) and enum.id in self.enums):
                raise Refused(call, f"{method} with an enum_class other than an enum class of the file")
            need: int | None = self.enum_bits(enum.id)
        elif any(name in values for name in SCALED.get(method, ())):
            need = None
        else:
            return
        what = ("a number type" if need is None else f"an enum class of the file, an enum, a bitmap or t.Bool of at most {need} bits" if self.builder == "TuyaQuirkBuilder"
                else f"an unsigned integer type of at most {need} bits")
        if self.builder == "TuyaQuirkBuilder":
            return self.tuya_value(call, values, need, what)
        return self.zcl_value(call, values, need, what)

    def enum_bits(self, name: str) -> int:
        """The bits of an enum class of the file (its t.enum8, t.enum16 or t.enum32 base)."""
        return next(ENUM_BITS[base] for base in ENUM_BASES if any(self.zigpy_type(b, (base,)) for b in self.classes[name].bases))

    def type_token(self, node: ast.expr | None) -> str | None:
        """A type as a definition names it: "t.uint16_t" for a zigpy type, the name of an enum class of the file; None else."""
        if node is not None and self.zigpy_type(node, TYPE_NAMES):
            return dotted(node)
        return node.id if isinstance(node, ast.Name) and node.id in self.enums else None

    def fits(self, token: str | None, need: int | None, tuya: bool) -> bool:
        """Whether every value of the type fits: a number for a divisor or a multiplier (need None), an enum class of need
        bits for an enum entity (an enum of the file at most as wide, an unsigned integer type that wide; on the Tuya
        builder only those the typed Tuya cluster keeps integers, INTEGER_ONLY)."""
        if token is None:
            return False
        if token in self.enums:
            return need is None or self.enum_bits(token) <= need
        name = token.removeprefix("t.")
        if need is None:
            return name in NUMBER_TYPES
        return name in UNSIGNED_BITS and UNSIGNED_BITS[name] <= need and (not tuya or name in INTEGER_ONLY)

    def tuya_value(self, call: ast.Call, values: dict[str, ast.expr], need: int | None, what: str) -> None:
        """On the Tuya builder: an attribute the chain declares on its Tuya cluster (TUYA_DECLARES), every declaration of the
        name with the same type, read on endpoint 1 by the tuya_* method itself or with cluster_id=TUYA_CLUSTER_ID."""
        method = call.func.attr
        name = values.get("attribute_name", ast.Constant(TUYA_DECLARES.get(method, (None, None))[1]))
        cluster = values.get("cluster_id")
        tuya_cluster = method.startswith("tuya_") or (isinstance(cluster, ast.Name) and cluster.id == "TUYA_CLUSTER_ID" and self.imported.get(cluster.id) == VALUE)
        if not (tuya_cluster and isinstance(name, ast.Constant) and type(name.value) is str and _int_in(values.get("endpoint_id", ast.Constant(1)), range(1, 2))):
            raise Refused(call, f"{method} that computes with its value on the Tuya builder other than on an attribute the chain declares on its Tuya "
                                "cluster (cluster_id=TUYA_CLUSTER_ID, attribute_name a text, endpoint 1)")
        types = set()
        for other in self.calls:
            declares = TUYA_DECLARES.get(other.func.attr)
            if declares is None:
                continue
            if other.args:
                raise Refused(call, f"{method} on the Tuya builder in a chain whose {other.func.attr} takes positional arguments (what it declares unread)")
            given = {keyword.arg: keyword.value for keyword in other.keywords}
            declared = given.get(declares[0], ast.Constant(declares[1]))
            if isinstance(declared, ast.Constant) and declared.value == name.value:
                types.add(self.type_token(given[declares[2]]) if declares[2] in given else declares[3])
        if len(types) != 1:
            raise Refused(call, f"{method} on {name.value!r}, which the chain declares on its Tuya cluster {'with different types' if types else 'nowhere'}")
        if not self.fits(next(iter(types)), need, tuya=True):
            raise Refused(call, f"{method} on {name.value!r} of the type {next(iter(types))}, while ZHA computes with it: the type must be {what}")

    def zcl_value(self, call: ast.Call, values: dict[str, ast.expr], need: int | None, what: str) -> None:
        """On QuirkBuilder: attribute_name=Cls.AttributeDefs.<name>.name, cluster_id=Cls.cluster_id on the server cluster of an
        endpoint where Cls certainly sits (the chain puts it there, or it is zigpy's own class of that cluster), no rival.
        The type from Cls's own definition, else an AttributeNeed for the check inside Home Assistant."""
        method = call.func.attr
        attribute, cluster = values.get("attribute_name"), values.get("cluster_id")
        path = (dotted(attribute) or "").split(".")
        owner = path[0] if len(path) == 4 and path[1] == "AttributeDefs" and path[3] == "name" else None
        endpoint = values.get("endpoint_id", ast.Constant(1))
        if call.args or owner is None or dotted(cluster) != f"{owner}.cluster_id" or "cluster_type" in values or not _int_in(endpoint, range(1, 255)):
            raise Refused(call, f"{method} that computes with its value other than of the keywords attribute_name=Cls.AttributeDefs.<name>.name, "
                                "cluster_id=Cls.cluster_id of a server cluster on an endpoint from 1 to 254")
        name = path[2]
        ours = owner in self.classes and owner not in self.enums
        zigpys = self.imported.get(owner) == CLUSTER and self.import_modules.get(owner) in CLUSTER_MODULES
        if not (ours or self.imported.get(owner) == CLUSTER):
            raise Refused(call, f"{method} on {owner}, which is no cluster class the file defines or imports")
        if not (self.placed(owner, endpoint.value) or (zigpys and not ours)):
            raise Refused(call, f"{method} on {owner}, which the chain does not put on endpoint {endpoint.value} with replaces, adds or replace_cluster_occurrences")
        # A class the chain may put in Cls's place is what the entity reads there: it lacks the name (ZHA then shows no
        # value) or holds it with a type that fits. Only a class of the file or an imported cluster class can be looked at.
        for rival in self.rivals(owner, endpoint.value):
            if not (isinstance(rival, ast.Name) and ((rival.id in self.classes and rival.id not in self.enums) or self.imported.get(rival.id) in (CLUSTER, INHERITED))):
                raise Refused(call, f"{method} on {owner}, while the chain may put {_short(rival)} in its place on endpoint {endpoint.value}")
            self.attribute_fits(call, rival.id, name, need, what, optional=True)
        self.attribute_fits(call, owner, name, need, what, optional=False)

    def attribute_fits(self, call: ast.Call, cls: str, name: str, need: int | None, what: str, optional: bool) -> None:
        """The type of name in the class an entity may read (a class of the file or an imported cluster class) fits need:
        checked here where the file's class defines it, else an AttributeNeed for the check inside Home Assistant. A class
        of the file without the name or a base it may have it from: refused, unless optional (a class that may stand in
        the place of the one the entity names: it lacks the name)."""
        method = call.func.attr
        ours = cls in self.classes
        if ours and (definition := self.own_definition(cls, name)) is not None:
            token = self.type_token(next((keyword.value for keyword in definition.keywords if keyword.arg == "type"), None)) if not definition.args else None
            if not self.fits(token, need, tuya=False):
                raise Refused(call, f"{method} on {cls}.AttributeDefs.{name} of the type {token or 'the guard cannot read'}, while ZHA computes with it: the type must be {what}")
            return
        candidates = self.attribute_bases(cls) if ours else ((self.import_modules[cls], cls),)
        if not candidates:
            if optional:
                return
            raise Refused(call, f"{method} on {cls}.AttributeDefs.{name}, which the class neither defines nor has from a base")
        self.needs.append(AttributeNeed(call.lineno, method, cls, name, candidates, need, optional))

    def own_definition(self, cls: str, name: str) -> ast.Call | None:
        """The definition of name in the nested AttributeDefs of a class of the file, None where it defines none."""
        for nested in self.classes[cls].body:
            if isinstance(nested, ast.ClassDef) and nested.name == "AttributeDefs":
                for stmt in nested.body:
                    if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == name and isinstance(stmt.value, ast.Call):
                        return stmt.value
        return None

    def attribute_bases(self, cls: str) -> tuple[tuple[str, str], ...]:
        """The imported classes (module, name) a definition the class of the file does not hold may come from: the bases
        of its nested AttributeDefs (Base.AttributeDefs), or without one the bases of the class."""
        node = self.classes[cls]
        nested = next((stmt for stmt in node.body if isinstance(stmt, ast.ClassDef) and stmt.name == "AttributeDefs"), None)
        names = [(dotted(base) or "").removesuffix(".AttributeDefs") for base in nested.bases] if nested is not None else [dotted(base) or "" for base in node.bases]
        return tuple((self.import_modules[name], name) for name in names if self.imported.get(name) in (CLUSTER, INHERITED))

    def typed_tuya(self, call: ast.Call) -> None:
        """add_to_registry(replacement_cluster=Cls) of a Tuya chain: Cls a class of the file on TuyaMCUCluster alone that
        holds a docstring and the fixed method TYPED_TUYA, nothing else. zhaquirks builds the device's Tuya cluster on
        Cls, so every datapoint value is checked against its attribute's type before zhaquirks stores it."""
        keyword = call.keywords[0] if len(call.keywords) == 1 and not call.args else None
        named = keyword.value if keyword is not None and keyword.arg == "replacement_cluster" else None
        node = self.classes.get(named.id) if isinstance(named, ast.Name) and named.id not in self.enums else None
        body = (node.body[1:] if node.body and docstring(node.body[0]) else node.body) if node is not None else []
        base = node.bases[0] if node is not None and len(node.bases) == 1 else None
        if not (isinstance(base, ast.Name) and self.imported.get(base.id) == CLUSTER and (self.import_modules.get(base.id), base.id) == TUYA_MCU
                and self.fixed.typed_tuya is not None and [ast.dump(stmt) for stmt in body] == [self.fixed.typed_tuya]):
            raise Refused(call, f"a Tuya chain that does not end in add_to_registry(replacement_cluster=Cls), Cls a class of the file on "
                                f"TuyaMCUCluster alone with the fixed {TYPED_TUYA} (it checks each datapoint value against its attribute's type)")

    @property
    def method_checks(self) -> dict:
        """The builder methods with a checker of their own."""
        return {
            HIDE: self.hide, REMOVE: self.removal, REPLACES_ENDPOINT: self.endpoint_replacement, CHANGE: self.change,
            "friendly_name": self.friendly_name, "exposes_feature": self.feature, "replace_cluster_occurrences": self.cluster_replacement,
            "firmware_version_filter": self.firmware,
        }

    def cluster_value(self, node: ast.expr) -> bool:
        """A cluster id: an integer literal up to 0xFFFF or the cluster_id of a cluster class the file imports or defines."""
        named = (isinstance(node, ast.Attribute) and node.attr == "cluster_id" and isinstance(node.value, ast.Name)
                 and (self.imported.get(node.value.id) == CLUSTER or (node.value.id in self.classes and node.value.id not in self.enums)))
        return named or _int_in(node, range(0x10000))

    def entity_keywords(self, call: ast.Call, keywords: list[ast.keyword], allowed: frozenset[str], label: str) -> None:
        """The keywords that pick entities (HIDE, EntityFilter): only those of allowed, cluster_id among them. The
        endpoint is an integer literal from 1 to 254, the cluster as cluster_value reads it, the suffix a string
        literal SUFFIX allows, entity_cls the name of an entity class of ENTITY_CLASSES the file imports."""
        if any(keyword.arg not in allowed for keyword in keywords):
            raise Refused(call, f"{label} with other arguments than the keywords {', '.join(sorted(allowed))}")
        if len({keyword.arg for keyword in keywords}) != len(keywords):
            raise Refused(call, f"{label} with a repeated keyword")
        values = {keyword.arg: keyword.value for keyword in keywords}
        if "cluster_id" not in values:
            raise Refused(call, f"{label} without cluster_id")
        if "endpoint_id" in values and not _int_in(values["endpoint_id"], range(1, 255)):
            raise Refused(values["endpoint_id"], f"{label} endpoint_id other than an integer from 1 to 254")
        if not self.cluster_value(values["cluster_id"]):
            raise Refused(values["cluster_id"], f"{label} cluster_id other than an integer from 0 to 0xFFFF or a cluster class's cluster_id")
        if "unique_id_suffix" in values and not self.text(values["unique_id_suffix"], SUFFIX):
            raise Refused(values["unique_id_suffix"], f"{label} unique_id_suffix other than a string of letters, digits, _ and -")
        if "entity_cls" in values:
            entity = values["entity_cls"]
            if not (isinstance(entity, ast.Name) and entity.id in ENTITY_CLASSES and self.imported.get(entity.id) == ENTITY_CLASS):
                raise Refused(entity, f"{label} entity_cls other than {', '.join(sorted(ENTITY_CLASSES))}, imported")

    @staticmethod
    def text(node: ast.expr, pattern: re.Pattern) -> bool:
        """A string literal the pattern matches whole, UTF-8, with no character of FORBIDDEN_CATEGORIES."""
        return (isinstance(node, ast.Constant) and type(node.value) is str and pattern.fullmatch(node.value) is not None
                and _utf8(node.value) and not any(unicodedata.category(char) in FORBIDDEN_CATEGORIES for char in node.value))

    def placeholders(self, node: ast.expr) -> bool:
        """Translation placeholders: a dict of up to 8 names (SUFFIX) and texts (TEXT), literals."""
        return (isinstance(node, ast.Dict) and 0 < len(node.keys) <= 8 and all(k is not None and self.text(k, SUFFIX) for k in node.keys)
                and all(self.text(v, TEXT) for v in node.values))

    def hide(self, call: ast.Call) -> None:
        """.prevent_default_entity_creation(endpoint_id=1, cluster_id=..., unique_id_suffix="..."):
        keywords of HIDE_KEYWORDS only (entity_keywords), cluster_id among them."""
        if call.args:
            raise Refused(call, f"{HIDE} with other arguments than the keywords {', '.join(sorted(HIDE_KEYWORDS))}")
        self.entity_keywords(call, call.keywords, HIDE_KEYWORDS, HIDE)

    def removal(self, call: ast.Call) -> None:
        """.removes_entity(EntityFilter(entity_cls=IASZone, endpoint_id=1, cluster_id=...)): one filter with the
        keywords of FILTER_KEYWORDS only (entity_keywords), cluster_id among them."""
        inner = call.args[0] if len(call.args) == 1 and not call.keywords else None
        if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id == "EntityFilter"
                and self.imported.get(inner.func.id) == ENTITY_FILTER and not inner.args):
            raise Refused(call, f"{REMOVE} other than one EntityFilter(...) with keywords")
        self.entity_keywords(inner, inner.keywords, FILTER_KEYWORDS, "EntityFilter")

    def endpoint_replacement(self, call: ast.Call) -> None:
        """.replaces_endpoint(1, device_type=DeviceType.DIMMABLE_LIGHT): an endpoint integer from 1 to 254 as
        the one positional argument and the keyword device_type as a member of DeviceType (on the Tuya builder
        too, which takes more forms: no generated file writes another)."""
        if len(call.args) != 1 or not _int_in(call.args[0], range(1, 255)):
            raise Refused(call, f"{REPLACES_ENDPOINT} other than an endpoint integer from 1 to 254 and device_type")
        if len({keyword.arg for keyword in call.keywords}) != len(call.keywords):
            raise Refused(call, f"{REPLACES_ENDPOINT} with a repeated keyword")
        for keyword in call.keywords:
            if keyword.arg != "device_type" or not self.member(keyword.value, (DEVICE_TYPE,)):
                raise Refused(call, f"{REPLACES_ENDPOINT} with a keyword other than device_type={DEVICE_TYPE}.MEMBER")

    def change(self, call: ast.Call) -> None:
        """.change_entity_metadata(endpoint_id=1, cluster_id=..., new_primary=False, ...): only the literal
        keywords of CHANGE_KEYWORDS, at least one entity filter and one new_* among them (see change_value)."""
        names = {keyword.arg for keyword in call.keywords}
        if call.args or not names <= CHANGE_KEYWORDS:
            raise Refused(call, f"{CHANGE} with other arguments than the keywords {', '.join(sorted(CHANGE_KEYWORDS))}")
        if len(names) != len(call.keywords):
            raise Refused(call, f"{CHANGE} with a repeated keyword")
        if not names & CHANGE_FILTERS or not any(name.startswith("new_") for name in names):
            raise Refused(call, f"{CHANGE} without an entity filter and a new_* keyword")
        for keyword in call.keywords:
            if not self.change_value(keyword.arg, keyword.value):
                raise Refused(keyword.value, f"{CHANGE} {keyword.arg} other than {CHANGE_KINDS.get(keyword.arg, 'its literal kind')}")

    def change_value(self, name: str, value: ast.expr) -> bool:
        if name == "endpoint_id":
            return _int_in(value, range(1, 255))
        if name == "cluster_id":
            return self.cluster_value(value)
        if name == "unique_id_suffix" or name in CHANGE_TEXT:
            return self.text(value, SUFFIX)
        if name in CHANGE_BOOL:
            return isinstance(value, ast.Constant) and type(value.value) is bool
        if name == "new_primary":
            # True would make every entity the filter matches explicitly primary: two stop ZHA (see the docstring).
            return isinstance(value, ast.Constant) and value.value is False
        if name == "new_fallback_name":
            return self.text(value, TEXT)
        if name in CHANGE_ENUMS:
            return self.member(value, CHANGE_ENUMS[name]) and value.attr in CHANGE_MEMBERS.get(name, (value.attr,))
        # new_translation_placeholders: a dict of up to 8 names and texts.
        return self.placeholders(value)

    def member(self, node: ast.expr, enums: tuple[str, ...]) -> bool:
        """Name.MEMBER with Name one of enums, imported as an enum (kind ENUM)."""
        return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in enums
                and self.imported.get(node.value.id) == ENUM and MEMBER.fullmatch(node.attr) is not None)

    def friendly_name(self, call: ast.Call) -> None:
        """.friendly_name(model="...", manufacturer="..."): both keywords as text literals, a name its manifest
        entry gives the device (compared in targeting)."""
        values = {keyword.arg: keyword.value for keyword in call.keywords}
        if call.args or set(values) != {"model", "manufacturer"} or len(call.keywords) != 2 or not all(self.text(v, TEXT) for v in values.values()):
            raise Refused(call, "friendly_name other than the keywords model and manufacturer, each a text literal")
        self.friendly_names.append((call, (values["manufacturer"].value, values["model"].value)))

    def feature(self, call: ast.Call) -> None:
        """.exposes_feature("siren_basic"): one string literal FEATURE allows, no config."""
        if len(call.args) != 1 or call.keywords or not self.text(call.args[0], FEATURE):
            raise Refused(call, "exposes_feature other than one feature name of lower case letters, digits, _ and .")

    def cluster_replacement(self, call: ast.Call) -> None:
        """.replace_cluster_occurrences(Cluster, replace_server_instances=True, replace_client_instances=False):
        one cluster class the file defines (not an imported zigpy class: it would swap the clusters of
        every device of that kind), bool literal keywords, none twice."""
        cluster = call.args[0] if len(call.args) == 1 else None
        if not (isinstance(cluster, ast.Name) and cluster.id in self.classes and cluster.id not in self.enums):
            raise Refused(call, "replace_cluster_occurrences other than one cluster class the file defines")
        if len({keyword.arg for keyword in call.keywords}) != len(call.keywords):
            raise Refused(call, "replace_cluster_occurrences with a repeated keyword")
        for keyword in call.keywords:
            if keyword.arg not in REPLACE_KEYWORDS or not (isinstance(keyword.value, ast.Constant) and type(keyword.value.value) is bool):
                raise Refused(call, f"replace_cluster_occurrences with a keyword other than {', '.join(sorted(REPLACE_KEYWORDS))} as a bool literal")

    def firmware(self, call: ast.Call) -> None:
        """.firmware_version_filter(min_version=0x01000000, max_version=..., allow_missing=True): keywords
        only, integers from 0 to 0xFFFFFFFF (minimum below maximum) and a bool. It only narrows the devices
        the file's fingerprints match: the filter is one more condition on them."""
        values = {keyword.arg: keyword.value for keyword in call.keywords}
        if call.args or not set(values) & set(FIRMWARE_VERSIONS) or not set(values) <= {*FIRMWARE_VERSIONS, "allow_missing"} or len(values) != len(call.keywords):
            raise Refused(call, "firmware_version_filter other than the keywords min_version, max_version and allow_missing")
        if not all(isinstance(value, ast.Constant) and _int_in(value, FIRMWARE_RANGE) for name, value in values.items() if name in FIRMWARE_VERSIONS):
            raise Refused(call, "firmware_version_filter version other than an integer from 0 to 0xFFFFFFFF")
        if "allow_missing" in values and not (isinstance(values["allow_missing"], ast.Constant) and type(values["allow_missing"].value) is bool):
            raise Refused(call, "firmware_version_filter allow_missing other than a bool literal")
        if all(name in values for name in FIRMWARE_VERSIONS) and not values["min_version"].value < values["max_version"].value:
            raise Refused(call, "firmware_version_filter with min_version not below max_version")

    def reporting_config(self, call: ast.Call, value: ast.expr) -> None:
        """reporting_config= of a chain call: None or ReportingConfig(...) as the reporting_config of an entity method of
        a QuirkBuilder chain (reported_attribute, reporting), nothing else."""
        if isinstance(value, ast.Constant) and value.value is None:
            return
        if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "ReportingConfig"
                and self.imported.get(value.func.id) == REPORTING):
            raise Refused(value, "reporting_config other than ReportingConfig(...) or None")
        if not (self.builder == "QuirkBuilder" and call in self.calls and call.func.attr in REPORTING_METHODS):
            raise Refused(value, "ReportingConfig other than the reporting_config of an entity method of a QuirkBuilder chain")
        self.reported_attribute(call)
        self.reporting(value)

    def reported_attribute(self, call: ast.Call) -> None:
        """The entity of a ReportingConfig reads an attribute its cluster certainly has: ZHA looks the name up there
        when it configures the device, an unknown one stops the configuration of the device on join. Keywords only,
        attribute_name=Cls.AttributeDefs.<name>.name and cluster_id=Cls.cluster_id of a cluster class Cls of the
        file (a name Cls lacks fails at import), the server cluster of an endpoint (an integer literal, 1 without
        one) where the chain puts Cls (placed) and nothing that may take its place (rival)."""
        values = {keyword.arg: keyword.value for keyword in call.keywords}
        attribute, cluster = values.get("attribute_name"), values.get("cluster_id")
        named = (isinstance(attribute, ast.Attribute) and attribute.attr == "name" and isinstance(attribute.value, ast.Attribute)
                 and isinstance(attribute.value.value, ast.Attribute) and attribute.value.value.attr == "AttributeDefs"
                 and isinstance(attribute.value.value.value, ast.Name) and attribute.value.value.value.id) or None
        if call.args or named not in self.classes or named in self.enums or not (
            isinstance(cluster, ast.Attribute) and cluster.attr == "cluster_id" and isinstance(cluster.value, ast.Name) and cluster.value.id == named
        ):
            raise Refused(call, "a ReportingConfig on an entity other than of the keywords attribute_name=Cls.AttributeDefs.<name>.name, "
                                "cluster_id=Cls.cluster_id of a cluster class Cls the file defines")
        endpoint = values.get("endpoint_id", ast.Constant(1))
        if "cluster_type" in values or not _int_in(endpoint, range(1, 255)):
            raise Refused(call, "a ReportingConfig on an entity of a client cluster (cluster_type) or of an endpoint that is no integer from 1 to 254")
        if not self.placed(named, endpoint.value):
            raise Refused(call, f"a ReportingConfig on {named}, which the chain does not put on endpoint {endpoint.value} with replaces, adds or replace_cluster_occurrences")
        if rival := self.rival(named, endpoint.value):
            raise Refused(call, f"a ReportingConfig on {named}, while the chain may put {rival} in its place on endpoint {endpoint.value}")

    def placement(self, call: ast.Call) -> ast.expr | None:
        """The cluster class (or id) a builder call puts on the device, None for a call that puts none (PLACEMENTS)."""
        keyword = PLACEMENTS.get(call.func.attr) if isinstance(call.func, ast.Attribute) else None
        if keyword is None:
            return None
        return call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == keyword), None)

    def placed(self, cls: str, endpoint: int) -> bool:
        """The chain puts the class on the server side of the endpoint: replaces(Cls) or adds(Cls) with at most
        endpoint_id (that integer literal, 1 without one) or replace_cluster_occurrences(Cls) for server instances."""
        for call in self.calls:
            target = self.placement(call)
            if not (isinstance(target, ast.Name) and target.id == cls and call.args == [target]):
                continue
            keywords = {keyword.arg: keyword.value for keyword in call.keywords}
            if call.func.attr == "replace_cluster_occurrences":
                if not (isinstance(keywords.get("replace_server_instances"), ast.Constant) and keywords["replace_server_instances"].value is False):
                    return True
            elif set(keywords) <= {"endpoint_id"} and _int_in(keywords.get("endpoint_id", ast.Constant(1)), range(endpoint, endpoint + 1)):
                return True
        return False

    def rival(self, cls: str, endpoint: int) -> str | None:
        """The text of the first of rivals, None without one."""
        found = self.rivals(cls, endpoint)
        return _short(found[0]) if found else None

    def rivals(self, cls: str, endpoint: int) -> list[ast.expr]:
        """Each other class the chain puts on the device that may sit where the entity reads Cls: on that endpoint's server
        side or where the call does not say for sure, with a cluster id that may be Cls's (the target as the call names it).
        An integer that adds() names takes no place a class holds (zigpy keeps the cluster there)."""
        found = []
        for call in self.calls:
            target = self.placement(call)
            if target is None or (isinstance(target, ast.Name) and target.id == cls) or (call.func.attr == "adds" and _int_in(target, range(0x10000))):
                continue
            keywords = {keyword.arg: keyword.value for keyword in call.keywords}
            if call.func.attr == "replace_cluster_occurrences":
                if isinstance(keywords.get("replace_server_instances"), ast.Constant) and keywords["replace_server_instances"].value is False:
                    continue
            elif (len(call.args) <= 1 and "cluster_type" not in keywords and "endpoint_id" in keywords
                  and isinstance(keywords["endpoint_id"], ast.Constant) and keywords["endpoint_id"].value != endpoint):
                continue
            elif len(call.args) <= 1 and "cluster_type" not in keywords and "endpoint_id" not in keywords and endpoint != 1:
                continue
            if not _differ(self.cluster_identity(cls), self.cluster_identity(target)):
                found.append(target)
        return found

    def cluster_identity(self, node: ast.expr | str) -> int | str | None:
        """Where a cluster class has its cluster id from: an integer (a cluster_id literal of a class of the file or
        CLUSTER_IDS), the name of the zigpy cluster class whose id it has or None when that is not known."""
        name = node if isinstance(node, str) else node.id if isinstance(node, ast.Name) else None
        if name in self.classes and name not in self.enums:
            for stmt in self.classes[name].body:
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == "cluster_id":
                    return stmt.value.value if _int_in(stmt.value, range(0x10000)) else None
            # The first base that has an id gives it (zhaquirks' CustomCluster and LocalDataCluster have none).
            for base in self.classes[name].bases:
                if (found := self.cluster_identity(base)) is not None:
                    return found
            return None
        if name is None or self.imported.get(name) != CLUSTER:
            return None
        if (self.import_modules.get(name), name) in SKIP_REGISTRY:
            return CLUSTER_IDS.get(name)
        return name  # a zigpy cluster class of CLUSTER_MODULES

    def reporting(self, node: ast.Call) -> None:
        """ReportingConfig(min_interval=15, max_interval=300, reportable_change=1): the three keywords, intervals
        integer literals from 0 to 0xFFFF (the maximum 0 or at least MIN_MAX_INTERVAL, the minimum not above the
        maximum unless that is 0), the change an integer or float literal from 0 to 0xFFFFFFFF."""
        values = {keyword.arg: keyword.value for keyword in node.keywords}
        if node.args or set(values) != set(REPORTING_KEYWORDS) or len(values) != len(node.keywords):
            raise Refused(node, f"ReportingConfig other than the keywords {', '.join(REPORTING_KEYWORDS)}")
        if not all(_int_in(values[name], range(0x10000)) for name in REPORTING_KEYWORDS[:2]):
            raise Refused(node, "ReportingConfig interval other than an integer literal from 0 to 0xFFFF")
        low, high = (ast.literal_eval(values[name]) for name in REPORTING_KEYWORDS[:2])
        if 0 < high < MIN_MAX_INTERVAL:
            raise Refused(node, f"ReportingConfig max_interval other than 0 or {MIN_MAX_INTERVAL} to 65535")
        if high != 0 and low > high:
            raise Refused(node, "ReportingConfig min_interval above max_interval")
        change = values["reportable_change"]
        # An integer is compared as it is: math.isfinite of an integer beyond the float range raises.
        if not (isinstance(change, ast.Constant) and type(change.value) in (int, float) and (type(change.value) is int or math.isfinite(change.value))
                and 0 <= change.value <= MAX_CHANGE):
            raise Refused(node, f"ReportingConfig reportable_change other than a number literal from 0 to {MAX_CHANGE}")

    def arguments(self, call: ast.Call, where: str) -> None:
        for arg in call.args:
            self.expr(arg, where)
        for keyword in call.keywords:
            if keyword.arg is None:
                raise Refused(call, "** in a call")
            value = keyword.value
            if where == CHAIN and keyword.arg == DP_HANDLER:
                raise Refused(value, f"{DP_HANDLER}, the method of the Tuya cluster zhaquirks calls for a datapoint")
            if where == CHAIN and keyword.arg in CONVERTER_KEYWORDS:
                self.converter(value, keyword.arg)
            elif where == CHAIN and keyword.arg in SCALES:
                if not self.scale(keyword.arg, value):
                    raise Refused(value, f"{keyword.arg} other than {'a finite number literal other than 0' if keyword.arg == 'divisor' else f'a number literal from -{MAX_FACTOR} to {MAX_FACTOR} other than 0'}")
            elif where == CHAIN and keyword.arg == "mode":
                if not (isinstance(value, ast.Constant) and type(value.value) is str and value.value in NUMBER_MODES):
                    raise Refused(value, f"mode other than one of {', '.join(sorted(NUMBER_MODES))} (Home Assistant's NumberMode)")
            elif where == CHAIN and keyword.arg == "reporting_config":
                self.reporting_config(call, value)
            elif where == CHAIN and keyword.arg in TEXT_KEYWORDS and (keyword.arg in TEXT_ONLY or isinstance(value, ast.Constant) and type(value.value) is str):
                if not self.text(value, TEXT_KEYWORDS[keyword.arg]):
                    raise Refused(value, f"{keyword.arg} other than {TEXT_KINDS[TEXT_KEYWORDS[keyword.arg]]}")
            elif where == CHAIN and keyword.arg == PLACEHOLDERS:
                if not self.placeholders(value):
                    raise Refused(value, f"{PLACEHOLDERS} other than a dict of up to 8 names and text literals")
            elif (where == CLASS and keyword.arg == "zcl_type" and isinstance(call.func, ast.Name) and call.func.id == "ZCLAttributeDef"
                  and self.imported.get(call.func.id) == DEFINITION and self.data_type(value)):
                continue
            else:
                self.expr(value, where)

    @staticmethod
    def scale(keyword: str, node: ast.expr) -> bool:
        """A divisor: an integer or finite float literal other than 0 (`value /= divisor`). A multiplier: a number literal from
        -MAX_FACTOR to MAX_FACTOR other than 0 (`value * multiplier`; a number entity divides by it when it writes)."""
        if not number(node):
            return False
        value = ast.literal_eval(node)
        if value == 0 or (type(value) is float and not math.isfinite(value)):
            return False
        return keyword == "divisor" or abs(value) <= MAX_FACTOR

    def data_type(self, node: ast.expr) -> bool:
        """DataTypeId.uint8: a member of DataTypeId, which stands only here, as the zcl_type of a ZCLAttributeDef."""
        return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "DataTypeId"
                and self.imported.get(node.value.id) == DATA_TYPE and node.attr in DATA_TYPES)

    def converter(self, node: ast.expr, keyword: str) -> None:
        """lambda x: ... over what a converter may use (keyword: the one it is passed as), inside the fixed helper of its
        keyword (CONVERTER_HELPERS) where not every error of it is caught. The file defines that helper by its fixed code."""
        if helper := CONVERTER_HELPERS.get(keyword):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == helper and len(node.args) == 1 and not node.keywords):
                raise Refused(node, f"{keyword} other than {helper}(lambda x: ...) (not every error of the converter is caught)")
            if not (helper in self.functions and helper in self.fixed.helpers):
                raise Refused(node, f"{helper} without its fixed definition in the file")
            node = node.args[0]
        if not isinstance(node, ast.Lambda):
            raise Refused(node, "a converter other than lambda x: ...")
        args = node.args
        if args.posonlyargs or args.vararg or args.kwonlyargs or args.kwarg or args.defaults or [a.arg for a in args.args] != ["x"]:
            raise Refused(node, "a converter other than lambda x: ...")
        self.expr(node.body, LAMBDA)
        # Each product may repeat x (a string from the device) up to MAX_FACTOR times: one of them only,
        # in a Tuya converter only (MAX_FACTOR).
        products = sum(isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mult) for n in ast.walk(node.body))
        if keyword != "converter" and products:
            raise Refused(node, f"a product in {keyword}")
        if products > 1:
            raise Refused(node, "a converter with more than one product")

    def callee(self, func: ast.expr, where: str) -> bool:
        """Whether a call of func is allowed here: an imported callable of this place; in a
        converter also the casts, t.Bool, the file's enum classes and helpers."""
        if isinstance(func, ast.Name):
            name = func.id
            if name in self.imported:
                return self.imported[name] == CALLS[where]
            if where != LAMBDA:
                return False
            if name in BUILTINS:
                return name not in self.bound
            # The converter helpers stand only around a converter (converter): inside one they would return a function as the value.
            return name in self.enums or (name not in CONVERTER_HELPERS.values() and name in self.functions and name in self.fixed.helpers)
        return where == LAMBDA and self.zigpy_type(func, TYPE_CALLS)

    def name(self, node: ast.Name, where: str) -> None:
        name = node.id
        if where == LAMBDA and name == "x":
            return
        # Module-level values (FINGERPRINTS, _LOGGER) and the plain imports serve only the fixed code.
        if where == CHAIN and (self.imported.get(name) in (VALUE, CLUSTER) or name in self.classes or (name == FILTER and name in self.functions)):
            return
        raise Refused(node, f"name {name}")

    def attribute(self, node: ast.Attribute, spread: bool = False) -> None:
        """An allowed attribute path."""
        parts: list[str] = []
        root: ast.expr = node
        while isinstance(root, ast.Attribute):
            parts.insert(0, root.attr)
            root = root.value
        if isinstance(root, ast.Name) and self.path(root.id, parts, spread):
            return
        raise Refused(node, f"attribute {_short(node)}")

    def path(self, root: str, parts: list[str], spread: bool) -> bool:
        kind = self.imported.get(root)
        if spread:
            return kind == CLUSTER and parts == [SPREAD]
        if root == "t" and self.modules.get("t") == "zigpy.types":
            return len(parts) == 1 and parts[0] in TYPE_NAMES
        if kind == ENUM:
            return len(parts) == 1 and MEMBER.fullmatch(parts[0]) is not None
        if kind == FOUNDATION:
            return len(parts) == 2 and parts[0] == "ZCLAttributeAccess" and MEMBER.fullmatch(parts[1]) is not None
        if root in self.enums:
            return len(parts) == 1 and parts[0] in self.enums[root] and not parts[0].startswith("_")
        if kind == CLUSTER or root in self.classes:
            match parts:
                case ["cluster_id"] | ["ep_attribute"]:
                    return True
                case ["AttributeDefs", attribute, "name" | "id"] | ["ServerCommandDefs", attribute, "name"]:
                    return NAME.fullmatch(attribute) is not None
                case [nested, member] if nested in CLUSTER_ENUMS:
                    return MEMBER.fullmatch(member) is not None
        return False

    def expr(self, node: ast.expr, where: str) -> None:
        """A value in a builder chain, a converter or a class body."""
        match node:
            case ast.Constant(value=str() as text) if any(unicodedata.category(char) in FORBIDDEN_CATEGORIES for char in text):
                raise Refused(node, "text with a control, format, surrogate, line or paragraph separator character")
            case ast.Constant():
                return None
            case ast.Name():
                return self.name(node, where)
            case ast.Attribute():
                return self.attribute(node)
            case ast.UnaryOp(op=op, operand=operand):
                if isinstance(op, (ast.USub, ast.UAdd)) and isinstance(operand, ast.Constant) and type(operand.value) in (int, float):
                    return None
                if isinstance(op, ast.Not) and where == LAMBDA:
                    return self.expr(operand, where)
                raise Refused(node, f"operator {type(op).__name__}")
            case ast.BinOp(left=left, op=op, right=right):
                if not isinstance(op, OPERATORS[where]):
                    raise Refused(node, f"operator {type(op).__name__}")
                # A mask: x & 0x10. The only bit operator of a converter, with an integer literal that bounds
                # the result (no bool, no negative number, no other expression on either side).
                if isinstance(op, ast.BitAnd) and not (
                    isinstance(left, ast.Name) and left.id == "x" and isinstance(right, ast.Constant) and type(right.value) is int and right.value in MASK_RANGE
                ):
                    raise Refused(node, "& other than x & an integer literal from 0 to 0xFFFFFFFF")
                if isinstance(left, ast.Constant) and isinstance(right, ast.Constant):
                    raise Refused(node, "arithmetic on two constants")
                # Numbers only: a string or bytes would turn a device value into a long repetition.
                if any(isinstance(side, ast.Constant) and type(side.value) not in (int, float) for side in (left, right)):
                    raise Refused(node, "arithmetic on something other than a number")
                # A product is x times a number literal, as gen_tuya writes it. Anything else could be
                # a sequence the product repeats (str(x), a list, a tuple, a looked-up or conditional
                # value) or a count the device chooses (x * int(x)). x itself is a string when the
                # device says so (the Tuya type byte, up to 255 bytes): the literal bounds that
                # repetition, so an integer one stays small and a product never chains.
                if isinstance(op, ast.Mult) and not (isinstance(left, ast.Name) and left.id == "x" and factor(right)):
                    raise Refused(node, f"a product other than x times a number literal of at most {MAX_FACTOR}")
                # x + x, x * 1000 + x: two values from the device joined, a sequence grows with each one.
                if where == LAMBDA and not isinstance(op, ast.Mult) and not (number(left) or number(right)):
                    raise Refused(node, "arithmetic without a number literal on one side")
                self.expr(left, where)
                return self.expr(right, where)
            case ast.Compare(left=left, ops=ops, comparators=comparators) if where == LAMBDA:
                if not all(isinstance(op, CMPOPS) for op in ops):
                    raise Refused(node, "comparison operator")
                for value in (left, *comparators):
                    self.expr(value, where)
                return None
            case ast.IfExp(test=test, body=body, orelse=orelse) if where == LAMBDA:
                for value in (test, body, orelse):
                    self.expr(value, where)
                return None
            case ast.Subscript(value=ast.Dict() as value, slice=index) if where == LAMBDA:
                # {wire: value}[x]
                self.expr(value, where)
                return self.expr(index, where)
            case ast.Subscript(value=value, slice=index) if where == CLASS and self.zigpy_type(value, TYPE_LISTS):
                # t.List[t.uint8_t]
                if not self.zigpy_type(index, TYPE_NAMES):
                    raise Refused(node, f"list of {_short(index)}")
                return None
            case ast.List(elts=elts) | ast.Tuple(elts=elts) | ast.Set(elts=elts):
                if where == LAMBDA:
                    # Each item would be computed: many products, each up to MAX_FACTOR times x.
                    raise Refused(node, f"a {type(node).__name__.lower()} in a converter")
                for value in elts:
                    self.expr(value, where)
                return None
            case ast.Dict(keys=keys, values=values):
                if where == LAMBDA and any(isinstance(n, ast.Name) and n.id == "x" for item in (*keys, *values) if item is not None for n in ast.walk(item)):
                    # {wire: value}[x]: every value is computed before the lookup.
                    raise Refused(node, "x in a dict of a converter (only as the key looked up)")
                for key, value in zip(keys, values, strict=True):
                    if key is None:
                        if where != CLASS or not isinstance(value, ast.Attribute):
                            raise Refused(node, f"** other than a cluster's {SPREAD}")
                        self.attribute(value, spread=True)
                        continue
                    self.expr(key, where)
                    self.expr(value, where)
                return None
            case ast.Call(func=func):
                if not self.callee(func, where):
                    raise Refused(node, f"call of {_short(func)}")
                return self.arguments(node, where)
        raise Refused(node, f"{type(node).__name__} expression")


def _quoted(pair: tuple[str | None, str]) -> str:
    text = repr(pair)
    return text if len(text) <= 80 else text[:77] + "..."


def _differ(one: int | str | None, other: int | str | None) -> bool:
    """Two cluster identities (QuirkFile.cluster_identity) that certainly stand for different cluster ids: two integers
    that differ, two zigpy cluster classes of different names (their ids are distinct) or a zigpy class beside an
    integer from STANDARD_IDS_BELOW on (all of zigpy's lie below it). Anything unknown may be the same id."""
    if one is None or other is None:
        return False
    if type(one) is type(other):
        return one != other
    return (one if isinstance(one, int) else other) >= STANDARD_IDS_BELOW


class AttributeNeed:
    """The type of an attribute an entity computes with that the guard cannot see, for the check inside Home Assistant
    (attribute_types.py): the line and method of the entity, the file's name of the class it reads (owner), the attribute,
    the imported classes (module, class) its definition may come from, what its type must fit (None for a number, else
    the bits of the enum class the entity names: an unsigned integer type at most that wide) and whether the name may be
    missing (optional: a class that may stand in the place of the one the entity names)."""

    def __init__(self, line: int, method: str, owner: str, attribute: str, candidates: tuple[tuple[str, str], ...], bits: int | None,
                 optional: bool = False) -> None:
        self.line = line
        self.method = method
        self.owner = owner
        self.attribute = attribute
        self.candidates = candidates
        self.bits = bits
        self.optional = optional

    def __repr__(self) -> str:
        return f"AttributeNeed({self.line}, {self.method!r}, {self.owner!r}, {self.attribute!r}, {self.candidates!r}, {self.bits!r}, {self.optional!r})"


class Inheritance:
    """What a file the guard passed does with one of ZHA's classes it builds on (a pair of Targets.inherits), for the
    check inside Home Assistant: its class on it (own), the members its nested classes define (nested class -> name
    -> id) and the names it reads through its class without defining them (nested class -> names), which ZHA's class
    must have."""

    def __init__(self, own: str, defines: dict[str, dict[str, int]], reads: dict[str, frozenset[str]]) -> None:
        self.own = own
        self.defines = defines
        self.reads = reads


def attribute_needs(source: str | bytes, fixed: Fixed, targets: Targets | None) -> list[AttributeNeed]:
    """The attribute types of a file the guard passed with these targets that the check inside Home Assistant looks up
    (attribute_types.py): one AttributeNeed per entity that computes with an attribute whose type the guard cannot see."""
    quirk = QuirkFile(ast.parse(source), fixed, targets.inherits if targets else frozenset())
    quirk.problems(targets)
    return quirk.needs


def inheritances(source: str | bytes, inherits: frozenset[tuple[str, str]]) -> dict[tuple[str, str], Inheritance]:
    """Per pair of inherits, what a file the guard passed with these inherits does with it (Inheritance). For such a file
    only: it reads the shapes QuirkFile.inheriting_class allows. The reads are the paths Own.AttributeDefs.x and
    Own.ServerCommandDefs.x (of .name or .id) anywhere in the file."""
    tree = ast.parse(source)
    imported = {alias.name: (stmt.module, alias.name) for stmt in tree.body if isinstance(stmt, ast.ImportFrom) for alias in stmt.names
                if (stmt.module, alias.name) in inherits}
    owners: dict[str, tuple[str, str]] = {}
    defined: dict[tuple[str, str], dict[str, dict[str, int]]] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef) and len(stmt.bases) == 1 and isinstance(stmt.bases[0], ast.Name) and stmt.bases[0].id in imported:
            pair = owners[stmt.name] = imported[stmt.bases[0].id]
            defined[pair] = {nested: {} for nested in INHERIT_NESTED}
            for nested in stmt.body:
                if isinstance(nested, ast.ClassDef) and nested.name in INHERIT_NESTED:
                    for member in (m for m in nested.body if isinstance(m, ast.Assign)):
                        defined[pair][nested.name][member.targets[0].id] = next(k.value.value for k in member.value.keywords if k.arg == "id")
    reads: dict[tuple[str, str], dict[str, set[str]]] = {pair: {nested: set() for nested in INHERIT_NESTED} for pair in defined}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) and isinstance(node.value.value, ast.Name)
                and node.value.value.id in owners and node.value.attr in INHERIT_NESTED):
            pair = owners[node.value.value.id]
            if node.attr not in defined[pair][node.value.attr]:
                reads[pair][node.value.attr].add(node.attr)
    own = {pair: name for name, pair in owners.items()}
    return {pair: Inheritance(own[pair], defined[pair], {nested: frozenset(names) for nested, names in reads[pair].items()}) for pair in defined}


def check_quirk(source: str | bytes, fixed: Fixed, targets: Targets | None) -> list[tuple[int, str]]:
    """(line, reason) for every statement of a quirk file outside the allowlist and every target
    outside its manifest entry's (Targets.of; None refuses the file); empty when it passes. Bytes
    are parsed as Python imports them."""
    raw = source.encode() if isinstance(source, str) else source
    # Quirk files have none; another encoding could make the file read differently.
    if any(CODING.match(line) for line in LINE_BREAK.split(raw.removeprefix(b"\xef\xbb\xbf"), maxsplit=2)[:2]):
        return [(1, "coding declaration")]
    try:
        tree = ast.parse(source)
        found = [(getattr(node, "lineno", 1), reason) for node, reason in QuirkFile(tree, fixed, targets.inherits if targets else frozenset()).problems(targets)]
        if not found:
            # What the checkers read is the tree, what ZHA runs is what compile() makes of it: a file the
            # compiler refuses (a keyword given twice that a checker read once) is refused here, whatever the checks said.
            try:
                # As importlib compiles it: without this module's future features (annotations).
                compile(tree, "<quirk>", "exec", dont_inherit=True)
            except Exception as error:  # noqa: BLE001 - SyntaxError for a repeated keyword, anything else is as bad
                return [(getattr(error, "lineno", None) or 0, f"does not compile: {getattr(error, 'msg', type(error).__name__)}")]
        return found
    except SyntaxError as error:
        return [(error.lineno or 0, f"does not parse: {error.msg}")]
    except (ValueError, RecursionError, MemoryError) as error:
        return [(0, f"does not parse: {type(error).__name__}")]
    except Exception as error:  # noqa: BLE001 - a file the guard cannot judge is refused, never a crash
        return [(0, f"cannot be checked: {type(error).__name__}")]
