"""The take-over rule: a take-over stays over ZHA's own quirk only while ZHA's entry is one it was built over.

The generator records the digest of ZHA's entries for the definition's exact names (zha_digests), the loader compares
the digest of the entry ZHA has now (loader.zha_digest). A newer ZHA quirk is simulated with a changed copy of ZHA's
own file, run under a zhaquirks module name: its entries carry the source of ZHA's file and replace ZHA's entry.
"""

import dataclasses
import importlib.util
import itertools
import json
import logging
import os
import pathlib
import subprocess
import sys

import pytest
import zhaquirks
from zha.quirks import DeviceMatch

import loader
from helpers import BY_ID, SAMPLE_TEXT, STUB_FILE, build_new, definition_of, label_of, raw_of

SHIPPED = pathlib.Path(zhaquirks.__file__).resolve().parent
LABELS = {"sonoff_zbminir2": "(SONOFF / ZBMINIR2)", "sonoff_swv": "(SONOFF / SWV)", "shelly_sbdw_103c": "(Shelly / BLU DoorWindow ZB)"}
NAMES = {"sonoff_zbminir2": ("SONOFF", "ZBMINIR2"), "sonoff_swv": ("SONOFF", "SWV"), "shelly_sbdw_103c": ("Shelly", "BLU DoorWindow ZB")}
_copies = itertools.count()


def device(ident):
    return raw_of(ident, *NAMES[ident])


def zhas(entry) -> bool:
    return isinstance(entry.device_match, DeviceMatch) and loader._is_shipped(getattr(entry.source, "file", None))


def zhas_entry(registry, label):
    (entry,) = [e for e in registry if zhas(e) and e.source.label == label]
    return entry


def entities(entry) -> set:
    return {(m.entity_platform, m.endpoint_id, m.resolved_unique_id_suffix) for m in entry.zha_device_factory.quirk_definition.entity_metadata}


def run_copy(tmp_path, registry, rel, replacements=(), extra="", imports=None):
    """ZHA's file rel (under zhaquirks/) run as a copy: each (old, new) replaced (each old once in the file), extra
    appended, with imports in sys.modules meanwhile. (module, the entries it registers with the source of ZHA's file)."""
    path = SHIPPED / rel
    text = path.read_text()
    for old, new in replacements:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    number = next(_copies)
    copy = tmp_path / f"zha_copy_{number}.py"
    copy.write_text(text + extra)
    name = f"zhaquirks.{rel.removesuffix('.py').replace('/', '.')}_copy_{number}"
    saved = {key: sys.modules.get(key) for key in imports or {}}
    with registry.preserve_state():
        before = {id(entry) for entry in registry}
        spec = importlib.util.spec_from_file_location(name, copy)
        module = importlib.util.module_from_spec(spec)
        sys.modules.update({name: module, **(imports or {})})
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(name)
            for key, value in saved.items():
                if value is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = value
        entries = [entry for entry in registry if id(entry) not in before]
    return module, [dataclasses.replace(e, source=dataclasses.replace(e.source, file=str(path))) for e in entries]


def swap(registry, label, newer) -> None:
    """ZHA's entry with this label replaced by a newer one, as a newer zha-quirks would register it."""
    registry.remove(zhas_entry(registry, label))
    registry.register(newer)


def takes_over(registry, ident) -> bool:
    return registry.match_entry(device(ident)).source.label == label_of(BY_ID[ident])


def test_the_definition_lists_zhas_entry_it_was_built_over(registry):
    for ident, label in LABELS.items():
        assert BY_ID[ident]["take_over"] is True
        assert BY_ID[ident]["zha_digests"] == [loader.zha_digest(zhas_entry(registry, label))]


@pytest.mark.parametrize("ident", sorted(LABELS))
def test_an_unchanged_zha_quirk_keeps_the_take_over(installation, registry, ident):
    assert zhas_entry(registry, LABELS[ident]).device_match.matches(device(ident))
    installation.options(high_risk=True)
    installation.setup()
    assert takes_over(registry, ident)


