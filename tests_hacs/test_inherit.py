"""A take-over file that builds on a class of ZHA's own quirk (the manifest entry's inherits): the entry field, the
guard's shape for it (the imported class only as the one base of one class of the file), the check inside Home
Assistant that imports the class before the file is offered or written, and the change list line."""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

# The check looks modules up among those ZHA loaded, it imports none: these are loaded here as ZHA's start loads every
# zhaquirks module (zhaquirks.setup).
import zhaquirks.bosch.rbsh_trv0_zb_eu
import zhaquirks.centralite.cl_3460L
import zhaquirks.clusters
import zhaquirks.mli.tintE14rgbcct
import zhaquirks.nodon.switch
import zhaquirks.philips
import zhaquirks.philips.soc001
import zhaquirks.sonoff.s60zbtpf  # noqa: F401

from .conftest import Zha, issue_of, offered, quirk_text, setup_entry, write_repo
from .integration import PACKAGE, content, guard, manager
from .test_guard_files import FROZEN, SAFE_HELPER, SHIPPED, TO_TAKE_OVER, ZCL, frozen

changes = importlib.import_module(f"{PACKAGE}.changes")
inherit = importlib.import_module(f"{PACKAGE}.inherit")

MODULE, CLASS = "zhaquirks.philips.soc001", "PhilipsContactCluster"
PAIR = [MODULE, CLASS]
TAKE_OVER = {"over_zha": {"adds": [], "zha_only": []}}
# The frozen Bosch file's entry as a take-over that builds on ZHA's Philips contact cluster.
ENTRY = {**FROZEN[ZCL], **TAKE_OVER, "inherits": [PAIR]}
IMPORT = f"from {MODULE} import {CLASS}\n"
CLUSTER_IMPORT = "from zhaquirks.clusters import CustomCluster\n"
FOUNDATION = "from zigpy.zcl.foundation import BaseAttributeDefs, ZCLAttributeDef\n"
OWN = '''class PhilipsContact(PhilipsContactCluster):
    """ZHA's Philips contact cluster with an attribute more."""

    class AttributeDefs(PhilipsContactCluster.AttributeDefs):
        """Attribute definitions ZHA's class lacks."""

        sensitivity = ZCLAttributeDef(id=0x0110, type=t.uint8_t, manufacturer_code=0x100B)
'''
CALLS = (
    "    .replaces(PhilipsContact, endpoint_id=2)\n"
    "    .binary_sensor(attribute_name=PhilipsContact.AttributeDefs.tamper.name, cluster_id=PhilipsContact.cluster_id, endpoint_id=2, fallback_name='Tamper')\n"
    "    .number(attribute_name=PhilipsContact.AttributeDefs.sensitivity.name, cluster_id=PhilipsContact.cluster_id, endpoint_id=2, min_value=0, max_value=10, step=1, "
    "translation_key='sensitivity', fallback_name='Sensitivity')\n"
)
FILTER = "    .filter(no_builtin_quirk)\n"
LOGGER = "\n\n_LOGGER = "
CHAIN = "\n\n(\n    QuirkBuilder("


def inheriting(own: str = OWN, calls: str = CALLS, imports: str = IMPORT, edits: tuple[tuple[str, str], ...] = ()) -> str:
    """The frozen Bosch file as a take-over that builds on ZHA's class: the take-over filter, the import, the class own
    before _LOGGER, the calls before its filter (with the fixed helper before the chain where a converter stands in
    it), then edits (old, new)."""
    text = frozen(ZCL).replace(*TO_TAKE_OVER, 1)
    assert text.count(CLUSTER_IMPORT) == 1 and text.count(LOGGER) == 1 and text.count(FILTER) == 1 and text.count(CHAIN) == 1
    text = text.replace(CLUSTER_IMPORT, CLUSTER_IMPORT + imports, 1).replace(LOGGER, f"\n\n{own}{LOGGER}", 1).replace(FILTER, calls + FILTER, 1)
    if f"{guard.SAFE}(" in calls:
        text = text.replace(CHAIN, f"\n\n{SAFE_HELPER}\n{CHAIN}", 1)
    for old, new in edits:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text


def problems(text: str, **changes_: object) -> list[str]:
    entry = {key: value for key, value in {**ENTRY, **changes_}.items() if value is not None}
    return list(SHIPPED.problems(text.encode(), guard.Targets.of(entry)))


def refused(text: str, reason: str, **changes_: object) -> None:
    found = problems(text, **changes_)
    assert any(reason in problem for problem in found), found


# The entry field.


def test_the_entry_field_is_read_into_the_targets():
    content.check_entry("bosch", ENTRY)
    targets = guard.Targets.of(ENTRY)
    assert targets.inherits == {(MODULE, CLASS)}
    # The verdicts are cached per targets: an entry that builds on ZHA's class never shares a key with one that does not.
    assert targets.key != guard.Targets.of({**FROZEN[ZCL], **TAKE_OVER}).key
    two = [["zhaquirks.philips", "PhilipsBasicCluster"], PAIR]
    assert guard.Targets.of({**ENTRY, "inherits": two}).inherits == {tuple(pair) for pair in two}
    content.check_entry("bosch", {**ENTRY, "inherits": two})
    # An empty list says what no field says.
    assert guard.Targets.of({**ENTRY, "inherits": []}).key == guard.Targets.of({**FROZEN[ZCL], **TAKE_OVER}).key
    content.check_entry("bosch", {**FROZEN[ZCL], "inherits": []})


