"""Precedence: the user's own quirks win, ZHA's own quirk wins, the take-over stays where it carries more.

Each case runs zhaquirks.setup over a custom quirks folder holding the stub and a user's file whose name
sorts before or after it ("a_user_..." or "z_user_..."). The user's quirk is a v2 builder quirk or a v1
CustomDevice, for the device's exact (manufacturer, model) pair or for its model alone.
"""

import dataclasses
import pathlib

import pytest
import zhaquirks
from zha.quirks import DeviceMatch, DeviceRegistry, ModelInfo, QuirkRegistryEntry, QuirkSource
from zhaquirks.builder import EntityType, QuirkBuilder

from helpers import BY_ID, label_of, layout_of, make_raw, raw_of

SHIPPED_FILE = str(pathlib.Path(zhaquirks.__file__).resolve().parent / "z2m_test_shipped.py")

# A definition registered under the exact pair and one under the model alone, with the name of their device.
CASES = {
    "exact": ("heat_decor_hd_t1000", "_TZE200_spyvfeti", "TS0601"),
    "model_only": ("sonoff_zbmicro", "SONOFF", "ZBMicro"),
}


def v2_file(manufacturer, model) -> str:
    return f"from zhaquirks.builder import QuirkBuilder\n\nQuirkBuilder({manufacturer!r}, {model!r}).add_to_registry()\n"


def v1_file(manufacturer, model, raw) -> str:
    """A v1 CustomDevice whose signature is the device's own endpoints (it changes nothing)."""
    endpoints = {
        ep_id: {"profile_id": ep.profile_id, "device_type": ep.device_type, "input_clusters": sorted(ep.in_clusters), "output_clusters": sorted(ep.out_clusters)}
        for ep_id, ep in raw.endpoints.items()
        if ep_id
    }
    names = f"MODELS_INFO: [({manufacturer!r}, {model!r})]" if manufacturer else f"MODEL: {model!r}"
    return (
        "from zhaquirks import CustomDevice\n"
        "from zhaquirks.const import DEVICE_TYPE, ENDPOINTS, INPUT_CLUSTERS, MODEL, MODELS_INFO, OUTPUT_CLUSTERS, PROFILE_ID\n\n"
        f"_EPS = {endpoints!r}\n"
        "_LAYOUT = {ep: {PROFILE_ID: v['profile_id'], DEVICE_TYPE: v['device_type'], INPUT_CLUSTERS: v['input_clusters'], OUTPUT_CLUSTERS: v['output_clusters']} for ep, v in _EPS.items()}\n\n\n"
        "class UserQuirk(CustomDevice):\n"
        f"    signature = {{{names}, ENDPOINTS: _LAYOUT}}\n"
        "    replacement = {ENDPOINTS: _LAYOUT}\n"
    )