OTHER_QUIRK = '''

(
    QuirkBuilder("SONOFF", "XQ-OTHER")
    .replaces(SonoffCluster)
    .switch(SonoffCluster.AttributeDefs.network_led.name, SonoffCluster.cluster_id, translation_key="network_led", fallback_name="LED")
    .add_to_registry()
)
'''


def test_an_edit_elsewhere_in_zhas_file_keeps_the_take_over(installation, registry, tmp_path):
    """A docstring, a comment and another device's quirk in ZHA's file: its entry for the device stays the same."""
    replace = [('"""Sonoff ZBMINIR2 and MINI-ZBD - Zigbee Switches."""', '"""Sonoff switches."""\n\n# One more device below.')]
    _, entries = run_copy(tmp_path, registry, "sonoff/zbminir2.py", replace, extra=OTHER_QUIRK)
    (same,) = [e for e in entries if e.source.label == LABELS["sonoff_zbminir2"]]
    assert len(entries) == 2 and loader.zha_digest(same) in BY_ID["sonoff_zbminir2"]["zha_digests"]
    swap(registry, LABELS["sonoff_zbminir2"], same)
    installation.options(high_risk=True)
    installation.setup()
    assert takes_over(registry, "sonoff_zbminir2")


# Newer ZHA quirks, none with an entity the take-over lacks (the rule before compared entities only).
NETWORK_LED = """    .switch(
        SonoffCluster.AttributeDefs.network_led.name,
        SonoffCluster.cluster_id,
        translation_key="network_led",
        fallback_name="Network LED",
    )
"""
ZBMINIR2_CHANGES = {
    "switch values": [("off_value=9,", "off_value=8,")],
    "attribute type": [("type=t.int16s,", "type=t.uint16_t,")],
    "enum member": [("    Normally_on_follow_trigger = 0x82\n", "    Normally_on_follow_trigger = 0x82\n    Reserved = 0x83\n")],
    "cluster code": [("    cluster_id = 0xFC11\n", "    cluster_id = 0xFC11\n\n    def _update_attribute(self, attrid, value):\n        super()._update_attribute(attrid, value)\n")],
    "device trigger": [("    .add_to_registry()", '    .device_automation_triggers({("remote_button_short_press", "button"): {"command": "toggle"}})\n    .add_to_registry()')],
    "firmware range": [("    .add_to_registry()", "    .firmware_version_filter(min_version=0x1000)\n    .add_to_registry()")],
    "entity name": [('fallback_name="Turbo mode"', 'fallback_name="Turbo"')],
    "an entity less": [(NETWORK_LED, "")],
}
SWV_CHANGES = {
    "converter": [("lambda x: x & ValveState.Water_Leakage,", "lambda x: x & ValveState.Water_Shortage_And_Leakage,")],
    "enum value a converter reads": [("    Water_Leakage = 2\n", "    Water_Leakage = 4\n")],
}
CHANGES = [("sonoff_zbminir2", "sonoff/zbminir2.py", name, change) for name, change in ZBMINIR2_CHANGES.items()] + [
    ("sonoff_swv", "sonoff/swv.py", name, change) for name, change in SWV_CHANGES.items()
]


@pytest.mark.parametrize("ident,rel,name,replacements", CHANGES, ids=[f"{c[0]}-{c[2]}" for c in CHANGES])
def test_a_changed_zha_quirk_wins_over_the_take_over(installation, registry, tmp_path, ident, rel, name, replacements):
    _, (newer,) = run_copy(tmp_path, registry, rel, replacements)
    # The rule before kept the take-over: the newer quirk has no entity it lacks.
    assert entities(newer) <= entities(build_new(BY_ID[ident])[0])
    assert loader.zha_digest(newer) not in BY_ID[ident]["zha_digests"]
    swap(registry, LABELS[ident], newer)
    installation.options(high_risk=True)
    installation.setup()
    assert registry.match_entry(device(ident)) is newer


