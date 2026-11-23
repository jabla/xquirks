"""After a ZHA start: the notice of the devices that run another definition than at the start before or that the
high-risk option holds back since then, each with a few words on what happened, and one Tuya data query to each Tuya
device on a new or changed definition (through ZHA's public cluster command service)."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import logging
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import zhaquirks
from homeassistant.components import zha as zha_component
from homeassistant.components.zha.services import SERVICE_ISSUE_ZIGBEE_CLUSTER_COMMAND, SERVICE_SCHEMAS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed
from zha.quirks import DEVICE_REGISTRY, DeviceMatch, ModelInfo, QuirkRegistryEntry, QuirkSource
from zha.zigbee.device import Device

from .conftest import NO_QUIRK, STRINGS, Zha, continue_fix, label, our_issues, render, setup_entry, start_fix
from .integration import DOMAIN, FOLDER, PACKAGE, const, diagnostics, install

IEEE_A = "00:11:22:33:44:55:66:01"
IEEE_B = "00:11:22:33:44:55:66:02"
IEEE_C = "00:11:22:33:44:55:66:03"
IEEE_D = "00:11:22:33:44:55:66:04"
IEEE_E = "00:11:22:33:44:55:66:05"
IEEE_F = "00:11:22:33:44:55:66:06"
IEEE_G = "00:11:22:33:44:55:66:07"
IEEE_H = "00:11:22:33:44:55:66:08"
REV = "1a2b3c4d"
NEW_REV = "5e6f7a8b"
# zha's quirk_class of a device on a quirk of the user's and on one of ZHA's own.
OWN = "z2m_own:(None / X)"
ZHA_QUIRK = "zhaquirks.tuya.ts0601_switch:TuyaSwitch"
# The page of the integration: its Configure opens the options.
OPTIONS = "/config/integrations/integration/xquirks"
HELD = {"en": f"needs “[high risk]({OPTIONS})”.", "de": f"braucht „[hohes Risiko]({OPTIONS})“."}


@pytest.fixture
def loader(quirks_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """loader.py as the stub loads it, on definitions of the test's own. define(id, *names) adds one (high risk and not
    verified unless said, calls go into its chain), forget(id) drops it, load() registers them with the options file as
    it is now: what ZHA's custom quirk import does at each start."""
    spec = importlib.util.spec_from_file_location("xquirks_loader", FOLDER / "loader.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    path, lines = tmp_path / "definitions.jsonl", {}

    def define(ident: str, *names: tuple[str | None, str | None], risk: str = "high", endpoints: dict | None = None, take_over: bool = False,
               verified: bool = False, calls: tuple = ()) -> None:
        chain = [["QuirkBuilder", list(names[0])], *(["applies_to", list(pair)] for pair in names[1:]), *calls, ["add_to_registry"]]
        line = {"id": ident, "rev": REV, "risk": risk, "endpoints": endpoints or {"1": [0]}, "chain": chain}
        line |= ({"take_over": True, "zha_digests": ["0" * 16]} if take_over else {}) | ({"verified": True} if verified else {})
        lines[ident] = json.dumps(line)

    def load() -> None:
        path.write_text("".join(f"{line}\n" for line in lines.values()))
        module.register(str(quirks_dir / "xquirks.py"), "xquirks", str(path))

    yield SimpleNamespace(define=define, forget=lines.pop, load=load)
    module.purge()


@pytest.fixture
def zha_quirk():
    """quirk(manufacturer, model) registers a quirk of ZHA's own for the name, as zhaquirks does."""
    entries = []

    def quirk(manufacturer: str, model: str) -> None:
        source = QuirkSource("zhaquirks.acme", str(Path(zhaquirks.__file__).parent / "acme.py"), 1, f"Acme{model}")
        entries.append(DEVICE_REGISTRY.register(QuirkRegistryEntry(DeviceMatch(applies_to=(ModelInfo(manufacturer, model),)), source=source)))

    yield quirk
    for entry in entries:
        DEVICE_REGISTRY.remove(entry)


def query(ieee: str) -> dict:
    """The data query as the service gets it: the command without payload to the Tuya MCU cluster on endpoint 1."""
    return {"ieee": ieee, "endpoint_id": 1, "cluster_id": 0xEF00, "cluster_type": "in", "command": 0x03, "command_type": "server", "params": {}}


def notice(hass: HomeAssistant) -> list[str]:
    """The lines of the notice the user sees, none without one."""
    issue = our_issues(hass).get(const.CHANGED)
    return issue.translation_placeholders["devices"].splitlines() if issue else []


async def dismiss(hass: HomeAssistant) -> None:
    ir.async_ignore_issue(hass, DOMAIN, const.CHANGED, True)


async def close(hass: HomeAssistant) -> dict:
    """OK in the notice's dialog."""
    return await continue_fix(hass, await start_fix(hass, const.CHANGED), const.CHANGED, {})


async def test_the_first_start_lists_each_device_on_a_definition(hass: HomeAssistant, zha: Zha):
    zha.add_device(IEEE_A, name="Alpha", quirk=label("tuya_a"))
    zha.add_device(IEEE_B, name="Beta")
    zha.add_device(IEEE_C, name="Gamma", quirk=label("acme_c"))
    await setup_entry(hass)
    assert notice(hass) == ["Alpha: new definition.", "Gamma: new definition."]
    assert our_issues(hass)[const.CHANGED].is_fixable


LINES = {
    "en": [
        "Alpha: new definition.", "Beta: new definition.", "Charlie: new definition.", "Delta: no definition anymore.", "Echo: custom quirk.",
        "Foxtrot: ZHA's quirk.", "Golf: excluded.", f"Hotel: {HELD['en']}",
    ],
    "de": [
        "Alpha: neue Definition.", "Beta: neue Definition.", "Charlie: neue Definition.", "Delta: keine Definition mehr.", "Echo: eigener Quirk.",
        "Foxtrot: Quirk von ZHA.", "Golf: ausgeschlossen.", f"Hotel: {HELD['de']}",
    ],
}


@pytest.mark.parametrize("language", ["en", "de"])
async def test_each_device_gets_one_line_with_what_happened(hass: HomeAssistant, zha: Zha, quirks_dir: Path, loader, language: str):
    """A new revision, a definition first and another definition are new. A device without one now: none of its own
    left, a quirk of yours or ZHA's covers it, excluded in the options or held back by the high-risk option."""
    hass.config.language = language
    zha.add_device(IEEE_A, model="A-1", name="Alpha", quirk=label("tuya_a"))
    zha.add_device(IEEE_B, model="B-1", name="Beta")
    for ieee, name, ident in ((IEEE_C, "Charlie", "c"), (IEEE_D, "Delta", "d"), (IEEE_E, "Echo", "e"), (IEEE_F, "Foxtrot", "f"), (IEEE_G, "Golf", "g")):
        zha.add_device(ieee, model=f"{ident.upper()}-1", name=name, quirk=label(f"acme_{ident}"))
        loader.define(f"acme_{ident}", ("Acme", f"{ident.upper()}-1"), risk="low")
    zha.add_device(IEEE_H, model="H-1", name="Hotel", quirk=label("acme_h"))
    loader.define("acme_h", ("Acme", "H-1"))
    await install.async_set_options(hass, quirks_dir, excluded=[], high_risk=True)
    loader.load()
    await setup_entry(hass)
    await close(hass)
    await install.async_set_options(hass, quirks_dir, excluded=[IEEE_G], high_risk=False)
    loader.forget("acme_d")
    loader.load()
    await zha.reload({
        IEEE_A: label("tuya_a", NEW_REV), IEEE_B: label("acme_b"), IEEE_C: label("acme_x"), IEEE_D: NO_QUIRK, IEEE_E: OWN, IEEE_F: ZHA_QUIRK,
        IEEE_G: NO_QUIRK, IEEE_H: NO_QUIRK,
    })
    assert notice(hass) == LINES[language]


@pytest.mark.parametrize("language", ["en", "de"])
async def test_a_device_the_high_risk_option_holds_back_is_listed_without_an_earlier_state(hass: HomeAssistant, zha: Zha, loader, language: str):
    """The first start lists it, as the dialog's Markdown with the option's words linking the integration's page. Once:
    after OK neither the next ZHA start nor a restart lists it again."""
    hass.config.language = language
    loader.define("sonoff_zbmicro", (None, "ZBMicro"))
    loader.load()
    zha.add_device(IEEE_A, "SONOFF", "ZBMicro", "SONOFF ZBMicro")
    entry = await setup_entry(hass)
    assert notice(hass) == [f"SONOFF ZBMicro: {HELD[language]}"]
    flow = await start_fix(hass, const.CHANGED)
    shown = render(STRINGS["issues"][const.CHANGED]["fix_flow"]["step"]["confirm"]["description"], flow["description_placeholders"])
    assert shown == f"SONOFF ZBMicro: {HELD[language]}"
    assert (await continue_fix(hass, flow, const.CHANGED, {}))["type"] is FlowResultType.CREATE_ENTRY
    await zha.reload()
    await restart(hass, entry)
    assert notice(hass) == []


async def test_a_device_that_joins_held_back_is_listed(hass: HomeAssistant, zha: Zha, loader):
    loader.define("acme_r", ("Acme", "R-1"))
    loader.load()
    zha.add_device(IEEE_A, name="Alpha")
    await setup_entry(hass)
    assert notice(hass) == []
    zha.add_device(IEEE_B, model="R-1", name="Radar")
    await zha.reload()
    assert notice(hass) == [f"Radar: {HELD['en']}"]


async def test_a_held_back_device_stays_listed_until_it_runs_its_definition(hass: HomeAssistant, zha: Zha, quirks_dir: Path, loader):
    loader.define("acme_r", ("Acme", "R-1"))
    loader.load()
    zha.add_device(IEEE_A, model="R-1", name="Radar")
    await setup_entry(hass)
    await zha.reload()
    assert notice(hass) == [f"Radar: {HELD['en']}"]
    await install.async_set_options(hass, quirks_dir, excluded=[], high_risk=True)
    loader.load()
    await zha.reload({IEEE_A: label("acme_r")})
    assert notice(hass) == ["Radar: new definition."]


async def test_the_definition_is_found_by_the_zigpy_devices_names(hass: HomeAssistant, zha: Zha, loader):
    """ZHA shows the name a quirk of its own gives the device. The registry matches zigpy's."""
    loader.define("acme_r", ("Acme", "R-1"))
    loader.load()
    zha.add_device(IEEE_A, model="Radar R1", name="Radar")
    zha.zigpy[IEEE_A].model = "R-1"
    await setup_entry(hass)
    assert notice(hass) == [f"Radar: {HELD['en']}"]


async def test_a_take_over_held_back_lists_a_device_on_zhas_quirk(hass: HomeAssistant, zha: Zha, loader):
    """The option would put the definition over ZHA's quirk (none in the registry here whose digest the take-over
    would have to know)."""
    loader.define("acme_t", ("Acme", "T-1"), take_over=True)
    loader.load()
    zha.add_device(IEEE_A, model="T-1", name="Thermostat", quirk=ZHA_QUIRK)
    await setup_entry(hass)
    assert notice(hass) == [f"Thermostat: {HELD['en']}"]


@pytest.mark.parametrize("high_risk", [False, True])
async def test_a_device_that_never_had_a_definition_is_listed_only_when_the_option_holds_it_back(
    hass: HomeAssistant, zha: Zha, quirks_dir: Path, loader, zha_quirk, high_risk: bool,
):
    """Nothing to say: the option would not give any of these a definition (or it is on already). The definition's match
    and the loader's own check decide: a firmware the definition is not for, an endpoint the device lacks, a definition
    that does not build, a take-over over another version of ZHA's quirk."""
    devices = {
        "own": ({"quirk": OWN}, {}), "zha": ({"quirk": ZHA_QUIRK}, {}), "excluded": ({}, {}), "verified": ({}, {"verified": True}),
        "firmware": ({}, {"calls": [["firmware_version_filter", {"max_version": 1, "allow_missing": False}]]}), "misfit": ({}, {"endpoints": {"2": [0]}}),
        "broken": ({}, {"calls": [["no_such_call"]]}), "taken": ({"quirk": ZHA_QUIRK}, {"take_over": True}),
    }
    zha.add_device(IEEE_A, name="Plain")
    for number, (kind, (device, definition)) in enumerate(devices.items(), 2):
        zha.add_device(f"00:11:22:33:44:55:66:{number:02}", model=kind, name=kind.title(), **device)
        loader.define(f"acme_{kind}", ("Acme", kind), **definition)
    zha_quirk("Acme", "taken")
    if high_risk:
        zha.add_device("00:11:22:33:44:55:66:10", model="R-1", name="Radar")
        loader.define("acme_r", ("Acme", "R-1"))
    await install.async_set_options(hass, quirks_dir, excluded=["00:11:22:33:44:55:66:04"], high_risk=high_risk)
    loader.load()
    await setup_entry(hass)
    assert const.CHANGED not in our_issues(hass)


async def test_an_open_notice_keeps_what_it_lists(hass: HomeAssistant, zha: Zha):
    """Each device from its definition before the notice: one back where it was drops out."""
    zha.add_device(IEEE_A, name="Alpha")
    zha.add_device(IEEE_B, name="Beta")
    await setup_entry(hass)
    assert notice(hass) == []
    await zha.reload({IEEE_A: label("tuya_a")})
    await zha.reload({IEEE_A: label("tuya_a", NEW_REV), IEEE_B: label("acme_b")})
    assert notice(hass) == ["Alpha: new definition.", "Beta: new definition."]
    await zha.reload({IEEE_A: NO_QUIRK})
    assert notice(hass) == ["Beta: new definition."]
    await zha.reload({IEEE_B: NO_QUIRK})
    assert const.CHANGED not in our_issues(hass)


async def test_a_dismissed_notice_comes_back_only_with_new_changes(hass: HomeAssistant, zha: Zha):
    zha.add_device(IEEE_A, name="Alpha", quirk=label("tuya_a"))
    await setup_entry(hass)
    await dismiss(hass)
    await zha.reload()
    assert notice(hass) == []
    await zha.reload({IEEE_A: label("tuya_a", NEW_REV)})
    assert notice(hass) == ["Alpha: new definition."]


async def test_a_device_zha_no_longer_has_is_not_listed(hass: HomeAssistant, zha: Zha):
    zha.add_device(IEEE_A, name="Alpha", quirk=label("tuya_a"))
    zha.add_device(IEEE_B, name="Beta", quirk=label("acme_b"))
    await setup_entry(hass)
    zha.remove_device(IEEE_A)
    await zha.reload()
    assert notice(hass) == ["Beta: new definition."]


async def restart(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Home Assistant stops and starts again. The issue registry loads what it saved: an issue that is not persistent
    comes back inactive, with its dismissal."""
    assert await hass.config_entries.async_unload(entry.entry_id)
    registry = ir.async_get(hass)
    await registry._store.async_save(registry._data_to_save())
    await registry.async_load()
    assert [issue_id for (domain, issue_id), issue in registry.issues.items() if domain == DOMAIN and issue.active] == []
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_the_notice_and_what_it_compares_with_survive_a_restart(hass: HomeAssistant, zha: Zha, hass_storage: dict[str, Any], loader):
    """The notice until it is dismissed. The store keeps each device's label before and its label now or why it has
    none, and the devices the high-risk option held back."""
    loader.define("acme_r", ("Acme", "R-1"))
    loader.load()
    zha.add_device(IEEE_A, name="Alpha", quirk=label("tuya_a"))
    zha.add_device(IEEE_B, model="R-1", name="Radar")
    entry = await setup_entry(hass)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()
    assert hass_storage["xquirks.labels"]["data"] == {
        "labels": {IEEE_A: f"xquirks:tuya_a {REV}"}, "held_back": [IEEE_B], "changes": {IEEE_A: [None, f"xquirks:tuya_a {REV}"], IEEE_B: [None, "high_risk"]},
    }
    await restart(hass, entry)
    assert notice(hass) == ["Alpha: new definition.", f"Radar: {HELD['en']}"]
    assert zha.calls == []
    await dismiss(hass)
    await restart(hass, entry)
    assert notice(hass) == []
    await zha.reload({IEEE_A: label("tuya_a", NEW_REV)})
    assert notice(hass) == ["Alpha: new definition."]


async def test_the_notice_says_why_from_the_store_while_zha_cannot_show_its_devices(hass: HomeAssistant, zha: Zha, quirks_dir: Path, loader):
    loader.define("tuya_a", ("Acme", "A-1"), risk="low")
    loader.load()
    zha.add_device(IEEE_A, name="Alpha", quirk=label("tuya_a"))
    entry = await setup_entry(hass)
    await close(hass)
    await install.async_set_options(hass, quirks_dir, excluded=[IEEE_A], high_risk=False)
    loader.load()
    await zha.reload({IEEE_A: NO_QUIRK})
    zha.data.gateway_proxy = None
    await restart(hass, entry)
    assert notice(hass) == ["Alpha: excluded."]


async def test_ok_in_the_dialog_closes_the_notice_for_good(hass: HomeAssistant, zha: Zha, hass_storage: dict[str, Any]):
    """The dialog shows the text of the notice with its list. OK takes the notice and its devices out of the store: no
    restart raises it again. OK in a second dialog finds nothing left to close."""
    zha.add_device(IEEE_A, name="Alpha", quirk=label("tuya_a"))
    entry = await setup_entry(hass)
    flow, second = await start_fix(hass, const.CHANGED), await start_fix(hass, const.CHANGED)
    shown = render(STRINGS["issues"][const.CHANGED]["fix_flow"]["step"]["confirm"]["description"], flow["description_placeholders"])
    assert flow["step_id"] == "confirm" and shown == "Alpha: new definition."
    assert (await continue_fix(hass, flow, const.CHANGED, {}))["type"] is FlowResultType.CREATE_ENTRY
    assert (DOMAIN, const.CHANGED) not in ir.async_get(hass).issues
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()
    assert hass_storage["xquirks.labels"]["data"] == {"labels": {IEEE_A: f"xquirks:tuya_a {REV}"}, "held_back": [], "changes": {}}
    assert (await continue_fix(hass, second, const.CHANGED, {}))["type"] is FlowResultType.CREATE_ENTRY
    await restart(hass, entry)
    assert (DOMAIN, const.CHANGED) not in ir.async_get(hass).issues


def test_the_button_of_the_dialog_reads_ok_in_every_language():
    for path in sorted((FOLDER / "translations").glob("*.json")):
        assert json.loads(path.read_text())["issues"][const.CHANGED]["fix_flow"]["step"]["confirm"]["submit"] == "OK", path.name


async def test_after_ok_only_a_new_change_raises_a_notice(hass: HomeAssistant, zha: Zha):
    zha.add_device(IEEE_A, name="Alpha", quirk=label("tuya_a"))
    zha.add_device(IEEE_B, name="Beta", quirk=label("acme_b"))
    await setup_entry(hass)
    await close(hass)
    await zha.reload()
    assert notice(hass) == []
    await zha.reload({IEEE_A: label("tuya_a", NEW_REV)})
    assert notice(hass) == ["Alpha: new definition."]


async def test_ok_while_zha_reloads_leaves_what_that_start_changes(hass: HomeAssistant, zha: Zha):
    """The next notice lists none of the devices the closed one listed."""
    zha.add_device(IEEE_A, name="Alpha", quirk=label("tuya_a"))
    zha.add_device(IEEE_B, name="Beta")
    await setup_entry(hass)
    flow = await start_fix(hass, const.CHANGED)
    zha.start_reload()
    assert (await continue_fix(hass, flow, const.CHANGED, {}))["type"] is FlowResultType.CREATE_ENTRY
    zha.quirks[IEEE_B] = label("acme_b")
    zha.finish_reload()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert notice(hass) == ["Beta: new definition."]


async def test_a_list_that_changed_while_the_dialog_was_open_shows_before_ok_closes_it(hass: HomeAssistant, zha: Zha):
    zha.add_device(IEEE_A, name="Alpha", quirk=label("tuya_a"))
    zha.add_device(IEEE_B, name="Beta")
    await setup_entry(hass)
    flow = await start_fix(hass, const.CHANGED)
    await zha.reload({IEEE_B: label("acme_b")})
    flow = await continue_fix(hass, flow, const.CHANGED, {})
    both = ["Alpha: new definition.", "Beta: new definition."]
    assert flow["type"] is FlowResultType.FORM and flow["description_placeholders"]["devices"].splitlines() == both
    assert notice(hass) == both
    assert (await continue_fix(hass, flow, const.CHANGED, {}))["type"] is FlowResultType.CREATE_ENTRY
    assert notice(hass) == []


async def test_ok_while_xquirks_is_not_loaded_keeps_the_notice(hass: HomeAssistant, zha: Zha):
    """Its next setup raises it again from the store."""
    zha.add_device(IEEE_A, name="Alpha", quirk=label("tuya_a"))
    entry = await setup_entry(hass)
    flow = await start_fix(hass, const.CHANGED)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert (await continue_fix(hass, flow, const.CHANGED, {}))["reason"] == "not_loaded"
    assert notice(hass) == ["Alpha: new definition."]
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert notice(hass) == ["Alpha: new definition."]
    assert (await close(hass))["type"] is FlowResultType.CREATE_ENTRY and notice(hass) == []


async def test_devices_zha_cannot_show_yet_are_read_with_its_next_change(hass: HomeAssistant, zha: Zha):
    zha.add_device(IEEE_A, name="Alpha", quirk=label("tuya_a"))
    gateway, zha.data.gateway_proxy = zha.data.gateway_proxy, None
    await setup_entry(hass)
    assert notice(hass) == []
    zha.data.gateway_proxy = gateway
    hass.config_entries.async_update_entry(zha.entry, title="Zigbee")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert notice(hass) == ["Alpha: new definition."]


async def test_a_name_is_plain_text_in_the_notice(hass: HomeAssistant, zha: Zha):
    """Also at the start of its line: no list item, no code fence."""
    zha.add_device(IEEE_A, name="Lamp *bright*\n[x](y) `z`", quirk=label("tuya_a"))
    zha.add_device(IEEE_B, name="1. OG Flur", quirk=label("tuya_b"))
    zha.add_device(IEEE_C, name="- Spot ~~old~~", quirk=label("tuya_c"))
    await setup_entry(hass)
    assert notice(hass) == ["\\- Spot \\~\\~old\\~\\~: new definition.", "1\\. OG Flur: new definition.", "Lamp \\*bright\\* \\[x\\](y) \\`z\\`: new definition."]


async def test_a_tuya_device_on_a_new_definition_is_asked_once(hass: HomeAssistant, zha: Zha):
    zha.add_device(IEEE_A, "_TZE200_aaaa", "TS0601", "Radar", tuya=True)
    entry = await setup_entry(hass)
    assert zha.calls == []
    await zha.reload({IEEE_A: label("tuya_radar")})
    assert zha.calls == [query(IEEE_A)]
    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    assert result["tuya_data_queries"] == {"tuya_radar": ["_TZE200_aaaa TS0601: sent"]}
    # Once: a ZHA start without a change asks nothing.
    await zha.reload()
    assert zha.calls == [query(IEEE_A)]
    # An update asks again, leaving xquirks does not.
    await zha.reload({IEEE_A: label("tuya_radar", NEW_REV)})
    await zha.reload({IEEE_A: NO_QUIRK})
    assert zha.calls == [query(IEEE_A)] * 2


async def test_only_tuya_devices_ZHA_reports_available_and_not_disabled_are_asked(hass: HomeAssistant, zha: Zha):
    """A device without a Tuya MCU cluster is not asked and not listed, an unavailable one is listed as skipped."""
    zha.add_device(IEEE_A, "_TZE200_aaaa", "TS0601", tuya=True)
    disabled = zha.add_device(IEEE_B, "_TZE200_bbbb", "TS0601", tuya=True)
    zha.add_device(IEEE_C, "_TZE200_cccc", "TS0601", tuya=True)
    zha.add_device(IEEE_D, "Acme", "D-1")
    dr.async_get(hass).async_update_device(disabled, disabled_by=dr.DeviceEntryDisabler.USER)
    zha.unavailable.add(IEEE_C)
    entry = await setup_entry(hass)
    await zha.reload({ieee: label("tuya_radar") for ieee in (IEEE_A, IEEE_B, IEEE_C)} | {IEEE_D: label("acme_d")})
    assert zha.calls == [query(IEEE_A)]
    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    assert result["tuya_data_queries"] == {"tuya_radar": ["_TZE200_aaaa TS0601: sent", "_TZE200_cccc TS0601: skipped, ZHA reports it unavailable"]}


async def test_a_failed_query_is_not_repeated_and_logs_nothing_above_debug(hass: HomeAssistant, zha: Zha, caplog: pytest.LogCaptureFixture):
    """A sleeping battery device may miss the query, a device may leave meanwhile: best effort, the next device is asked
    all the same."""
    zha.add_device(IEEE_A, "_TZE200_aaaa", "TS0601", tuya=True)
    zha.add_device(IEEE_B, "_TZE200_bbbb", "TS0601", tuya=True)
    zha.errors[IEEE_A] = ValueError(f"Device with IEEE {IEEE_A} not found")
    entry = await setup_entry(hass)
    with caplog.at_level(logging.DEBUG):
        await zha.reload({IEEE_A: label("tuya_radar"), IEEE_B: label("tuya_radar")})
    assert zha.calls == [query(IEEE_A), query(IEEE_B)]
    # The error's kind only: its text names the device's address.
    assert entry.runtime_data.queries == {"tuya_radar": ["_TZE200_aaaa TS0601: not delivered (ValueError)", "_TZE200_bbbb TS0601: sent"]}
    assert IEEE_A not in json.dumps(await diagnostics.async_get_config_entry_diagnostics(hass, entry))
    ours = [record for record in caplog.records if record.name.startswith(PACKAGE)]
    assert any("not delivered" in record.getMessage() for record in ours)
    assert all(record.levelno <= logging.DEBUG for record in ours)
    await zha.reload()
    assert len(zha.calls) == 2


async def test_the_queries_of_a_zha_start_stop_when_zha_goes_down(hass: HomeAssistant, zha: Zha):
    """They would go to the devices of a gateway that is gone."""
    zha.add_device(IEEE_A, "_TZE200_aaaa", "TS0601", tuya=True)
    zha.add_device(IEEE_B, "_TZE200_bbbb", "TS0601", tuya=True)
    await setup_entry(hass)
    zha.hold = asyncio.Event()
    zha.start_reload()
    zha.quirks.update({IEEE_A: label("tuya_radar"), IEEE_B: label("tuya_radar")})
    zha.finish_reload()
    # The start reads the files first.
    for _ in range(200):
        if zha.calls:
            break
        await asyncio.sleep(0.01)
    assert zha.calls == [query(IEEE_A)]
    zha.start_reload()
    zha.hold.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert zha.calls == [query(IEEE_A)]


def test_the_query_is_valid_for_zhas_service():
    """The data the integration sends passes the schema of ZHA's own service."""
    validated = SERVICE_SCHEMAS[SERVICE_ISSUE_ZIGBEE_CLUSTER_COMMAND](query(IEEE_A))
    assert str(validated["ieee"]) == IEEE_A and validated["params"] == {} and "args" not in validated


def test_zha_set_its_devices_up_when_its_entry_is_loaded():
    """The query goes out when the integration sees ZHA's entry loaded: by then ZHA's setup has applied the quirks and
    set up the devices and their entities. Availability is a property of ZHA's device."""
    setup = inspect.getsource(zha_component.async_setup_entry)
    assert setup.index("await zha_gateway.async_initialize()") < setup.index("await ha_zha_data.gateway_proxy.async_initialize_devices_and_entities()")
    assert setup.index("async_initialize_devices_and_entities()") < setup.index("await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)")
    assert isinstance(inspect.getattr_static(Device, "available"), property)
