"""Building on demand: register() puts a stand-in per definition into ZHA's registry and builds the definition when
ZHA first tries it for a device (loader.OnDemand).

The results must be those of building every definition at once (register_eagerly): the same entries in the same order,
the same definition for every device, the same resolved device with the same entities and states, the same device
triggers for Home Assistant's early trigger cache. Only the entry ZHA keeps on the device is the stand-in.
"""

import json
import pathlib
import threading
import time

import pytest
from zha.quirks import DEVICE_REGISTRY, DeviceMatch, ModelInfo, QuirkRegistryEntry, QuirkSource
from zha.zigbee.device import get_device_automation_triggers

import loader
from helpers import (
    BY_ID, EDGE_INPUTS, IEEE, SAMPLE, Describer, alias_layouts, build_zha_device, definition_of, device_view, label_of, layout_of, make_raw, raw_of,
)


def is_ours(entry) -> bool:
    return str(getattr(entry.source, "label", "")).startswith(loader.LABEL_PREFIX)


def ours(result) -> bool:
    """An outcome of one of our definitions."""
    return result is not None and result["label"].startswith(loader.LABEL_PREFIX)


def holders(registry) -> dict:
    """The OnDemand of every stand-in by its label (any copy of the loader: the stub loads its own)."""
    return {e.source.label: f for e in registry if is_ours(e) for f in e.device_match.filters if type(f).__name__ == "OnDemand"}


def built(entry):
    """The entry behind a stand-in. An entry built at once (an alias) or none of ours as it is."""
    holder = next((f for f in entry.device_match.filters if type(f).__name__ == "OnDemand"), None)
    return holder.entry() if holder is not None else entry


def register_eagerly(stub: pathlib.Path, path: pathlib.Path) -> None:
    """Every definition built at once and registered in line order."""
    options = loader.Options.read(stub.with_name(loader.OPTIONS_FILE))
    loader.purge()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            for entry in loader.build(loader.parse(line), str(stub), "xquirks", options):
                DEVICE_REGISTRY.register(entry)


def register_on_demand(stub: pathlib.Path, path: pathlib.Path) -> None:
    loader.register(str(stub), "xquirks", str(path))


# Devices: (name, consts, manufacturer, model) for make_raw.


def sample_devices() -> list:
    """The sample's devices and one whose endpoints do not fit (the definition steps back)."""
    misfit = dict(layout_of(BY_ID["sonoff_zbmicro"]), ENDPOINTS={1: [], 2: [0xFC11]})
    return [*definition_devices(SAMPLE), ("sonoff_zbmicro misfit", misfit, None, None)]


def definition_devices(definitions) -> list:
    """Each definition's device under each of its names (an alias: one device per fingerprint)."""
    out = []
    for definition in definitions:
        if "alias" in definition:
            for layout in alias_layouts(definition):
                out.append((f"{definition['id']} {(layout['MANUFACTURER'], layout['MODEL'])}", layout, None, None))
            continue
        consts = layout_of(definition)
        out.append((definition["id"], consts, None, None))
        for call in definition["chain"]:
            if call[0] in ("QuirkBuilder", "TuyaQuirkBuilder", "applies_to") and len(call) > 1:
                manufacturer, model = call[1]
                out.append((f"{definition['id']} {manufacturer}/{model}", consts, manufacturer or consts["MANUFACTURER"], model or consts["MODEL"]))
    return out


def outcome(registry, name, consts, manufacturer, model, inputs, with_device: bool) -> dict | None:
    """What the registry makes of the device: the definition, its built entry, the resolved device and entities."""
    entry = registry.match_entry(make_raw(consts, manufacturer, model))
    if entry is None:
        return None
    out = {"label": entry.source.label, "source": (entry.source.module, entry.source.file, entry.source.line)}
    if not is_ours(entry):
        return out
    out["applies_to"] = tuple(entry.device_match.applies_to)
    out["entry"] = Describer({loader.__name__}, inputs).entry(built(entry))
    if with_device:
        resolved = registry.resolve(make_raw(consts, manufacturer, model))
        assert getattr(resolved, "_quirk_registry_entry") is entry, name
        device, entities = build_zha_device(resolved)
        out["quirk_class"] = device.quirk_class
        out["device"] = device_view(resolved, device, entities, Describer({loader.__name__}, inputs))
    return out