@pytest.mark.parametrize(
    "inherits",
    [
        pytest.param(PAIR, id="one pair, not a list of pairs"),
        pytest.param("zhaquirks.philips.soc001.PhilipsContactCluster", id="text"),
        pytest.param({MODULE: CLASS}, id="dict"),
        pytest.param([[MODULE]], id="pair of one"),
        pytest.param([[MODULE, CLASS, "x"]], id="pair of three"),
        pytest.param([(MODULE, CLASS)], id="tuple"),
        pytest.param([[MODULE, 1]], id="not text"),
        pytest.param([["zigpy.zcl.clusters.general", "OnOff"]], id="module outside zhaquirks"),
        pytest.param([["zhaquirks", "LocalDataCluster"]], id="the zhaquirks package itself"),
        pytest.param([["zhaquirksx.philips", CLASS]], id="module named like zhaquirks"),
        pytest.param([["zhaquirks.philips.1soc", CLASS]], id="module part that starts with a digit"),
        pytest.param([["zhaquirks.philips._soc001", CLASS]], id="private module part"),
        pytest.param([["zhaquirks.__init__", CLASS]], id="dunder module part"),
        pytest.param([["zhaquirks.philips-soc001", CLASS]], id="module part with a dash"),
        pytest.param([["zhaquirks.philips.", CLASS]], id="empty module part"),
        pytest.param([["zhaquirks.philips.soc001 ", CLASS]], id="module with a space"),
        pytest.param([[MODULE, "philipsContactCluster"]], id="lower case class"),
        pytest.param([[MODULE, "Philips.ContactCluster"]], id="dotted class"),
        pytest.param([[MODULE, "Exception"]], id="builtin name"),
        pytest.param([["zhaquirks.clusters", "CustomCluster"]], id="a class quirk files import anyway"),
        pytest.param([PAIR, ["zhaquirks.philips", "PhilipsBasicCluster"]], id="unsorted"),
        pytest.param([PAIR, PAIR], id="pair twice"),
    ],
)
def test_the_entry_field_in_another_shape_is_refused(inherits):
    entry = {**ENTRY, "inherits": inherits}
    assert guard.Targets.of(entry) is None
    with pytest.raises(content.BadContent, match="inherits missing or malformed"):
        content.check_entry("bosch", entry)
    # The file has no entry to compare with: refused.
    refused(inheriting(), "no manifest entry", inherits=inherits)


# zhaquirks modules with capitals in their names: their classes have code a copy cannot express.
CAPITAL_MODULES = [("zhaquirks.mli.tintE14rgbcct", "TintRGBCCTColorCluster"), ("zhaquirks.centralite.cl_3460L", "CustomPowerConfigurationCluster")]


@pytest.mark.parametrize("pair", CAPITAL_MODULES)
def test_a_module_with_capitals_in_its_name_is_read(pair):
    entry = {**ENTRY, "inherits": [list(pair)]}
    content.check_entry("bosch", entry)
    assert guard.Targets.of(entry).inherits == {pair}
    # The check inside Home Assistant takes the class as zhaquirks ships it.
    assert inherit.problems(frozenset({pair}), {pair: Inheritance("Own", {}, {})}) == ()


def test_every_module_of_the_installed_zhaquirks_fits_the_entry_field():
    """The pattern takes each module name zhaquirks has: an inheriting take-over of any of its quirks has an entry the
    integration reads (one it refused would make the whole manifest BadContent)."""
    import pkgutil

    import zhaquirks

    names = [name for _finder, name, _package in pkgutil.walk_packages(zhaquirks.__path__, "zhaquirks.")]
    assert len(names) > 400
    assert [name for name in names if not guard.INHERIT_MODULE.fullmatch(name)] == []
    assert any(not name.islower() for name in names)


def test_the_entry_field_stands_on_take_overs_only():
    entry = {key: value for key, value in ENTRY.items() if key != "over_zha"}
    assert guard.Targets.of(entry) is None
    with pytest.raises(content.BadContent, match="inherits missing or malformed"):
        content.check_entry("bosch", entry)


# The guard: what it passes.

COMMANDS = '''class PhilipsContact(PhilipsContactCluster):
    """ZHA's class with commands more."""

    class AttributeDefs(PhilipsContactCluster.AttributeDefs):
        """Attribute definitions."""

        sensitivity = ZCLAttributeDef(id=0x0110, type=t.uint8_t, manufacturer_code=0x100B)
        led = ZCLAttributeDef(id=0x0111, type=t.Bool, is_manufacturer_specific=True)

    class ServerCommandDefs(PhilipsContactCluster.ServerCommandDefs):
        """Commands to the device."""

        reset = ZCLCommandDef(id=0x10, schema={}, manufacturer_code=0x100B)

    class ClientCommandDefs(PhilipsContactCluster.ClientCommandDefs):
        """Commands from the device."""

        notify = ZCLCommandDef(id=0x11, schema={'value': t.uint8_t})
'''
ACCEPTED = {
    "a class with an attribute more": inheriting(),
    "a class with a docstring only": inheriting('class PhilipsContact(PhilipsContactCluster):\n    """ZHA\'s class as it is."""\n',
                                                CALLS.split("    .number(")[0] + "\n"),
    "a class with attributes and commands more": inheriting(COMMANDS, edits=((FOUNDATION, FOUNDATION.replace("ZCLAttributeDef", "ZCLAttributeDef, ZCLCommandDef")),)),
    # ZHA reads the name when it configures the device: Python resolves it through ZHA's class at import (before the chain
    # registers anything), so an inherited attribute certainly exists like one the file defines.
    "a ReportingConfig on an attribute ZHA's class has": inheriting(
        imports=IMPORT + "from zhaquirks.builder import ReportingConfig\n",
        calls=CALLS + "    .sensor(attribute_name=PhilipsContact.AttributeDefs.last_contact_change.name, cluster_id=PhilipsContact.cluster_id, endpoint_id=2, "
        "reporting_config=ReportingConfig(min_interval=30, max_interval=900, reportable_change=1), translation_key='changed', fallback_name='Changed')\n",
    ),
    "the cluster hidden and its entities changed by the class's id": inheriting(
        calls=CALLS + "    .prevent_default_entity_creation(endpoint_id=2, cluster_id=PhilipsContact.cluster_id)\n"
        "    .change_entity_metadata(endpoint_id=2, cluster_id=PhilipsContact.cluster_id, new_entity_registry_enabled_default=False)\n"
    ),
    "the class in place of every occurrence": inheriting(calls="    .replace_cluster_occurrences(PhilipsContact)\n"),
}


