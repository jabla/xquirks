"""The trial build's attribute check: each entity reads an attribute its cluster has, of a type that holds every value
ZHA computes with while it loads its devices. At every start ZHA raises there for a missing attribute (a sensor shows no
value) or a value its divisor, multiplier or enum class cannot take.
"""

import pytest

import loader
from helpers import shipped
from test_take_over import trial

WB_MSW = "wiren_board_wb_msw_zigbee_v_4_official"


def test_an_enum_narrower_than_a_standard_attribute_fails(registry, tmp_path):
    """The IR action of the WB-MSW looks GenMultistateOutputCluster's present_value (zigpy's uint16_t of
    MultistateOutput) up in its enum IrAction: an enum8 there would raise for a value from 256 on."""
    definition = shipped(WB_MSW)
    (ir_action,) = [spec for spec in definition["classes"] if spec["name"] == "IrAction"]
    assert ir_action["base"] == {"ref": "t.enum16"}
    assert trial(tmp_path, definition) == {}
    ir_action["base"] = {"ref": "t.enum8"}
    (reason,) = trial(tmp_path, definition).values()
    assert "GenMultistateOutputCluster.AttributeDefs.present_value of the type uint16_t" in reason and "at most 8 bits" in reason


def typed(kind: str, entity: list, enum_base: str = "t.enum8") -> dict:
    """A QuirkBuilder definition whose cluster has the attribute "value" of this type, read by the entity."""
    return {
        "id": "typed",
        "endpoints": {"1": [0xFC00]},
        "classes": [
            {"kind": "enum", "name": "Mode", "base": {"ref": enum_base}, "members": [["off", 0], ["on", 1]]},
            {
                "kind": "cluster", "name": "Vendor", "bases": [{"ref": "CustomCluster"}], "data": {"cluster_id": 0xFC00, "ep_attribute": "vendor"},
                "attribute_defs": {"base": {"ref": "BaseAttributeDefs"}, "members": [["value", {"id": 0, "type": {"ref": kind}}]]},
            },
        ],
        "chain": [["QuirkBuilder", ["XQ", "Typed"]], ["replaces", [{"ref": "Vendor"}]], entity, ["add_to_registry"]],
    }


def reading(method: str, attribute: str = "value", **keywords) -> list:
    """The entity call: method reads the attribute of the definition's cluster."""
    return [method, {"attribute_name": attribute, "cluster_id": 0xFC00, **keywords, "translation_key": "value", "fallback_name": "Value"}]


ENUM = reading("enum", enum_class={"ref": "Mode"})
TEXT = {"ops": [["str"]], "on_error": "none"}
NUMBER_TYPE = "needs a number type"
UNSIGNED = "needs an unsigned integer type of at most 8 bits"
MISSING = "reads Vendor.AttributeDefs.other, which the cluster lacks"
CASES = {
    # name: (attribute type, entity, part of the reason or None)
    "enum8 on uint8_t": ("t.uint8_t", ENUM, None),
    "enum8 on an enum8": ("t.enum8", ENUM, None),
    "enum8 on Bool": ("t.Bool", ENUM, None),
    "enum8 on a bitmap8": ("t.bitmap8", ENUM, None),
    "enum8 on uint16_t": ("t.uint16_t", ENUM, UNSIGNED),
    "enum8 on int8s": ("t.int8s", ENUM, UNSIGNED),
    "enum8 on text": ("t.CharacterString", ENUM, UNSIGNED),
    "a divisor on text": ("t.CharacterString", reading("sensor", divisor=10), NUMBER_TYPE),
    "a multiplier on bytes": ("t.LVBytes", reading("sensor", multiplier=2), NUMBER_TYPE),
    "text as it is": ("t.CharacterString", reading("sensor"), None),
    "a divisor of 1 on text": ("t.CharacterString", reading("sensor", divisor=1), None),  # ZHA leaves 1 out
    "a divisor on a float": ("t.Single", reading("sensor", divisor=10), None),
    "a converter's divisor and multiplier on text": ("t.CharacterString", reading("sensor", divisor=10, multiplier=2, attribute_converter=TEXT), None),
    "a number's multiplier on text": ("t.CharacterString", reading("number", multiplier=0.1), NUMBER_TYPE),
    "a number's multiplier on int16s": ("t.int16s", reading("number", multiplier=0.1), None),
    "a number's multiplier of 1.0 on text": ("t.CharacterString", reading("number", multiplier=1.0), NUMBER_TYPE),
    "a number on text": ("t.CharacterString", reading("number"), NUMBER_TYPE),  # ZHA multiplies by 1
    "a number on uint8_t": ("t.uint8_t", reading("number"), None),
    "an enum on a missing attribute": ("t.uint8_t", reading("enum", "other", enum_class={"ref": "Mode"}), MISSING),
    "a switch on a missing attribute": ("t.uint8_t", reading("switch", "other"), MISSING),
    "a binary_sensor on a missing attribute": ("t.uint8_t", reading("binary_sensor", "other"), MISSING),
    "a number on a missing attribute": ("t.uint8_t", reading("number", "other"), MISSING),
    "a switch's missing invert attribute": ("t.uint8_t", reading("switch", invert_attribute_name="other"), MISSING),
    "a switch's invert attribute": ("t.uint8_t", reading("switch", invert_attribute_name="value"), None),
    "an enum class of no zigpy type": ("t.uint8_t", reading("enum", enum_class={"ref": "EntityType"}), "looks its value up in EntityType, which is no zigpy enum or bitmap"),
}


