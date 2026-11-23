"""Config flow (one entry, writes the stub and the options) and the options (devices left alone, high risk, a ZHA
reload)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import STRINGS, Zha, check_page, label, setup_entry
from .integration import DOMAIN, FOLDER

IEEE_A = "00:11:22:33:44:55:66:01"
IEEE_B = "00:11:22:33:44:55:66:02"
IEEE_GONE = "00:11:22:33:44:55:66:09"


async def start(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    return check_page(STRINGS["config"], result)


async def choose(hass: HomeAssistant, flow: dict, option: str) -> dict:
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"next_step_id": option})
    await hass.async_block_till_done(wait_background_tasks=True)
    return check_page(STRINGS["config"], result)


@pytest.mark.parametrize(("option", "reloads"), [("reload", 1), ("later", 0)])
async def test_setup_writes_the_stub_and_the_options(hass: HomeAssistant, zha: Zha, quirks_dir: Path, option: str, reloads: int):
    flow = await start(hass)
    # Nothing is written before the choice.
    assert flow["type"] is FlowResultType.MENU and flow["description_placeholders"] == {"folder": str(quirks_dir)}
    assert list(quirks_dir.iterdir()) == []
    result = await choose(hass, flow, option)
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["data"] == {}
    assert (quirks_dir / "xquirks.py").read_bytes() == (FOLDER / "stub.py.txt").read_bytes()
    assert json.loads((quirks_dir / "xquirks_options.json").read_text()) == {"excluded": [], "high_risk": False}
    assert zha.reloads == reloads
    assert hass.config_entries.async_entries(DOMAIN)[0].state is config_entries.ConfigEntryState.LOADED
    # Only one entry: Home Assistant aborts with a text of its own.
    second = await start(hass)
    assert second["reason"] == "single_instance_allowed" and second["translation_domain"] == "homeassistant"


@pytest.mark.parametrize(("state", "reloads"), [(config_entries.ConfigEntryState.SETUP_IN_PROGRESS, 1), (config_entries.ConfigEntryState.SETUP_RETRY, 0)])
async def test_a_zha_still_starting_is_reloaded_after_its_setup(hass: HomeAssistant, zha: Zha, state: config_entries.ConfigEntryState, reloads: int):
    """A setup in progress imported the custom quirks before the stub was there: ZHA reloads once it is done. A retry of a
    failed setup imports them anyway."""
    zha.entry.mock_state(hass, state)
    result = await choose(hass, await start(hass), "reload")
    assert result["type"] is FlowResultType.CREATE_ENTRY and zha.reloads == reloads


async def test_setup_without_a_custom_quirks_folder_stops(hass: HomeAssistant, zha: Zha):
    zha.data.yaml_config.pop("custom_quirks_path")
    assert (await start(hass))["reason"] == "no_folder"


async def test_a_failed_write_says_why(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    quirks_dir.rmdir()
    result = await choose(hass, await start(hass), "reload")
    assert result["reason"] == "write_failed" and "No such file or directory" in result["description_placeholders"]["error"]
    assert hass.config_entries.async_entries(DOMAIN) == [] and zha.reloads == 0


async def options(hass: HomeAssistant, entry) -> dict:
    return check_page(STRINGS["options"], await hass.config_entries.options.async_init(entry.entry_id))


async def submit(hass: HomeAssistant, flow: dict, user_input: dict) -> dict:
    result = await hass.config_entries.options.async_configure(flow["flow_id"], user_input)
    await hass.async_block_till_done(wait_background_tasks=True)
    return check_page(STRINGS["options"], result)


async def test_the_options_offer_the_devices_on_xquirks_and_those_left_alone(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    zha.add_device(IEEE_A, name="Radar", quirk=label("tuya_radar"))
    zha.add_device(IEEE_B, name="Lamp", quirk="zhaquirks.tuya.ts0601:Lamp")
    (quirks_dir / "xquirks_options.json").write_text(json.dumps({"excluded": [IEEE_GONE], "high_risk": True}))
    entry = await setup_entry(hass)
    flow = await options(hass, entry)
    schema = flow["data_schema"].schema
    keys = {str(key): key for key in schema}
    assert list(keys) == ["excluded", "high_risk", "reload"]
    assert [keys[name].default() for name in keys] == [[IEEE_GONE], True, True]
    # A device on another quirk is not offered; one left alone that ZHA no longer has shows its address.
    assert schema[keys["excluded"]].config["options"] == [{"value": IEEE_GONE, "label": IEEE_GONE}, {"value": IEEE_A, "label": "Radar (tuya_radar)"}]


async def test_saving_the_options_replaces_the_file_and_reloads_zha(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    zha.add_device(IEEE_A, name="Radar", quirk=label("tuya_radar"))
    (quirks_dir / "xquirks_options.json").write_text(json.dumps({"excluded": [IEEE_GONE], "high_risk": True}))
    entry = await setup_entry(hass)
    result = await submit(hass, await options(hass, entry), {"excluded": [IEEE_A], "high_risk": False, "reload": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert json.loads((quirks_dir / "xquirks_options.json").read_text()) == {"excluded": [IEEE_A], "high_risk": False}
    assert zha.reloads == 1
    await submit(hass, await options(hass, entry), {"excluded": [], "high_risk": True, "reload": False})
    assert json.loads((quirks_dir / "xquirks_options.json").read_text()) == {"excluded": [], "high_risk": True}
    assert zha.reloads == 1


async def test_the_options_say_that_their_file_is_broken(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    """No defaults in its place: saving them would replace the file."""
    broken = f'{{"excluded": ["{IEEE_A}",]}}\n'
    (quirks_dir / "xquirks_options.json").write_text(broken)
    entry = await setup_entry(hass)
    result = await options(hass, entry)
    assert result["reason"] == "options_broken" and "xquirks_options.json" in result["description_placeholders"]["error"]
    assert (quirks_dir / "xquirks_options.json").read_text() == broken


async def test_the_options_need_the_folder(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    entry = await setup_entry(hass)
    zha.data.yaml_config.pop("custom_quirks_path")
    assert (await options(hass, entry))["reason"] == "no_folder"
    assert list(quirks_dir.iterdir()) == []