@pytest.mark.parametrize("text", ACCEPTED.values(), ids=ACCEPTED)
def test_a_file_builds_on_zhas_class_its_entry_names(text):
    assert problems(text) == []


def test_two_classes_of_zha_from_one_import():
    two = [["zhaquirks.philips", "PhilipsBasicCluster"], ["zhaquirks.philips", "PhilipsRemoteCluster"]]
    text = inheriting(
        'class Basic(PhilipsBasicCluster):\n    """ZHA\'s basic cluster."""\n\n\nclass Remote(PhilipsRemoteCluster):\n    """ZHA\'s remote cluster."""\n',
        "    .replaces(Basic, endpoint_id=2)\n    .replaces(Remote, endpoint_id=2)\n",
        "from zhaquirks.philips import PhilipsBasicCluster, PhilipsRemoteCluster\n",
    )
    assert problems(text, inherits=two) == []


# The guard: what it refuses.

PLAIN = inheriting()
SECOND_CLASS = '\n\nclass Other(PhilipsContactCluster):\n    """A second class on it."""\n'
REFUSED = {
    # The pair is not in the entry: the import is one no quirk file holds.
    "the pair not in the entry": (PLAIN, f"import of {CLASS} from {MODULE}", {"inherits": None}),
    "another class of the module than the entry names": (inheriting(imports=IMPORT.replace(CLASS, "PhilipsOther"), edits=(("(PhilipsContactCluster):", "(PhilipsOther):"), ("(PhilipsContactCluster.AttributeDefs)", "(PhilipsOther.AttributeDefs)"))), "import of PhilipsOther", {}),
    "the class from another module than the entry names": (inheriting(imports=IMPORT.replace(MODULE, "zhaquirks.philips")), f"import of {CLASS} from zhaquirks.philips", {}),
    "imported under another name": (inheriting(imports=IMPORT.replace("\n", " as Contact\n"), edits=(("(PhilipsContactCluster):", "(Contact):"), ("(PhilipsContactCluster.AttributeDefs)", "(Contact.AttributeDefs)"))), "as Contact", {}),
    # A class of the entry the file does not import: the entry says more than the file does (the change list and the
    # check inside Home Assistant read the entry).
    "a pair of the entry the file does not import": (frozen(ZCL).replace(*TO_TAKE_OVER, 1), f"builds on ZHA's class {CLASS} ({MODULE}), which the file does not import", {}),
    "imported, no class on it": (inheriting(own="", calls=""), f"ZHA's class {CLASS} imported, no class of the file builds on it", {}),
    # Uses of the name other than the one base.
    "as a value in a chain": (inheriting(calls=CALLS + f"    .adds({CLASS}, endpoint_id=2)\n"), f"ZHA's class {CLASS} other than as the one base", {}),
    "as the class a chain replaces": (inheriting(calls=CALLS.replace(".replaces(PhilipsContact,", f".replaces({CLASS},")), f"name {CLASS}", {}),
    "its cluster id in a chain": (inheriting(calls=CALLS.replace("cluster_id=PhilipsContact.cluster_id, endpoint_id=2, fallback_name='Tamper'", f"cluster_id={CLASS}.cluster_id, endpoint_id=2, fallback_name='Tamper'")), f"attribute {CLASS}.cluster_id", {}),
    "an attribute path on it": (inheriting(calls=CALLS.replace("PhilipsContact.AttributeDefs.tamper", f"{CLASS}.AttributeDefs.tamper")), f"attribute {CLASS}.AttributeDefs.tamper.name", {}),
    "in a hidden cluster": (inheriting(calls=CALLS + f"    .prevent_default_entity_creation(endpoint_id=2, cluster_id={CLASS}.cluster_id)\n"), "prevent_default_entity_creation cluster_id", {}),
    "in place of every occurrence": (inheriting(calls=f"    .replace_cluster_occurrences({CLASS})\n"), "replace_cluster_occurrences other than one cluster class the file defines", {}),
    "in a converter": (inheriting(calls=CALLS + f"    .binary_sensor(attribute_name=PhilipsContact.AttributeDefs.contact.name, cluster_id=PhilipsContact.cluster_id, endpoint_id=2, "
                                                f"attribute_converter=_none_on_error(lambda x: x == {CLASS}.cluster_id), fallback_name='Open')\n"), f"attribute {CLASS}.cluster_id", {}),
    "called in a converter": (inheriting(calls=CALLS + f"    .binary_sensor(attribute_name=PhilipsContact.AttributeDefs.contact.name, cluster_id=PhilipsContact.cluster_id, endpoint_id=2, "
                                                   f"attribute_converter=_none_on_error(lambda x: {CLASS}(x)), fallback_name='Open')\n"), f"call of {CLASS}", {}),
    "as the converter inside the helper": (inheriting(calls=CALLS + f"    .binary_sensor(attribute_name=PhilipsContact.AttributeDefs.contact.name, cluster_id=PhilipsContact.cluster_id, endpoint_id=2, "
                                                                f"attribute_converter=_none_on_error({CLASS}), fallback_name='Open')\n"), "a converter other than lambda x", {}),
    "read by a converter helper": (inheriting(edits=((LOGGER, f"\n\ndef _none_on_error(converter):\n    return {CLASS}\n{LOGGER}"),)), "FunctionDef statement", {}),
    "beside another base after it": (inheriting(edits=(("(PhilipsContactCluster):", "(PhilipsContactCluster, CustomCluster):"),)), "beside another base", {}),
    "beside another base before it": (inheriting(edits=(("(PhilipsContactCluster):", "(CustomCluster, PhilipsContactCluster):"),)), "beside another base", {}),
    "beside a zigpy cluster": (inheriting(imports=IMPORT + "from zigpy.zcl.clusters.general import OnOff\n", edits=(("(PhilipsContactCluster):", "(PhilipsContactCluster, OnOff):"),)), "beside another base", {}),
    "under two classes": (inheriting(edits=((LOGGER, SECOND_CLASS + LOGGER),)), "which class PhilipsContact builds on already", {}),
    "spread into another class's constants": (inheriting(edits=((LOGGER, '\n\nclass Other(CustomCluster):\n    """Constants."""\n\n    cluster_id = 0xFC07\n'
                                                                      f"    _CONSTANT_ATTRIBUTES = {{**{CLASS}._CONSTANT_ATTRIBUTES}}\n{LOGGER}"),)), f"attribute {CLASS}._CONSTANT_ATTRIBUTES", {}),
    "the base of another class's nested attributes": (inheriting(edits=((LOGGER, '\n\nclass Other(CustomCluster):\n    """Other."""\n\n    cluster_id = 0xFC07\n\n'
                                                                                f"    class AttributeDefs({CLASS}.AttributeDefs):\n        \"\"\"Definitions.\"\"\"\n{LOGGER}"),)), f"base class {CLASS}.AttributeDefs", {}),
    "a type in a definition": (inheriting(edits=(("type=t.uint8_t, manufacturer_code=0x100B)", f"type={CLASS}, manufacturer_code=0x100B)"),)), f"name {CLASS}", {}),
    # The class on it: a docstring and nested definitions on ZHA's own, nothing else.
    "a cluster_id in the class": (inheriting(edits=(('lacks."""\n', 'lacks."""\n\n        cluster_id = 0xFC07\n'), ('more."""\n', 'more."""\n\n    cluster_id = 0xFC07\n'))), "Assign statement in class PhilipsContact", {}),
    "an ep_attribute in the class": (inheriting(edits=(('more."""\n', "more.\"\"\"\n\n    ep_attribute = 'contact'\n"),)), "Assign statement in class PhilipsContact", {}),
    "constants in the class": (inheriting(edits=(('more."""\n', 'more."""\n\n    _CONSTANT_ATTRIBUTES = {0x0100: 1}\n'),)), "Assign statement in class PhilipsContact", {}),
    "a method in the class": (inheriting(edits=(('more."""\n', 'more."""\n\n    def _update_attribute(self, attrid, value):\n        return None\n'),)), "FunctionDef statement in class PhilipsContact", {}),
    "no docstring": (inheriting(edits=(('    """ZHA\'s Philips contact cluster with an attribute more."""\n\n', ""),)), "without a docstring", {}),
    "a decorator": (inheriting(edits=(("class PhilipsContact(", "@functools.cache\nclass PhilipsContact("),)), "with decorators", {}),
    "a metaclass": (inheriting(edits=(("(PhilipsContactCluster):", "(PhilipsContactCluster, metaclass=type):"),)), "with decorators, keywords", {}),
    "a name the fixed code binds": (inheriting(edits=(("class PhilipsContact(", "class DeviceMatch("),)), "class name DeviceMatch", {}),
    # The fixed helpers' `except Exception` would catch the file's class.
    "a builtin name": (inheriting(calls=CALLS.replace("PhilipsContact", "Exception"), edits=(("class PhilipsContact(", "class Exception("),)), "class name Exception", {}),
    "a builtin exception the Tuya helper raises": (inheriting(calls=CALLS.replace("PhilipsContact", "ValueError"), edits=(("class PhilipsContact(", "class ValueError("),)), "class name ValueError", {}),
    # Nested definitions on another base would drop ZHA's definitions, which ZHA's code reads.
    "nested attributes on zigpy's base": (inheriting(edits=(("(PhilipsContactCluster.AttributeDefs)", "(BaseAttributeDefs)"),)), "nested class AttributeDefs on other than PhilipsContactCluster.AttributeDefs", {}),
    "nested attributes on another class's": (inheriting(edits=(("(PhilipsContactCluster.AttributeDefs)", "(CustomCluster.AttributeDefs)"),)), "on other than PhilipsContactCluster.AttributeDefs", {}),
    "nested attributes on ZHA's commands": (inheriting(edits=(("(PhilipsContactCluster.AttributeDefs)", "(PhilipsContactCluster.ServerCommandDefs)"),)), "on other than PhilipsContactCluster.AttributeDefs", {}),
    "nested attributes on two bases": (inheriting(edits=(("(PhilipsContactCluster.AttributeDefs)", "(PhilipsContactCluster.AttributeDefs, BaseAttributeDefs)"),)), "on other than PhilipsContactCluster.AttributeDefs", {}),
    "nested attributes on the class's own name": (inheriting(edits=(("(PhilipsContactCluster.AttributeDefs)", "(PhilipsContact.AttributeDefs)"),)), "on other than PhilipsContactCluster.AttributeDefs", {}),
    "nested attributes with a metaclass": (inheriting(edits=(("(PhilipsContactCluster.AttributeDefs)", "(PhilipsContactCluster.AttributeDefs, metaclass=type)"),)), "nested class AttributeDefs with decorators", {}),
    "nested attributes twice": (inheriting(edits=(("manufacturer_code=0x100B)\n", "manufacturer_code=0x100B)\n\n    class AttributeDefs(PhilipsContactCluster.AttributeDefs):\n        \"\"\"Again.\"\"\"\n"),)), "nested class AttributeDefs twice", {}),
    "another nested class": (inheriting(edits=(("class AttributeDefs(PhilipsContactCluster.AttributeDefs)", "class Attributes(PhilipsContactCluster.AttributeDefs)"),)), "ClassDef statement in class PhilipsContact", {}),
    "a definition with a computed id": (inheriting(edits=(("id=0x0110", "id=PhilipsContact.AttributeDefs.contact.id"),)), "with an id other than an integer literal", {}),
    "a definition with an id out of range": (inheriting(edits=(("id=0x0110", "id=0x10000"),)), "with an id other than an integer literal from 0 to 65535", {}),
    "a definition with a negative id": (inheriting(edits=(("id=0x0110", "id=-1"),)), "with an id other than an integer literal", {}),
    "a definition without an id": (inheriting(edits=(("id=0x0110, ", ""),)), "with an id other than an integer literal", {}),
    "a definition with a positional argument": (inheriting(edits=(("ZCLAttributeDef(id=0x0110", "ZCLAttributeDef('sensitivity', id=0x0110"),)), "only name = ZCLAttributeDef(id=..., ...)", {}),
    "a command in the attributes": (inheriting(edits=(("ZCLAttributeDef(id=0x0110, type=t.uint8_t, manufacturer_code=0x100B)", "ZCLCommandDef(id=0x10, schema={})"), (FOUNDATION, FOUNDATION.replace("ZCLAttributeDef", "ZCLAttributeDef, ZCLCommandDef")))), "only name = ZCLAttributeDef(id=..., ...)", {}),
    "a definition that is no call": (inheriting(edits=(("ZCLAttributeDef(id=0x0110, type=t.uint8_t, manufacturer_code=0x100B)", "0x0110"),)), "only name = ZCLAttributeDef(id=..., ...)", {}),
    "a private definition name": (inheriting(edits=(("sensitivity = ZCLAttributeDef", "_sensitivity = ZCLAttributeDef"),), calls=CALLS.split("    .number(")[0] + "\n"), "only name = ZCLAttributeDef(id=..., ...)", {}),
    "a command id out of range": (inheriting(COMMANDS.replace("id=0x10,", "id=0x100,"), edits=((FOUNDATION, FOUNDATION.replace("ZCLAttributeDef", "ZCLAttributeDef, ZCLCommandDef")),)), "with an id other than an integer literal from 0 to 255", {}),
    "a class on the file's class": (inheriting(edits=((LOGGER, '\n\nclass Sub(PhilipsContact):\n    """On the file\'s class."""\n' + LOGGER),)), "base class PhilipsContact", {}),
    "the class after the chain": (inheriting(own="", edits=(("    .add_to_registry()\n)\n", "    .add_to_registry()\n)\n\n\n" + OWN),)), "ClassDef statement after the first builder chain", {}),
    # The guard does not know the class's cluster id (ZHA's class has it): another class on the endpoint may be in its place.
    "a ReportingConfig beside a class of unknown rival id": (inheriting(
        imports=IMPORT + "from zhaquirks.builder import ReportingConfig\n",
        calls=CALLS + "    .adds(BoschWaterAlarmCluster, endpoint_id=2)\n    .sensor(attribute_name=PhilipsContact.AttributeDefs.contact.name, cluster_id=PhilipsContact.cluster_id, endpoint_id=2, "
        "reporting_config=ReportingConfig(min_interval=30, max_interval=900, reportable_change=1), translation_key='open', fallback_name='Open')\n",
    ), "while the chain may put BoschWaterAlarmCluster in its place on endpoint 2", {}),
    "code in a definition": (inheriting(edits=(("manufacturer_code=0x100B)", "manufacturer_code=__import__('os').getpid())"),)), "call of __import__", {}),
}