def compare(installation, registry, path, devices, with_device=True, inputs=EDGE_INPUTS) -> dict:
    """Both registrations over the same devices: the order of our entries and each device's outcome."""
    sides = {}
    for name, register in (("eager", register_eagerly), ("on demand", register_on_demand)):
        register(installation.stub, path)
        sides[name] = {
            "order": [e.source.label for e in registry if is_ours(e)],
            "devices": {d[0]: outcome(registry, *d, inputs=inputs, with_device=with_device) for d in devices},
        }
        if name == "on demand":
            sides[name]["states"] = sorted({h.state for h in holders(registry).values()})
    eager, on_demand = sides["eager"], sides["on demand"]
    assert on_demand["order"] == eager["order"]
    for name in eager["devices"]:
        assert on_demand["devices"][name] == eager["devices"][name], name
    assert "failed" not in on_demand["states"]
    return sides


def test_same_entries_as_building_at_once(installation, registry):
    """High risk on, high risk off, the device excluded and a user's quirk for one name registered before ours."""
    path = installation.integration / loader.DEFINITIONS_FILE
    devices = sample_devices()
    user = QuirkRegistryEntry(
        device_match=DeviceMatch(applies_to=(ModelInfo("_TZE200_ra6wrlgv", "TS0601"),)),
        source=QuirkSource(module="a_user_v2", file=str(installation.quirks / "a_user_v2.py"), line=1, label="UserQuirk"),
    )
    registry.register(user)
    installation.options(high_risk=True)
    sides = compare(installation, registry, path, devices)
    outcomes = sides["eager"]["devices"]
    applied = {o["label"] for o in outcomes.values() if ours(o)}
    # Every sample definition applies to its own device except the one under the user's name.
    assert applied == {label_of(d) for d in SAMPLE} - {label_of(BY_ID["box_erc2206_z"])}
    assert outcomes["box_erc2206_z"]["label"] == "UserQuirk"
    assert outcomes["sonoff_zbmicro misfit"] is None
    assert sum(map(ours, outcomes.values())) == len(devices) - 3  # all but box_erc2206_z's two and the misfit
    assert all(o["device"]["entities"] for o in outcomes.values() if ours(o))
    assert sides["on demand"]["states"] == ["built"]
    installation.options()
    sides = compare(installation, registry, path, devices, with_device=False)
    assert outcomes["heat_decor_hd_t1000"] and sides["eager"]["devices"]["heat_decor_hd_t1000"] is None  # high risk off
    installation.options(high_risk=True, excluded=[IEEE])
    sides = compare(installation, registry, path, devices, with_device=False)
    assert not any(ours(o) for o in sides["eager"]["devices"].values())
    assert sides["eager"]["devices"]["box_erc2206_z"]["label"] == "UserQuirk"


def test_nothing_is_built_until_a_device_shows_up(installation, registry):
    installation.options(high_risk=True)
    installation.setup()
    pending = holders(registry)
    assert len(pending) == sum("chain" in d for d in SAMPLE) == 14
    assert {h.state for h in pending.values()} == {"pending"}
    assert registry.match_entry(raw_of("sonoff_zbmicro")).source.label == label_of(BY_ID["sonoff_zbmicro"])
    assert {label: h.state for label, h in pending.items() if h.state != "pending"} == {label_of(BY_ID["sonoff_zbmicro"]): "built"}


def test_a_device_that_joins_later_gets_its_quirk(installation, registry):
    """ZHA resolves a joining device as it resolves the known ones at setup: no other setup needed."""
    installation.options(high_risk=True)
    installation.setup()
    registry.resolve(raw_of("sonoff_zbmicro"))
    label = label_of(BY_ID["sonoff_mini_zbdim"])
    assert holders(registry)[label].state == "pending"
    joined = registry.resolve(raw_of("sonoff_mini_zbdim"))
    stand_in = joined._quirk_registry_entry
    assert stand_in.source.label == label and holders(registry)[label].state == "built"
    device, entities = build_zha_device(joined)
    assert type(device).__name__ == "QuirkV2Device"
    assert device.quirk_metadata is built(stand_in).zha_device_factory.quirk_definition
    assert device.quirk_class == f"xquirks:{label}"
    metadata = device.quirk_metadata.entity_metadata
    assert metadata and {f"{IEEE}-{m.endpoint_id}-{m.resolved_unique_id_suffix}" for m in metadata} <= {e.unique_id for e in entities}
    # A re-interview resolves the device anew: the same factory, so ZHA rebuilds its device instead of replacing it.
    again = registry.resolve(raw_of("sonoff_mini_zbdim"))
    assert again._quirk_registry_entry.zha_device_factory is stand_in.zha_device_factory


