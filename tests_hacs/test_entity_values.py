"""divisor and multiplier, and the attribute types of the entities that compute with them or with an enum.

ZHA computes an entity's state while it adds the device's entities, also at every start inside load_devices, where
nothing catches an error: `value /= divisor`, `value * multiplier`, `EnumClass(value)`. A divisor of 0, text where a
number is meant, an int8s of -1 or a uint16 of 256 for a t.enum8 stopped ZHA at every start. The
guard checks the scaling literals and the attribute's type where the file defines it (guard.QuirkFile.entity_value),
the check inside Home Assistant the types it cannot see (attribute_types.py)."""

from __future__ import annotations

import pytest
import zigpy.types as t

from .integration import attribute_types, guard, manager
from .test_guard_files import METER, SHIPPED, TUYA_END, ZCL, frozen, targets

SWITCH_END = "    # water_leak: standard clusters, ZHA's own entities\n"
DEFS_END = "        alarm_on_motion = ZCLAttributeDef(id=0x0003, type=t.Bool, manufacturer_code=0x1209)\n"
CLASS = "class BoschWaterAlarmCluster(CustomCluster):\n"
ENUMS = ("class Mode8(t.enum8):\n    \"\"\"Mode values.\"\"\"\n\n    off = 0\n    on = 1\n\n\n"
         "class Mode16(t.enum16):\n    \"\"\"Mode values.\"\"\"\n\n    off = 0\n    on = 1\n\n\n")
OWN = "BoschWaterAlarmCluster"


def zcl(entity: str, attribute_type: str = "t.uint16_t", imports: str = "") -> str:
    """The Bosch file with an attribute `level` of this type on its cluster, two enum classes and this entity call."""
    text = frozen(ZCL)
    text = text.replace(DEFS_END, DEFS_END + f"        level = ZCLAttributeDef(id=0x0004, type={attribute_type}, manufacturer_code=0x1209)\n", 1)
    text = text.replace(CLASS, ENUMS + CLASS, 1)
    text = text.replace(SWITCH_END, f"    {entity}\n" + SWITCH_END, 1)
    if imports:
        text = text.replace("from zhaquirks.clusters import CustomCluster\n", f"from zhaquirks.clusters import CustomCluster\n{imports}", 1)
    return text


def own(method: str, keywords: str, cls: str = OWN, name: str = "level") -> str:
    return (f".{method}(attribute_name={cls}.AttributeDefs.{name}.name, cluster_id={cls}.cluster_id, {keywords}, "
            f"translation_key='{name}_x', fallback_name='Level x')")


def problems(text: str, name: str = ZCL) -> list[str]:
    return list(SHIPPED.problems(text.encode(), targets(name)))


def verdict(text: str, name: str = ZCL) -> tuple[str, ...]:
    """The guard's reasons, then those of the checks inside Home Assistant."""
    return manager.verdict_of(SHIPPED, text.encode(), targets(name))


def refused(found, reason: str) -> None:
    assert found and all(reason in problem for problem in found), found


# Divisor and multiplier take number literals only, no 0, nothing infinite, a multiplier at most MAX_FACTOR.
GOOD_SCALES = ["divisor=10", "divisor=0.5", "divisor=-2", "multiplier=0.001", "multiplier=1000", "multiplier=-1000.0", "divisor=10, multiplier=2"]
BAD_SCALES = {
    "divisor=0": "divisor other than", "divisor=0.0": "divisor other than", "divisor=-0": "divisor other than",
    "divisor=1e999": "divisor other than", "divisor='10'": "divisor other than", "divisor=True": "divisor other than",
    "divisor=MAX": "divisor other than",
    "multiplier=0": "multiplier other than", "multiplier=1001": "multiplier other than", "multiplier=-1001": "multiplier other than",
    "multiplier=1000.5": "multiplier other than", "multiplier=1e999": "multiplier other than", "multiplier=100000000": "multiplier other than",
}


@pytest.mark.parametrize("scale", GOOD_SCALES)
def test_a_scaled_sensor_on_a_number_attribute_passes(scale):
    assert problems(zcl(own("sensor", scale))) == []


@pytest.mark.parametrize("scale", sorted(BAD_SCALES))
def test_a_scale_other_than_a_bounded_number_literal_is_refused(scale):
    refused(problems(zcl(own("sensor", scale))), BAD_SCALES[scale])


def test_a_number_takes_the_same_multiplier_rule():
    assert problems(zcl(own("number", "multiplier=0.5, min_value=0, max_value=100, step=1"))) == []
    refused(problems(zcl(own("number", "multiplier=0, min_value=0, max_value=100, step=1"))), "multiplier other than")