@pytest.mark.parametrize(("text", "reason", "changes_"), REFUSED.values(), ids=REFUSED)
def test_zhas_class_stands_only_as_the_one_base_of_one_class(text, reason, changes_):
    refused(text, reason, **changes_)


def test_the_facts_of_a_file_for_the_check_inside_home_assistant():
    text = inheriting(COMMANDS, CALLS + "    .command_button(command_name=PhilipsContact.ServerCommandDefs.reset.name, cluster_id=PhilipsContact.cluster_id, endpoint_id=2, fallback_name='Reset')\n",
                      edits=((FOUNDATION, FOUNDATION.replace("ZCLAttributeDef", "ZCLAttributeDef, ZCLCommandDef")),))
    assert problems(text) == []
    found = guard.inheritances(text.encode(), frozenset({(MODULE, CLASS)}))
    assert set(found) == {(MODULE, CLASS)}
    use = found[(MODULE, CLASS)]
    assert use.own == "PhilipsContact"
    assert use.defines == {"AttributeDefs": {"sensitivity": 0x0110, "led": 0x0111}, "ServerCommandDefs": {"reset": 0x10}, "ClientCommandDefs": {"notify": 0x11}}
    # What it reads through its class without defining it: from ZHA's class.
    assert use.reads == {"AttributeDefs": {"tamper"}, "ServerCommandDefs": frozenset(), "ClientCommandDefs": frozenset()}