@pytest.mark.parametrize("order", ["before", "after"])
@pytest.mark.parametrize("kind", ["v2", "v1"])
@pytest.mark.parametrize("key", ["exact", "model"])
@pytest.mark.parametrize("case", sorted(CASES))
def test_the_users_quirk_wins(installation, registry, case, key, kind, order):
    ident, manufacturer, model = CASES[case]
    consts = layout_of(BY_ID[ident])
    raw = make_raw(consts, manufacturer, model)
    installation.options(high_risk=True)
    installation.setup()
    assert registry.match_entry(make_raw(consts, manufacturer, model)).source.label.startswith(f"xquirks:{ident} "), "the definition applies without the user's file"
    user_manufacturer = manufacturer if key == "exact" else None
    text = v2_file(user_manufacturer, model) if kind == "v2" else v1_file(user_manufacturer, model, raw)
    user = installation.user_file(f"{'a' if order == 'before' else 'z'}_user_{kind}.py", text)
    installation.setup()
    entry = registry.match_entry(make_raw(consts, manufacturer, model))
    assert entry is not None and entry.source.file == str(user), entry and entry.source
    resolved = registry.resolve(make_raw(consts, manufacturer, model))
    assert resolved._quirk_registry_entry.source.file == str(user)
    # The definition's own filter steps back, naming the user's file (whichever entry the registry tries first).
    ours = next(e for e in registry if str(getattr(e.source, "label", "")).startswith(f"xquirks:{ident} "))
    holder = next(f for f in ours.device_match.filters if type(f).__name__ == "OnDemand")
    step_back = next(f for f in holder.entry().device_match.filters if type(f).__name__ == "StepBack")
    assert str(user) in step_back.reason(make_raw(consts, manufacturer, model))


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_quirk_shipped_with_zha_wins(installation, registry, case):
    """A stand-in for a quirk ZHA ships, registered before the custom quirks as zhaquirks registers its own."""
    ident, manufacturer, model = CASES[case]
    consts = layout_of(BY_ID[ident])
    shipped = QuirkRegistryEntry(
        device_match=DeviceMatch(applies_to=(ModelInfo(manufacturer if case == "exact" else None, model),)),
        source=QuirkSource(module="zhaquirks.z2m_test_shipped", file=SHIPPED_FILE, line=1, label="ShippedStandIn"),
    )
    registry.register(shipped)
    installation.options(high_risk=True)
    installation.setup()
    assert registry.match_entry(make_raw(consts, manufacturer, model)) is shipped


def test_take_over_stays_over_zhas_quirk_with_fewer_entities(installation, registry):
    """zhaquirks ships sonoff.zbminir2 with fewer entities than the definition carries."""
    raw = raw_of("sonoff_zbminir2", "SONOFF", "ZBMINIR2")
    assert any(e.source.file and "zhaquirks/sonoff/zbminir2.py" in e.source.file for e in registry if isinstance(e.device_match, DeviceMatch) and e.device_match.matches(raw))
    installation.options(high_risk=True)
    installation.setup()
    assert registry.match_entry(raw_of("sonoff_zbminir2", "SONOFF", "ZBMINIR2")).source.label == label_of(BY_ID["sonoff_zbminir2"])


def test_take_over_steps_back_when_zhas_quirk_has_an_entity_it_lacks(installation, registry):
    """A newer ZHA quirk (a stand-in with one more entity) wins over the take-over."""
    scratch = DeviceRegistry()
    entry = (
        QuirkBuilder("SONOFF", "ZBMINIR2", registry=scratch)
        .switch(attribute_name="back_light", cluster_id=0xFC11, entity_type=EntityType.CONFIG, translation_key="back_light", fallback_name="Backlight")
        .add_to_registry()
    )
    newer = dataclasses.replace(entry, source=QuirkSource(module="zhaquirks.z2m_test_shipped", file=SHIPPED_FILE, line=1, label="NewerZhaQuirk"))
    registry.register(newer)
    installation.options(high_risk=True)
    installation.setup()
    assert registry.match_entry(raw_of("sonoff_zbminir2", "SONOFF", "ZBMINIR2")) is newer


def test_take_over_steps_back_for_the_users_quirk(installation, registry):
    """The take-over exception covers ZHA's own quirk, never the user's file."""
    installation.setup()
    user = installation.user_file("a_user_v2.py", v2_file("SONOFF", "ZBMINIR2"))
    installation.setup()
    assert registry.match_entry(raw_of("sonoff_zbminir2", "SONOFF", "ZBMINIR2")).source.file == str(user)


def test_endpoints_that_do_not_fit(installation, registry):
    """sonoff_zbmicro needs cluster 0xFC11 on endpoint 1: a device with it on endpoint 2 keeps ZHA's handling."""
    consts = dict(layout_of(BY_ID["sonoff_zbmicro"]), ENDPOINTS={1: [], 2: [0xFC11]})
    installation.options(high_risk=True)
    installation.setup()
    assert registry.match_entry(make_raw(consts)) is None