def test_the_step_back_says_why_once(installation, registry, tmp_path, caplog):
    _, (newer,) = run_copy(tmp_path, registry, "sonoff/zbminir2.py", ZBMINIR2_CHANGES["switch values"])
    swap(registry, LABELS["sonoff_zbminir2"], newer)
    installation.options(high_risk=True)
    installation.setup()
    with caplog.at_level(logging.INFO, logger="xquirks"):
        for _ in range(3):
            assert registry.match_entry(device("sonoff_zbminir2")) is newer
    lines = [r.getMessage() for r in caplog.records if "steps back" in r.getMessage()]
    assert lines == ["xquirks: sonoff_zbminir2 steps back for a4:c1:38:00:00:00:00:01 (ZHA's quirk (SONOFF / ZBMINIR2) is not the one this take-over was built over)"]


SHELLY_CHANGES = {
    # (replacements in zhaquirks/shelly/__init__.py, extra text, whether ZHA's entry for the door sensor changes)
    "an enum member": ([("    bright = 0x02\n", "    bright = 0x02\n    very_bright = 0x03\n")], "", True),
    "a constant a class reads": ([("SHELLY_MANUFACTURER_CODE = 0x1490", "SHELLY_MANUFACTURER_CODE = 0x1491")], "", True),
    "an unrelated class and docstring": (
        [('"""Module for Shelly devices."""', '"""Shelly devices."""')],
        "\n\nclass Unrelated(CustomCluster):\n    cluster_id = 0xFC99\n",
        False,
    ),
}


@pytest.mark.parametrize("name", sorted(SHELLY_CHANGES))
def test_a_change_in_another_module_of_zha_counts(installation, registry, tmp_path, name):
    """ZHA's door_window.py stays byte for byte, zhaquirks/shelly/__init__.py (LightLevel, the cluster) changes. The
    take-over copies LightLevel: a digest of the quirk file alone would miss it."""
    replacements, extra, changes = SHELLY_CHANGES[name]
    shelly, _ = run_copy(tmp_path, registry, "shelly/__init__.py", replacements, extra=extra)
    _, (newer,) = run_copy(tmp_path, registry, "shelly/door_window.py", imports={"zhaquirks.shelly": shelly})
    assert (loader.zha_digest(newer) not in BY_ID["shelly_sbdw_103c"]["zha_digests"]) is changes
    swap(registry, LABELS["shelly_sbdw_103c"], newer)
    installation.options(high_risk=True)
    installation.setup()
    assert takes_over(registry, "shelly_sbdw_103c") is not changes


SYNTHETIC = '''
from zigpy.zcl.clusters.general import OnOff

from zhaquirks.builder import QuirkBuilder

MASK = {mask}

(
    QuirkBuilder("XQ", "Synthetic")
    .binary_sensor("on_off", OnOff.cluster_id, attribute_converter=lambda x: x & MASK, translation_key="x", fallback_name="X")
    .add_to_registry()
)
'''


def synthetic_digest(tmp_path, registry, text) -> str:
    number = next(_copies)
    path = tmp_path / f"synthetic_{number}.py"
    path.write_text(text)
    with registry.preserve_state():
        spec = importlib.util.spec_from_file_location(f"zhaquirks.xq_synthetic_{number}", path)
        spec.loader.exec_module(importlib.util.module_from_spec(spec))
        (entry,) = [e for e in registry if e.source.label == "(XQ / Synthetic)"]
        return loader.zha_digest(entry)


def test_a_global_a_converter_reads_counts(tmp_path, registry):
    """The converter's text stays, the module constant it reads changes."""
    digests = [synthetic_digest(tmp_path, registry, SYNTHETIC.format(mask=mask)) for mask in (2, 2, 3)]
    assert digests[0] == digests[1] != digests[2]


ENUM_METHOD = '''
import enum

from zigpy.zcl.clusters.general import OnOff

from zhaquirks.builder import QuirkBuilder


class Mode(enum.Enum):
    Heat = 1

    def holds(self, value):
        return value {op} self.value


(
    QuirkBuilder("XQ", "Synthetic")
    .binary_sensor("on_off", OnOff.cluster_id, attribute_converter=lambda x: Mode.Heat.holds(x), translation_key="x", fallback_name="X")
    .add_to_registry()
)
'''