# The check inside Home Assistant (inherit.py): imports each class and compares it with the file's.

Inheritance = guard.Inheritance
USE = Inheritance("PhilipsContact", {"AttributeDefs": {"sensitivity": 0x0110}}, {"AttributeDefs": frozenset({"tamper", "contact"})})


def check(pair: tuple[str, str] = (MODULE, CLASS), use: object = USE) -> tuple[str, ...]:
    return inherit.problems(frozenset({pair}), {pair: use} if use is not None else {})


def test_zhas_class_as_zhaquirks_ships_it_passes():
    assert check() == ()
    # The file's own sample: the guard's facts of the shape and the check together.
    targets = guard.Targets.of(ENTRY)
    assert manager.verdict_of(SHIPPED, inheriting().encode(), targets) == ()


@pytest.mark.parametrize(
    ("module", "name", "use"),
    [
        ("zhaquirks.sonoff.s60zbtpf", "SonoffEwelinkCluster", Inheritance("Ewelink", {"AttributeDefs": {"xq_extra": 0x7777}}, {"AttributeDefs": frozenset()})),
        ("zhaquirks.bosch.rbsh_trv0_zb_eu", "BoschThermostatCluster", Inheritance("Thermostat", {}, {"AttributeDefs": frozenset({"local_temperature"})})),
        ("zhaquirks.philips", "PhilipsRemoteCluster", Inheritance("Remote", {"ClientCommandDefs": {"xq_note": 0x7F}}, {})),
    ],
)
def test_other_classes_of_zhaquirks_with_behaviour_code_pass(module, name, use):
    assert check((module, name), use) == ()