@pytest.mark.parametrize("case", CASES)
def test_each_value_zha_computes_with_has_a_type_that_holds_it(registry, tmp_path, case):
    kind, entity, reason = CASES[case]
    failures = trial(tmp_path, typed(kind, entity))
    assert (failures == {}) if reason is None else (reason in failures.get("typed", "")), failures


def test_an_enum16_holds_a_uint16(registry, tmp_path):
    assert trial(tmp_path, typed("t.uint16_t", ENUM, "t.enum16")) == {}


def test_a_cluster_the_device_does_not_get_makes_no_entity(registry, tmp_path):
    """ZHA makes no entity on a cluster the device lacks after the transforms: nothing to check."""
    definition = typed("t.CharacterString", reading("sensor", divisor=10))
    definition["chain"][1:2] = [["removes", [0xFC00]]]
    assert trial(tmp_path, definition) == {}


def tuya(declared: str, entity: list) -> dict:
    """A Tuya definition whose datapoint 2 goes to the attribute "level" of this type, read by the entity."""
    return {
        "id": "typed_tuya",
        "endpoints": {"1": [0xEF00]},
        "classes": [{"kind": "enum", "name": "Mode", "base": {"ref": "t.enum8"}, "members": [["off", 0], ["on", 1]]}],
        "chain": [
            ["TuyaQuirkBuilder"], ["applies_to", ["_TZE200_xqtyped", "TS0601"]],
            ["tuya_dp_attribute", {"dp_id": 2, "attribute_name": "level", "type": {"ref": declared}}],
            entity,
            ["add_to_registry", {"replacement_cluster": {"ref": "TypedTuyaCluster"}}],
        ],
    }


TUYA_ENUM = ["enum", {"attribute_name": "level", "cluster_id": {"ref": "TUYA_CLUSTER_ID"}, "enum_class": {"ref": "Mode"}, "translation_key": "level", "fallback_name": "Level"}]


TUYA_TYPE = "needs an enum or Bool type (no bitmap) of at most 8 bits"


@pytest.mark.parametrize("declared,reason", [("Mode", None), ("t.Bool", None), ("t.uint8_t", TUYA_TYPE), ("t.bitmap8", TUYA_TYPE)])
def test_an_enum_entity_of_a_tuya_device_reads_an_enum_or_bool(registry, tmp_path, declared, reason):
    """The typed Tuya cluster lets a float into a plain integer type, reads its own plain integer attributes signed and
    lets a negative value into a bitmap (zigpy's bitmaps wrap it instead of raising): no enum takes 1.5 or -1."""
    failures = trial(tmp_path, tuya(declared, TUYA_ENUM))
    assert (failures == {}) if reason is None else (reason in failures.get("typed_tuya", "")), failures


def test_an_entity_on_an_attribute_the_tuya_cluster_lacks_fails(registry, tmp_path):
    """The check covers every kind of cluster: zhaquirks' local clusters return None for a name they lack."""
    switch = ["switch", {"attribute_name": "other", "cluster_id": {"ref": "TUYA_CLUSTER_ID"}, "translation_key": "other", "fallback_name": "Other"}]
    (reason,) = trial(tmp_path, tuya("Mode", switch)).values()
    assert reason.startswith("the switch other on endpoint 1 reads ") and reason.endswith(".AttributeDefs.other, which the cluster lacks"), reason


def test_the_reason_names_the_entity(registry, tmp_path):
    assert trial(tmp_path, typed("t.uint16_t", ENUM)) == {
        "typed": "the select value on endpoint 1 reads Vendor.AttributeDefs.value of the type uint16_t: ZHA computes with it while it loads its "
        "devices and needs an unsigned integer type of at most 8 bits",
    }


def test_a_type_check_that_cannot_run_fails_its_definition_only(registry, tmp_path, monkeypatch):
    resolved = loader._resolved

    def failing(definition, entry, entities):
        if definition["id"] == "typed":
            raise RuntimeError("no device")
        return resolved(definition, entry, entities)

    monkeypatch.setattr(loader, "_resolved", failing)
    assert trial(tmp_path, typed("t.uint8_t", ENUM), {**typed("t.uint16_t", ENUM), "id": "typed_wide"}) == {
        "typed": "the attributes its entities read cannot be checked: RuntimeError: no device",
        "typed_wide": "the select value on endpoint 1 reads Vendor.AttributeDefs.value of the type uint16_t: ZHA computes with it while it "
        "loads its devices and needs an unsigned integer type of at most 8 bits",
    }