# Reproducers: each passed an earlier guard and stopped ZHA at the next start (ZeroDivisionError, TypeError, or text repeated 1e8 times).
REPRODUCERS = [
    ".sensor(attribute_name='zcl_version', cluster_id=0, divisor=0, translation_key='zcl_version_x', fallback_name='Version')",
    ".sensor(attribute_name='model', cluster_id=0, divisor=10, translation_key='model_text', fallback_name='Model')",
    ".sensor(attribute_name='model', cluster_id=0, multiplier=0.5, translation_key='model_text', fallback_name='Model')",
    ".sensor(attribute_name='model', cluster_id=0, multiplier=100000000, translation_key='model_text', fallback_name='Model')",
    ".sensor(attribute_name='model', cluster_id=0, multiplier=1000, translation_key='model_text', fallback_name='Model')",
]


@pytest.mark.parametrize("entity", REPRODUCERS)
def test_the_scaling_reproducers_are_refused(entity):
    assert problems(zcl(entity))


# An entity that scales reads a number attribute.
@pytest.mark.parametrize("attribute_type", ["t.uint16_t", "t.int16s", "t.uint48_t", "t.Single", "t.Bool", "t.bitmap8", "t.enum8"])
def test_a_scaled_sensor_reads_a_number_type(attribute_type):
    assert problems(zcl(own("sensor", "divisor=10"), attribute_type)) == []


@pytest.mark.parametrize("attribute_type", ["t.CharacterString", "t.LVBytes"])
def test_a_scaled_sensor_on_text_is_refused(attribute_type):
    refused(problems(zcl(own("sensor", "divisor=10"), attribute_type)), "while ZHA computes with it: the type must be a number type")
    refused(problems(zcl(own("number", "multiplier=2, min_value=0, max_value=100, step=1"), attribute_type)), "the type must be a number type")


def test_an_unscaled_sensor_on_text_passes():
    """No divisor, no multiplier: ZHA's formatter passes the value through."""
    assert problems(zcl(own("sensor", "entity_type=EntityType.STANDARD"), "t.CharacterString")) == []


@pytest.mark.parametrize("entity", [
    ".sensor(attribute_name='level', cluster_id=BoschWaterAlarmCluster.cluster_id, divisor=10, fallback_name='Level x')",
    ".sensor(attribute_name=BoschWaterAlarmCluster.AttributeDefs.level.name, cluster_id=0xFCAC, divisor=10, fallback_name='Level x')",
    ".sensor(attribute_name=BoschWaterAlarmCluster.AttributeDefs.level.name, cluster_id=BoschWaterAlarmCluster.cluster_id, endpoint_id=2, divisor=10, fallback_name='Level x')",
])
def test_a_scaled_entity_names_its_attribute_through_the_class_where_it_sits(entity):
    assert problems(zcl(entity))


# An enum entity reads its enum class or an unsigned type no wider than the enum.
@pytest.mark.parametrize(("attribute_type", "enum_class"), [
    ("t.uint8_t", "Mode8"), ("t.Bool", "Mode8"), ("t.bitmap8", "Mode8"), ("t.enum8", "Mode8"),
    ("t.uint16_t", "Mode16"), ("t.bitmap16", "Mode16"), ("t.enum16", "Mode16"), ("t.uint8_t", "Mode16"),
])
def test_an_enum_entity_on_a_type_its_enum_takes_whole_passes(attribute_type, enum_class):
    assert problems(zcl(own("enum", f"enum_class={enum_class}"), attribute_type)) == []


@pytest.mark.parametrize(("attribute_type", "enum_class"), [
    ("t.int8s", "Mode8"), ("t.uint16_t", "Mode8"), ("t.bitmap16", "Mode8"), ("t.uint32_t", "Mode8"), ("t.enum16", "Mode8"),
    ("t.int16s", "Mode16"), ("t.uint24_t", "Mode16"), ("t.Single", "Mode8"), ("t.CharacterString", "Mode8"),
])
def test_an_enum_entity_on_a_wider_or_signed_type_is_refused(attribute_type, enum_class):
    refused(problems(zcl(own("enum", f"enum_class={enum_class}"), attribute_type)), "while ZHA computes with it: the type must be an unsigned integer type of at most")


def test_an_enum_entity_names_an_enum_class_of_the_file():
    refused(problems(zcl(own("enum", "enum_class=t.enum8"))), "enum with an enum_class other than an enum class of the file")


def test_the_enum_reproducer_is_refused():
    """z2m_heiman_hs9ms_e: camera_ready is an int8s, its sensor a t.enum8; a report of -1 stopped ZHA at every start."""
    entity = own("enum", "enum_class=Mode8, entity_platform=EntityPlatform.SENSOR, entity_type=EntityType.STANDARD")
    text = zcl(entity, "t.int8s").replace("    EntityType,\n", "    EntityPlatform,\n    EntityType,\n", 1)
    refused(problems(text), "level of the type t.int8s, while ZHA computes with it")
    assert problems(text.replace("type=t.int8s", "type=t.uint8_t")) == []