def fake_module(monkeypatch, name: str = "zhaquirks.xq_fake", file: str | None = None, **members: object) -> str:
    """A module in sys.modules (import_module returns it) under name, its file in zhaquirks' folder unless file says otherwise."""
    import zhaquirks

    module = types.ModuleType(name)
    module.__file__ = file or str(Path(zhaquirks.__file__).resolve().parent / "xq_fake.py")
    for key, value in members.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return name


def cluster_class(name: str = "FakeCluster", module: str = "zhaquirks.xq_fake", **body: object) -> type:
    """A subclass of zhaquirks' CustomCluster, without a cluster id unless body gives one (zigpy registers none then)."""
    from zhaquirks.clusters import CustomCluster

    return type(name, (CustomCluster,), {"__module__": module, **body})


def test_a_module_zha_has_not_loaded_gives_no_verdict_and_is_never_imported(monkeypatch):
    """Importing a zhaquirks module registers its quirks: before ZHA's start loaded them, or with ZHA's quirks switched
    off, the check would register them itself. No verdict instead, the check runs again."""
    monkeypatch.delitem(sys.modules, MODULE)
    with pytest.raises(inherit.NotLoaded, match=f"ZHA has not loaded {MODULE} yet"):
        check()
    assert MODULE not in sys.modules
    with pytest.raises(inherit.NotLoaded):
        check(("zhaquirks.xq_no_such_module", "FakeCluster"))
    assert "zhaquirks.xq_no_such_module" not in sys.modules
    # zhaquirks itself too: the check reads its CustomCluster from the loaded package.
    monkeypatch.delitem(sys.modules, "zhaquirks.clusters")
    with pytest.raises(inherit.NotLoaded, match="zhaquirks.clusters"):
        check(("zhaquirks.philips", "PhilipsRemoteCluster"), Inheritance("Remote", {}, {}))


def test_a_module_zha_is_loading_gives_no_verdict(monkeypatch):
    """A partly run module lacks names it will have: no verdict that says it has no such class."""
    name = fake_module(monkeypatch)
    sys.modules[name].__spec__ = types.SimpleNamespace(_initializing=True)
    with pytest.raises(inherit.NotLoaded):
        check((name, "FakeCluster"))


def test_a_name_outside_zhaquirks_is_never_looked_up():
    assert check(("os", "Path")) == ("ZHA's class Path (os): no zhaquirks module and class name",)
    assert check(("zhaquirks.philips.soc001", "lower")) == ("ZHA's class lower (zhaquirks.philips.soc001): no zhaquirks module and class name",)


def test_a_module_outside_the_zhaquirks_folder_refuses_the_file(monkeypatch):
    name = fake_module(monkeypatch, file="/tmp/elsewhere/xq_fake.py", FakeCluster=cluster_class(cluster_id=0xFC77, _skip_registry=True))
    assert check((name, "FakeCluster")) == (f"ZHA's class FakeCluster ({name}): no module of the installed zhaquirks",)


def test_a_missing_class_refuses_the_file():
    assert check((MODULE, "NoSuchCluster")) == (f"ZHA's class NoSuchCluster ({MODULE}): {MODULE} has no NoSuchCluster",)


def test_a_name_only_a_module_getattr_has_refuses_the_file(monkeypatch):
    calls = []
    name = fake_module(monkeypatch, __getattr__=lambda attr: calls.append(attr) or cluster_class())
    assert check((name, "FakeCluster")) == (f"ZHA's class FakeCluster ({name}): {name} has no FakeCluster",)
    assert calls == []


def test_something_other_than_a_class_refuses_the_file():
    # The module's SIGNIFY is a text.
    assert check((MODULE, "SIGNIFY")) == (f"ZHA's class SIGNIFY ({MODULE}): not a class",)


def test_a_class_other_than_a_custom_cluster_refuses_the_file():
    # zigpy's OnOff, which the module imports: zigpy registers it, a class on it would take its place for every device.
    assert check((MODULE, "OnOff")) == (f"ZHA's class OnOff ({MODULE}): no zhaquirks cluster class (CustomCluster)",)


def test_a_class_zigpy_would_register_refuses_the_file(monkeypatch):
    # No cluster id: zigpy registers nothing for it here.
    name = fake_module(monkeypatch, FakeCluster=cluster_class(_skip_registry=False))
    assert check((name, "FakeCluster")) == (
        f"ZHA's class FakeCluster ({name}): not kept out of zigpy's registry (_skip_registry): zigpy would build every device's cluster of its id from the file's class",
    )
    truthy = fake_module(monkeypatch, FakeCluster=cluster_class(_skip_registry=1))
    assert "not kept out of zigpy's registry" in check((truthy, "FakeCluster"))[0]


def test_a_class_defined_outside_zhaquirks_refuses_the_file(monkeypatch):
    name = fake_module(monkeypatch, FakeCluster=cluster_class(module="custom_quirk", cluster_id=0xFC77))
    assert check((name, "FakeCluster")) == (f"ZHA's class FakeCluster ({name}): not defined in zhaquirks",)