def test_device_triggers_for_the_early_trigger_cache(installation, registry):
    """Home Assistant reads device triggers from devices it resolves before ZHA starts (the triggers transform)."""
    definition = definition_of("aqara_qbkg17lm")
    installation.definitions(json.dumps(definition) + "\n")
    installation.options(high_risk=True)
    installation.setup()
    resolved = registry.resolve(make_raw(layout_of(definition)))
    expected = dict(loader.build(definition, str(installation.stub), "xquirks", loader.Options())[0].zha_device_factory.quirk_definition.device_automation_triggers)
    assert expected
    assert get_device_automation_triggers(resolved) == {("device_offline", "device_offline"): {"device_event_type": "device_offline"}, **expected}


NAMES = [
    [["QuirkBuilder", ["A", "B"]]],
    [["QuirkBuilder", [None, "B"]], ["applies_to", ["C", "D"]], ["applies_to", [None, "E"]]],
    [["TuyaQuirkBuilder"], ["applies_to", ["_TZE200_x", "TS0601"]], ["applies_to", ["_TZE204_x", "TS0601"]]],
    [["QuirkBuilder", {"model": "B"}]],
    [["QuirkBuilder", ["A"]]],
    [["QuirkBuilder", {"manufacturer": "A", "model": "B"}], ["applies_to", {"manufacturer": None, "model": "C"}]],
]


@pytest.mark.parametrize("calls", NAMES, ids=range(len(NAMES)))
def test_names_as_the_builder_collects_them(calls):
    """The names a stand-in registers under are the applies_to of the entry the builder makes from the same calls."""
    from zha.quirks import DeviceRegistry
    from zhaquirks.builder import QuirkBuilder
    from zhaquirks.tuya.builder import TuyaQuirkBuilder

    scratch = DeviceRegistry()
    first = calls[0]
    builder = (TuyaQuirkBuilder if first[0] == "TuyaQuirkBuilder" else QuirkBuilder)(*loader._call_parts(first)[0], registry=scratch, **loader._call_parts(first)[1])
    for call in calls[1:]:
        args, kwargs = loader._call_parts(call)
        builder.applies_to(*args, **kwargs)
    assert loader._names([*calls, ["add_to_registry"]]) == builder.add_to_registry().device_match.applies_to


@pytest.mark.parametrize("calls,reason", [
    ([["QuirkBuilder"]], "no manufacturer and model"),
    ([["QuirkBuilder", [None, None]]], "must be named"),
    ([["TuyaQuirkBuilder"], ["applies_to", [None, None]]], "must be named"),
    ([["QuirkBuilder", [{"ref": "COMMAND"}, "B"]]], "literal strings or null"),
    ([["QuirkBuilder", {"model": "B", "registry": None}]], "literal strings or null"),
    ([["QuirkBuilder"], ["applies_to", ["A"]]], "missing a required argument"),
    ([["Builder", ["A", "B"]]], "starts with QuirkBuilder"),
])
def test_names_refused_at_load(calls, reason):
    """The builder refuses these too (or the loader's checks would at the build)."""
    try:
        loader._names([*calls, ["add_to_registry"]])
    except loader.DefinitionError as err:
        assert reason in str(err)
    else:
        raise AssertionError("not refused")


def test_one_build_when_threads_race(installation, registry, monkeypatch):
    installation.options(high_risk=True)
    register_on_demand(installation.stub, installation.integration / loader.DEFINITIONS_FILE)
    holder = holders(registry)[label_of(BY_ID["sonoff_zbmicro"])]
    calls = []
    original = loader.build

    def slow(*args):
        calls.append(args)
        time.sleep(0.05)
        return original(*args)

    monkeypatch.setattr(loader, "build", slow)
    results = []
    threads = [threading.Thread(target=lambda: results.append(holder.entry())) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 1
    assert len(results) == 4 and results[0] is not None and all(r is results[0] for r in results)