# Where the guard cannot see the type (zigpy's class of a standard cluster) the check inside Home Assistant looks it up.
BASIC = "from zigpy.zcl.clusters.general import Basic, MultistateOutput\n"


def test_an_imported_cluster_hands_the_type_to_the_check_inside_home_assistant():
    text = zcl(own("sensor", "divisor=10", cls="Basic", name="zcl_version"), imports=BASIC)
    assert problems(text) == []
    needs = guard.attribute_needs(text, SHIPPED.fixed, targets(ZCL))
    assert [(n.owner, n.attribute, n.candidates, n.bits, n.optional) for n in needs] == [
        ("Basic", "zcl_version", (("zigpy.zcl.clusters.general", "Basic"),), None, False)]
    assert verdict(text) == ()


def test_a_scaled_sensor_on_a_standard_text_attribute_is_refused_inside_home_assistant():
    """The `model` reproducer in the shape the guard passes: Basic.model is a CharacterString."""
    text = zcl(own("sensor", "divisor=10", cls="Basic", name="model"), imports=BASIC)
    assert problems(text) == []
    refused(verdict(text), "of the type LimitedCharString, while ZHA computes with it: the type must be a number type")


def test_an_enum_on_a_standard_attribute_is_checked_inside_home_assistant():
    assert verdict(zcl(own("enum", "enum_class=Mode8", cls="Basic", name="power_source"), imports=BASIC)) == ()
    refused(verdict(zcl(own("enum", "enum_class=Mode8", cls="MultistateOutput", name="present_value"), imports=BASIC)),
            "of the type uint16_t, while ZHA computes with it: the type must be an unsigned integer type of at most 8 bits")
    assert verdict(zcl(own("enum", "enum_class=Mode16", cls="MultistateOutput", name="present_value"), imports=BASIC)) == ()


def test_a_name_no_class_defines_is_refused_inside_home_assistant():
    refused(verdict(zcl(own("sensor", "divisor=10", cls="Basic", name="no_such_attribute"), imports=BASIC)), "none of Basic defines it")


@pytest.mark.parametrize(("kind", "bits", "fit"), [
    (t.uint16_t, None, True), (t.Single, None, True), (t.CharacterString, None, False), (t.LVBytes, None, False), (str, None, False),
    (t.uint8_t, 8, True), (t.Bool, 8, True), (t.bitmap8, 8, True), (t.enum8, 8, True), (t.uint16_t, 8, False), (t.int8s, 8, False),
    (t.Single, 8, False), (t.uint16_t, 16, True), (t.bitmap16, 16, True), (t.int16s, 16, False), ("t.uint8_t", 8, False),
])
def test_the_check_inside_home_assistant_takes_what_every_value_fits(kind, bits, fit):
    assert attribute_types.fits(kind, bits) is fit


# A class the chain may put in the place of the one the entity names is read there too.
RIVAL = ("class RivalCluster(CustomCluster):\n    \"\"\"Another cluster of an id the guard cannot tell from OnOff's.\"\"\"\n\n"
         "    cluster_id = 0x0006\n\n{defs}\n")
RIVAL_DEFS = "    class AttributeDefs(BaseAttributeDefs):\n        \"\"\"Attribute definitions.\"\"\"\n\n        {member}\n"


def rival(defs: str) -> str:
    """A scaled sensor on zigpy's OnOff on endpoint 1 while the chain also adds a class of the file with cluster id 6 there."""
    text = zcl(own("sensor", "divisor=10", cls="OnOff", name="on_time"), imports="from zigpy.zcl.clusters.general import OnOff\n")
    text = text.replace(CLASS, RIVAL.format(defs=defs) + "\n" + CLASS, 1)
    return text.replace("    .replaces(BoschWaterAlarmCluster)\n", "    .replaces(BoschWaterAlarmCluster)\n    .adds(RivalCluster)\n", 1)


def test_a_rival_class_with_the_name_of_another_type_is_refused():
    refused(problems(rival(RIVAL_DEFS.format(member="on_time = ZCLAttributeDef(id=0x0004, type=t.CharacterString)"))),
            "sensor on RivalCluster.AttributeDefs.on_time of the type t.CharacterString")
    fitting = rival(RIVAL_DEFS.format(member="on_time = ZCLAttributeDef(id=0x0004, type=t.uint16_t)"))
    assert problems(fitting) == []
    assert verdict(fitting) == ()


def test_a_rival_class_without_the_name_passes():
    assert verdict(rival(RIVAL_DEFS.format(member="other = ZCLAttributeDef(id=0x0005, type=t.CharacterString)"))) == ()
    bare = rival("")
    assert problems(bare) == []
    needs = guard.attribute_needs(bare, SHIPPED.fixed, targets(ZCL))
    assert [(n.owner, n.optional) for n in needs] == [("RivalCluster", True), ("OnOff", False)]
    assert verdict(bare) == ()