def test_a_class_without_a_cluster_id_refuses_the_file(monkeypatch):
    from zhaquirks.clusters import CustomCluster

    assert check(("zhaquirks.clusters", "CustomCluster")) == ("ZHA's class CustomCluster (zhaquirks.clusters): no cluster id",)
    assert CustomCluster.cluster_id is None
    # Out of range, set after zigpy read the class (zigpy turns the id of a class it reads into a 16 bit integer).
    cls = cluster_class()
    cls.cluster_id = 0x10000
    name = fake_module(monkeypatch, FakeCluster=cls)
    assert check((name, "FakeCluster")) == (f"ZHA's class FakeCluster ({name}): no cluster id",)


@pytest.mark.parametrize(
    ("use", "reason"),
    [
        (Inheritance("PhilipsContact", {"AttributeDefs": {"contact": 0x0110}}, {}), "the file's PhilipsContact.AttributeDefs defines contact, which ZHA's class has"),
        (Inheritance("PhilipsContact", {"AttributeDefs": {"open": 0x0100}}, {}), "the file's PhilipsContact.AttributeDefs defines open with the id 0x0100 of ZHA's contact"),
        (Inheritance("PhilipsContact", {}, {"AttributeDefs": frozenset({"no_such"})}), "the file reads PhilipsContact.AttributeDefs.no_such, which neither its class nor ZHA's defines"),
        (Inheritance("PhilipsContact", {}, {"ServerCommandDefs": frozenset({"reset"})}), "the file reads PhilipsContact.ServerCommandDefs.reset, which neither its class nor ZHA's defines"),
        (None, "no class of the file builds on it"),
    ],
)
def test_the_files_class_adds_only_what_zhas_class_lacks_and_reads_what_it_has(use, reason):
    assert check(use=use) == (f"ZHA's class {CLASS} ({MODULE}): {reason}",)


def test_the_commands_of_zhas_class_count():
    # ZHA's remote cluster sends notification (id 0) to the client.
    use = Inheritance("Remote", {"ClientCommandDefs": {"notification": 0x01, "other": 0x00}, "ServerCommandDefs": {"notify": 0x00}}, {})
    assert check(("zhaquirks.philips", "PhilipsRemoteCluster"), use) == (
        "ZHA's class PhilipsRemoteCluster (zhaquirks.philips): the file's Remote.ClientCommandDefs defines notification, which ZHA's class has",
        "ZHA's class PhilipsRemoteCluster (zhaquirks.philips): the file's Remote.ClientCommandDefs defines other with the id 0x0000 of ZHA's notification",
    )


def test_a_command_name_counts_in_both_command_classes():
    """zigpy finds cluster.<name> in ClientCommandDefs, then in ServerCommandDefs: a client command of the file named like
    a server command of ZHA's class would change what ZHA's light and switch entities send with cluster.on()."""
    nodon = ("zhaquirks.nodon.switch", "OnOffSIN4_1_20")
    assert check(nodon, Inheritance("Switch", {"ClientCommandDefs": {"on": 0x40}}, {})) == (
        "ZHA's class OnOffSIN4_1_20 (zhaquirks.nodon.switch): the file's Switch.ClientCommandDefs defines on, a command of ZHA's class in its other command class (zigpy finds both by the name)",
    )
    # ZHA's client command named in the file's server commands, the other way round.
    remote = ("zhaquirks.philips", "PhilipsRemoteCluster")
    assert check(remote, Inheritance("Remote", {"ServerCommandDefs": {"notification": 0x40}}, {})) == (
        "ZHA's class PhilipsRemoteCluster (zhaquirks.philips): the file's Remote.ServerCommandDefs defines notification, a command of ZHA's class in its other command class (zigpy finds both by the name)",
    )
    # The file's own two command classes share no name.
    assert check(remote, Inheritance("Remote", {"ServerCommandDefs": {"xq_note": 0x40}, "ClientCommandDefs": {"xq_note": 0x41}}, {})) == (
        "ZHA's class PhilipsRemoteCluster (zhaquirks.philips): the file's Remote defines the command xq_note in both ServerCommandDefs and ClientCommandDefs",
    )
    # New names in both directions, an id ZHA uses in the other direction: commands of another direction.
    assert check(nodon, Inheritance("Switch", {"ClientCommandDefs": {"xq_report": 0x00}, "ServerCommandDefs": {"xq_set": 0x50}}, {})) == ()


def test_a_check_that_fails_refuses_the_file(monkeypatch):
    def broken(*args):
        raise TypeError("odd class")

    monkeypatch.setattr(inherit, "_reasons", broken)
    assert check() == (f"ZHA's class {CLASS} ({MODULE}): cannot be checked (TypeError: odd class)",)


def test_the_check_reads_the_nested_classes_the_guard_allows():
    assert set(inherit.INHERIT_NESTED) == {"AttributeDefs", "ServerCommandDefs", "ClientCommandDefs"} == set(guard.INHERIT_NESTED)


def test_the_guard_verdict_comes_first():
    """A file the guard refuses is never imported against: its reasons are the verdict."""
    targets = guard.Targets.of({**ENTRY, "inherits": [[MODULE, "NoSuchCluster"]]})
    text = inheriting(imports=IMPORT.replace(CLASS, "NoSuchCluster"), edits=(("(PhilipsContactCluster):", "(NoSuchCluster):"), ("(PhilipsContactCluster.AttributeDefs)", "(NoSuchCluster.AttributeDefs)")))
    assert manager.verdict_of(SHIPPED, text.encode(), targets) == (f"ZHA's class NoSuchCluster ({MODULE}): {MODULE} has no NoSuchCluster",)
    found = manager.verdict_of(SHIPPED, (text + "\nimport os\n").encode(), targets)
    assert found and not any(reason.startswith("ZHA's class") for reason in found)
    # Without inherits the check is not run at all.
    assert manager.verdict_of(SHIPPED, frozen(ZCL).encode(), guard.Targets.of(FROZEN[ZCL])) == ()


# Through the integration: the check refuses a file the guard passes, before the list and before a write.