def test_a_method_of_an_enum_counts(tmp_path, registry):
    """The converter calls a method of a zhaquirks enum: the method's text counts as a class's methods do."""
    digests = [synthetic_digest(tmp_path, registry, ENUM_METHOD.format(op=op)) for op in (">=", ">=", ">")]
    assert digests[0] == digests[1] != digests[2]


# The loader of sys.argv[1] as the module loader (LOAD).
LOAD = """
import importlib.util
spec = importlib.util.spec_from_file_location("loader", sys.argv[1])
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)
"""
SET_DIGEST = """
import sys, types, warnings
warnings.simplefilter("ignore")
""" + LOAD + """
from zha.quirks import DeviceRegistry
module = types.ModuleType("zhaquirks.xq_set")
module.REGISTRY = DeviceRegistry()
exec(sys.argv[2], module.__dict__)
print(loader.zha_digest(module.ENTRY))
"""
SET_OF_MEMBERS = '''
import enum

from zigpy.zcl.clusters.general import OnOff

from zhaquirks.builder import QuirkBuilder


class Mode(enum.Enum):  # its members hash by name: a set of them has another order under another hash seed
    Heat = 1
    Cool = 2
    Auto = 3
    Dry = 4


MODES = {Mode.Heat, Mode.Cool, Mode.Auto, Mode.Dry}
ENTRY = (
    QuirkBuilder("XQ", "Synthetic", registry=REGISTRY)
    .binary_sensor("on_off", OnOff.cluster_id, attribute_converter=lambda x: x in MODES, translation_key="x", fallback_name="X")
    .add_to_registry()
)
'''