# The Tuya builder: the attribute the chain declares on its Tuya cluster.
ENERGY = ".tuya_sensor(dp_id=1, attribute_name='energy', type=t.uint32_t, divisor=100,"
MODE_Y = ("    .tuya_attribute(dp_id=60, attribute_name='mode_y', type={type})\n"
          "    .enum(attribute_name='mode_y', cluster_id=TUYA_CLUSTER_ID, enum_class=OnlineState, translation_key='mode_y', fallback_name='Mode y')\n")


def meter(old: str = "", new: str = "", extra: str = "") -> str:
    text = frozen(METER)
    if old:
        assert text.count(old) == 1, old
        text = text.replace(old, new, 1)
    return text.replace(TUYA_END, extra + TUYA_END, 1)


def test_a_scaled_tuya_sensor_reads_a_number_type():
    assert problems(meter(), METER) == []
    refused(problems(meter(ENERGY, ENERGY.replace("t.uint32_t", "t.CharacterString")), METER), "tuya_sensor on 'energy' of the type t.CharacterString")
    refused(problems(meter(ENERGY, ".tuya_sensor(dp_id=1, attribute_name='energy', divisor=100,"), METER), "tuya_sensor on 'energy'")


def test_a_tuya_scale_takes_the_scale_rule():
    refused(problems(meter(ENERGY, ENERGY.replace("divisor=100", "divisor=0")), METER), "divisor other than")


@pytest.mark.parametrize(("attribute_type", "fit"), [("OnlineState", True), ("t.enum8", True), ("t.bitmap8", True), ("t.Bool", True),
                                                     ("t.uint8_t", False), ("t.uint16_t", False), ("t.enum16", False)])
def test_an_enum_on_the_tuya_cluster_reads_an_integer_its_enum_takes(attribute_type, fit):
    """The typed Tuya cluster lets any finite number into a plain integer type (t.uint8_t), a float or one outside its range,
    never into an enum, a bitmap or a Bool."""
    found = problems(meter(extra=MODE_Y.format(type=attribute_type)), METER)
    if fit:
        assert found == []
    else:
        refused(found, f"enum on 'mode_y' of the type {attribute_type}, while ZHA computes with it")


def test_an_enum_on_the_tuya_builder_reads_a_declared_attribute_of_the_tuya_cluster():
    undeclared = MODE_Y.format(type="t.enum8").split("\n", 1)[1]
    refused(problems(meter(extra=undeclared), METER), "which the chain declares on its Tuya cluster nowhere")
    twice = MODE_Y.format(type="t.enum8") + "    .tuya_attribute(dp_id=61, attribute_name='mode_y', type=t.uint16_t)\n"
    refused(problems(meter(extra=twice), METER), "with different types")
    elsewhere = MODE_Y.format(type="t.enum8").replace("cluster_id=TUYA_CLUSTER_ID", "cluster_id=0xEF00")
    refused(problems(meter(extra=elsewhere), METER), "other than on an attribute the chain declares on its Tuya cluster")


def test_the_type_lists_match_zigpy():
    """NUMBER_TYPES, UNSIGNED_BITS and INTEGER_ONLY as zigpy has the types the guard names (TYPE_NAMES)."""
    for name in guard.TYPE_NAMES:
        kind = getattr(t, name)
        assert (name in guard.NUMBER_TYPES) is issubclass(kind, (int, float)), name
        unsigned = issubclass(kind, int) and not kind._signed
        assert guard.UNSIGNED_BITS.get(name) == (kind._bits if unsigned else None), name
        assert (name in guard.INTEGER_ONLY) is (unsigned and hasattr(kind, "__members__")), name
    assert set(guard.UNSIGNED_BITS) <= guard.NUMBER_TYPES and guard.INTEGER_ONLY <= set(guard.UNSIGNED_BITS)


def test_the_tuya_declarations_match_zhaquirks():
    """TUYA_DECLARES: the keyword of the name, its default, the keyword of the type and its default, as zhaquirks has them."""
    import inspect

    from zhaquirks.tuya.builder import TuyaQuirkBuilder

    for method, (name_keyword, name_default, type_keyword, type_default) in guard.TUYA_DECLARES.items():
        parameters = inspect.signature(getattr(TuyaQuirkBuilder, method)).parameters
        default = parameters[name_keyword].default
        assert (None if default is inspect.Parameter.empty else default) == name_default, method
        if type_keyword is not None:
            default = parameters[type_keyword].default
            assert (None if default is inspect.Parameter.empty else f"t.{default.__name__}") == type_default, method
        else:
            assert type_default == "t.Bool" and type_keyword not in parameters, method
