"""Config flow (one entry), the options (the devices xquirks leaves alone, high-risk files) and the subentry type of the
device check (test_checks.py, test_self_check_flow.py)."""

from __future__ import annotations

import importlib
from pathlib import Path

import orjson
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import MANIFEST, STRINGS, Zha, apply_list, issue_of, issues_of, listed, published, setup_entry, write_repo
from .integration import DOMAIN, PACKAGE, const

config_flow = importlib.import_module(f"{PACKAGE}.config_flow")

IEEE = "00:11:22:33:44:55:66:01"


async def settings(hass: HomeAssistant, entry) -> dict:
    """Configure: the settings form at once (the device check left the options)."""
    return await hass.config_entries.options.async_init(entry.entry_id)


async def test_user_flow_creates_the_entry(hass: HomeAssistant, zha: Zha, quirks_dir: Path, repo_dir: Path):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"] == {"folder": str(quirks_dir)}
    with published(repo_dir):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {}
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.state is config_entries.ConfigEntryState.LOADED

    # Only one entry.
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_user_flow_without_custom_quirks_path(hass: HomeAssistant, zha: Zha):
    zha.data.yaml_config.pop("custom_quirks_path")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["description_placeholders"] == {"folder": "not configured"}


async def test_options_list_the_devices_xquirks_leaves_alone(hass: HomeAssistant, zha: Zha, repo_dir: Path, quirks_dir: Path):
    """Saving the options plans again: a file only an excluded device runs goes with the next list."""
    device = zha.add_device(IEEE, "_TZE200_aaaa", "TS0601")
    entry = await setup_entry(hass, repo_dir)
    await apply_list(hass)
    zha.reload({IEEE: "z2m_tuya_a:TuyaA"})
    await entry.runtime_data.async_refresh()
    assert listed(hass)["install"] == listed(hass)["remove"] == []

    result = await settings(hass, entry)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "settings"
    assert set(result["data_schema"].schema) == {const.CONF_EXCLUDED, const.CONF_HIGH_RISK}
    assert set(STRINGS["options"]["step"]["settings"]["data"]) == {const.CONF_EXCLUDED, const.CONF_HIGH_RISK}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {const.CONF_EXCLUDED: [device]})
    await hass.async_block_till_done(wait_background_tasks=True)
    # High-risk files stay held back unless turned on.
    assert result["type"] is FlowResultType.CREATE_ENTRY and entry.options == {const.CONF_EXCLUDED: [device], const.CONF_HIGH_RISK: False}
    assert listed(hass)["remove"] == ["z2m_tuya_a.py"]
    # Nothing goes before the list is confirmed.
    assert (quirks_dir / "z2m_tuya_a.py").exists()

    # Taken off the list again: the file stays.
    result = await settings(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {const.CONF_EXCLUDED: []})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert listed(hass)["remove"] == []


async def test_the_options_offer_high_risk_files_only_when_turned_on(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """The toggle is off at first and keeps what was saved; saving plans again."""
    write_repo(repo_dir, {**MANIFEST, "tuya_a": {**MANIFEST["tuya_a"], "risk": "high", "risk_why": ["cover"]}})
    zha.add_device(IEEE, "_TZE200_aaaa", "TS0601")
    entry = await setup_entry(hass, repo_dir)
    assert listed(hass)["install"] == [] and issue_of(hass, "held_back", "z2m_tuya_a.py")

    result = await settings(hass, entry)
    defaults = {str(key): key.default() for key in result["data_schema"].schema}
    assert defaults == {const.CONF_EXCLUDED: [], const.CONF_HIGH_RISK: False}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {const.CONF_HIGH_RISK: True})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.options == {const.CONF_EXCLUDED: [], const.CONF_HIGH_RISK: True}
    assert listed(hass)["install"] == ["z2m_tuya_a.py"] and issues_of(hass, "held_back") == []

    result = await settings(hass, entry)
    assert {str(key): key.default() for key in result["data_schema"].schema}[const.CONF_HIGH_RISK] is True


async def test_configure_shows_only_the_settings(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """No menu: Configure opens the settings form. A step of the device check is no step of the options."""
    entry = await setup_entry(hass, repo_dir)
    result = await settings(hass, entry)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "settings"
    handler = hass.config_entries.options._progress[result["flow_id"]]
    assert not [name for name in dir(handler) if name.startswith("async_step_") and name not in ("async_step_init", "async_step_settings")]
    assert set(STRINGS["options"]) == {"step"} and set(STRINGS["options"]["step"]) == {"settings"}


async def test_the_device_check_is_the_one_subentry_type(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """The integration page shows a button and a three-dot menu item for each subentry type: "Check a device". It has no
    reconfigure step (Home Assistant would offer one on each subentry)."""
    entry = await setup_entry(hass, repo_dir)
    flow = config_flow.QuirksConfigFlow
    assert flow.async_get_supported_subentry_types(entry) == {"device_check": config_flow.DeviceCheckFlow}
    assert entry.supported_subentry_types == {"device_check": {"supports_reconfigure": False}}
    # What the frontend gets for the integration page.
    assert b'"supported_subentry_types":{"device_check":{"supports_reconfigure":false}}' in orjson.dumps(entry.as_json_fragment)
    assert STRINGS["config_subentries"]["device_check"]["initiate_flow"] == {"user": "Check a device"}
    assert entry.subentries == {}


def test_the_options_step_says_what_xquirks_offers():
    step = STRINGS["options"]["step"]["settings"]
    assert step["title"] == "What xquirks offers"
    assert step["data"][const.CONF_HIGH_RISK] == "Offer high-risk files"
    assert "**Offer high-risk files**: xquirks holds back files for covers and thermostats" in step["description"]