FILE = "z2m_philips_contact.py"
TAKE = {
    "file": f"quirks/{FILE}",
    "fingerprints": [[None, "SOC001"], ["Signify Netherlands B.V.", "SOC001"]],
    "claimed_elsewhere": [],
    "vendor": "Philips",
    "model": "SOC001",
    "status": "generated",
    "path": "zcl",
    "risk": "low",
    "risk_why": [],
    "over_zha": {"adds": ["Sensitivity"], "zha_only": []},
}
IEEE = "00:11:22:33:44:55:66:1a"


def builds_on(repo_dir: Path, name: str) -> bytes:
    """The take-over file of the test repository, with a class on ZHA's class name of the Philips quirk module."""
    data = quirk_text("philips_contact", 1, TAKE) + f'\nfrom {MODULE} import {name}\n\n\nclass Contact({name}):\n    """ZHA\'s class."""\n'.encode()
    (repo_dir / "quirks" / FILE).write_bytes(data)
    return data


async def test_a_file_on_a_class_zhaquirks_has_is_offered(hass: HomeAssistant, zha: Zha, repo_dir: Path, quirks_dir: Path):
    write_repo(repo_dir, {"philips_contact": {**TAKE, "inherits": [PAIR]}})
    builds_on(repo_dir, CLASS)
    zha.add_device(IEEE, "Signify Netherlands B.V.", "SOC001", "Door")
    await setup_entry(hass, repo_dir)
    assert offered(hass) == [FILE]


async def test_a_file_on_a_class_zhaquirks_lacks_is_refused(hass: HomeAssistant, zha: Zha, repo_dir: Path, quirks_dir: Path):
    write_repo(repo_dir, {"philips_contact": {**TAKE, "inherits": [[MODULE, "NoSuchCluster"]]}})
    data = builds_on(repo_dir, "NoSuchCluster")
    zha.add_device(IEEE, "Signify Netherlands B.V.", "SOC001", "Door")
    entry = await setup_entry(hass, repo_dir)
    # The guard passes the file (it cannot see the class), the check inside Home Assistant refuses it like the guard would.
    targets = guard.Targets.of({**TAKE, "inherits": [[MODULE, "NoSuchCluster"]]})
    assert SHIPPED.problems(data, targets) == ()
    assert offered(hass) == [] and list(quirks_dir.iterdir()) == []
    assert issue_of(hass, "refused", FILE)
    assert entry.runtime_data.summary()["refused_by_the_guard"][FILE]["problems"] == [f"ZHA's class NoSuchCluster ({MODULE}): {MODULE} has no NoSuchCluster"]
    # And again before a write.
    with pytest.raises(manager.GuardRefused):
        await entry.runtime_data._refuse(str(quirks_dir / FILE), data)
    assert list(quirks_dir.iterdir()) == []


async def test_a_file_on_a_module_zha_has_not_loaded_waits(hass: HomeAssistant, zha: Zha, repo_dir: Path, quirks_dir: Path, monkeypatch):
    """No verdict while ZHA has not loaded the module: the file waits for the check and is not written."""
    write_repo(repo_dir, {"philips_contact": {**TAKE, "inherits": [PAIR]}})
    data = builds_on(repo_dir, CLASS)
    zha.add_device(IEEE, "Signify Netherlands B.V.", "SOC001", "Door")
    monkeypatch.delitem(sys.modules, MODULE)
    entry = await setup_entry(hass, repo_dir)
    assert offered(hass) == [] and list(quirks_dir.iterdir()) == []
    summary = entry.runtime_data.summary()
    assert summary["waiting_for_the_check"] == {FILE: f"ZHA has not loaded {MODULE} yet"}
    assert FILE not in summary["refused_by_the_guard"]
    with pytest.raises(manager.GuardRefused):
        await entry.runtime_data._refuse(str(quirks_dir / FILE), data)
    assert MODULE not in sys.modules and list(quirks_dir.iterdir()) == []


# The change list.

OLD = frozen(ZCL).replace(*TO_TAKE_OVER, 1).encode()


def test_the_change_list_says_which_class_of_zha_a_file_builds_on():
    new = inheriting().encode()
    summary = changes.summarize(OLD, new)
    assert summary.settings_added == [f"builds on ZHA's class {CLASS} ({MODULE})"]
    assert f"Builds on ZHA's class {CLASS} ({MODULE})" in summary.items()
    back = changes.summarize(new, OLD)
    assert back.settings_removed == [f"builds on ZHA's class {CLASS} ({MODULE})"]
    assert f"No longer builds on ZHA's class {CLASS} ({MODULE})" in back.items()
    # The same class in both versions: nothing to say about it.
    assert changes.summarize(new, new.replace(b"Sensitivity", b"Sensitivity level")).settings_added == []


def test_one_line_per_class_of_zha():
    two = inheriting(
        'class Basic(PhilipsBasicCluster):\n    """ZHA\'s basic cluster."""\n\n\nclass Remote(PhilipsRemoteCluster):\n    """ZHA\'s remote cluster."""\n',
        "    .replaces(Basic, endpoint_id=2)\n    .replaces(Remote, endpoint_id=2)\n",
        "from zhaquirks.philips import PhilipsBasicCluster, PhilipsRemoteCluster\n",
    )
    assert changes.summarize(OLD, two.encode()).settings_added == [
        "builds on ZHA's class PhilipsBasicCluster (zhaquirks.philips)", "builds on ZHA's class PhilipsRemoteCluster (zhaquirks.philips)",
    ]


def test_plain_bases_are_no_class_of_zha():
    # The Bosch file's class on CustomCluster, an Aqara file's on XiaomiAqaraE1Cluster: none builds on ZHA's own.
    assert changes.inventory(OLD).settings == ()
    assert changes.inventory(frozen("z2m_aqara_qbcz15lm.py").encode()).settings == ()
    assert changes.ZHAQUIRKS_BASES == guard.SKIP_REGISTRY