def test_a_set_digests_the_same_in_every_python_run():
    """A converter reads a module constant set of enum members (no quirk of zha-quirks 2.4.1 reaches such a set): the
    same digest under hash seeds that put the set in three orders."""
    digests = set()
    for seed in ("0", "3", "4"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        done = subprocess.run([sys.executable, "-c", SET_DIGEST, loader.__file__, SET_OF_MEMBERS], env=env, capture_output=True, text=True, check=True, timeout=600)
        digests.add(done.stdout)
    assert len(digests) == 1, digests


def with_digests(ident, digests) -> str:
    definition = {**BY_ID[ident], "zha_digests": digests}
    lines = [json.dumps(definition, ensure_ascii=False, separators=(",", ":")) if json.loads(line)["id"] == ident else line for line in SAMPLE_TEXT.splitlines()]
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("listed,stays", [("other and current", True), ("other only", False)])
def test_any_listed_digest_counts(installation, registry, listed, stays):
    """zha_digests lists ZHA's entries of every release the definition is meant for (the HA release and its beta)."""
    current = BY_ID["sonoff_zbminir2"]["zha_digests"][0]
    installation.definitions(with_digests("sonoff_zbminir2", ["0123456789abcdef", current] if stays else ["0123456789abcdef"]))
    installation.options(high_risk=True)
    installation.setup()
    assert takes_over(registry, "sonoff_zbminir2") is stays


DIGESTS = """
import json, sys, warnings
warnings.simplefilter("ignore")
import zhaquirks
zhaquirks.setup()
from zha.quirks import DEVICE_REGISTRY, DeviceMatch
""" + LOAD + """
print(json.dumps(sorted(loader.zha_digest(e) for e in DEVICE_REGISTRY if isinstance(e.device_match, DeviceMatch) and loader._is_shipped(e.source.file))))
"""


def test_digests_are_the_same_in_every_python_run(registry):
    """Every entry of ZHA's registry, digested in two runs with other hash seeds and in this one."""
    runs = []
    for seed in ("0", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        done = subprocess.run([sys.executable, "-c", DIGESTS, loader.__file__], env=env, capture_output=True, text=True, check=True, timeout=600)
        runs.append(json.loads(done.stdout))
    here = sorted(loader.zha_digest(e) for e in registry if zhas(e))
    assert len(here) > 700 and runs[0] == runs[1] == here


def trial(tmp_path, *definitions) -> dict:
    """check() over the definitions, each with its own rev as the generator writes it."""
    path = tmp_path / "definitions.jsonl"
    path.write_text("".join(json.dumps({**definition, "rev": loader.revision(definition)}) + "\n" for definition in definitions))
    return loader.check(str(path))


NOT_KEPT = "ZHA's quirk (SONOFF / ZBMINIR2) is not the one this take-over was built over"
TRIALS = {
    "unchanged": None,
    "another release listed too": None,
    "only another release listed": NOT_KEPT,
    "a changed quirk": NOT_KEPT,
    "no quirk of ZHA": "take-over: ZHA ships no v2 quirk for its exact names",
}


@pytest.mark.parametrize("case", TRIALS)
def test_the_trial_build_names_a_take_over_zha_would_not_keep(registry, tmp_path, case):
    """A take-over steps back from each of ZHA's entries for its names whose digest it does not list (a newer
    zha-quirks). The trial build fails it as it fails one ZHA ships no v2 quirk for."""
    definition = dict(BY_ID["sonoff_zbminir2"])
    current = definition["zha_digests"][0]
    if case == "another release listed too":
        definition["zha_digests"] = ["0123456789abcdef", current]
    elif case == "only another release listed":
        definition["zha_digests"] = ["0123456789abcdef"]
    elif case == "a changed quirk":
        _, (newer,) = run_copy(tmp_path, registry, "sonoff/zbminir2.py", ZBMINIR2_CHANGES["switch values"])
        swap(registry, LABELS["sonoff_zbminir2"], newer)
    elif case == "no quirk of ZHA":
        registry.remove(zhas_entry(registry, LABELS["sonoff_zbminir2"]))
    assert trial(tmp_path, definition) == ({} if TRIALS[case] is None else {"sonoff_zbminir2": TRIALS[case]})


@pytest.mark.parametrize("listed", [2, 1])
def test_the_trial_build_checks_each_of_zhas_entries(registry, tmp_path, listed):
    """ZHA registers two entries for the S60ZBTPF (firmware before 0x2003 and from it on): a take-over that lists one
    would step back from the other for the devices of its firmware."""
    definition = definition_of("sonoff_s60zbtpf")
    assert len(definition["zha_digests"]) == 2
    definition["zha_digests"] = definition["zha_digests"][:listed]
    failures = trial(tmp_path, definition)
    assert failures == ({} if listed == 2 else {"sonoff_s60zbtpf": "ZHA's quirk (SONOFF / S60ZBTPF) is not the one this take-over was built over"})


def test_the_trial_build_names_a_take_over_whose_zha_quirk_cannot_be_digested(registry, tmp_path, monkeypatch):
    """A digest that raises fails its take-over: at load the take-over steps back from that entry. The trial build still
    checks the definitions after it."""
    digest = loader.zha_digest

    def failing(entry):
        if entry.source.label == LABELS["sonoff_zbminir2"]:
            raise RecursionError("maximum recursion depth exceeded")
        return digest(entry)

    monkeypatch.setattr(loader, "zha_digest", failing)
    swv = {**BY_ID["sonoff_swv"], "zha_digests": ["0123456789abcdef"]}
    assert trial(tmp_path, BY_ID["sonoff_zbminir2"], swv, BY_ID["shelly_sbdw_103c"]) == {
        "sonoff_zbminir2": "ZHA's quirk (SONOFF / ZBMINIR2) could not be digested: RecursionError: maximum recursion depth exceeded",
        "sonoff_swv": "ZHA's quirk (SONOFF / SWV) is not the one this take-over was built over",
    }


@pytest.mark.parametrize("change", ["take-over without digests", "digests without take-over", "empty list", "short digest", "upper case"])
def test_take_over_and_digests_go_together(change):
    definition = dict(BY_ID["sonoff_zbmicro" if change == "digests without take-over" else "sonoff_zbminir2"])
    if change == "take-over without digests":
        del definition["zha_digests"]
    else:
        definition["zha_digests"] = {"digests without take-over": ["0123456789abcdef"], "empty list": [], "short digest": ["01234567"], "upper case": ["0123456789ABCDEF"]}[change]
    with pytest.raises(loader.DefinitionError, match="take_over and zha_digests"):
        loader.build(definition, STUB_FILE, "xquirks", loader.Options(high_risk=True))
