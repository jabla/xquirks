"""Diagnostics: no personal data, each device on xquirks with its values and a link to the filled-in report form."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.components.diagnostics import _get_diagnostics_for_config_entry
from pytest_homeassistant.typing import ClientSessionGenerator

from .conftest import Zha, label, our_issues, setup_entry
from .integration import ROOT, const, diagnostics, install

IEEE_A = "00:11:22:33:44:55:66:01"
IEEE_B = "00:11:22:33:44:55:66:02"
IEEE_C = "00:11:22:33:44:55:66:03"
FORM = ROOT / ".github" / "ISSUE_TEMPLATE" / diagnostics.FORMS["report"][0]


def add_entity(hass: HomeAssistant, zha: Zha, device_id: str, domain: str, unique_id: str, state: str | None, unit: str | None = None, **kwargs) -> str:
    entry = er.async_get(hass).async_get_or_create(domain, "zha", unique_id, config_entry=zha.entry, device_id=device_id, **kwargs)
    if state is not None:
        hass.states.async_set(entry.entity_id, state, {"unit_of_measurement": unit} if unit else {})
    return entry.entity_id


def query_of(url: str) -> dict[str, str]:
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == diagnostics.NEW_ISSUE
    return {key: values[0] for key, values in parse_qs(parts.query, keep_blank_values=True).items()}


async def test_a_device_on_xquirks_comes_with_its_values_and_a_filled_in_report(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    device_id = zha.add_device(IEEE_A, "_TZE200_aaaa", "TS0601", "Living room radar", label("tuya_radar"))
    dr.async_get(hass).async_update_device(device_id, sw_version="0x00000046")
    add_entity(hass, zha, device_id, "sensor", f"{IEEE_A}-1-1026", "21.5", "°C")
    add_entity(hass, zha, device_id, "switch", f"{IEEE_A}-1-led|indicator", "off")
    add_entity(hass, zha, device_id, "number", f"{IEEE_A}-1-fading_time", None, disabled_by=er.RegistryEntryDisabler.USER)
    add_entity(hass, zha, device_id, "select", f"{IEEE_A}-1-mode", None)
    await install.async_install(hass, quirks_dir)
    await install.async_set_options(hass, quirks_dir, excluded=[IEEE_B], high_risk=True)
    entry = await setup_entry(hass)
    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    (device,) = result["devices"]
    values = {"number 1-fading_time": "disabled", "select 1-mode": "no state", "sensor 1-1026": "21.5 °C", "switch 1-led|indicator": "off"}
    assert {key: value for key, value in device.items() if key != "report"} == {
        "definition": "tuya_radar", "revision": "1a2b3c4d", "manufacturer": "_TZE200_aaaa", "model": "TS0601", "firmware": "0x00000046", "values": values,
    }
    assert {key: value for key, value in result.items() if key != "devices"} == {
        "version": "0.0.0", "folder": "outside the config folder", "stub": "current", "options": {"excluded_devices": 1, "high_risk": True},
        "zha_devices": 1, "loader": {"loaded": False}, "tuya_data_queries": {},
    }
    fields = query_of(device["report"])
    assert fields == {
        "template": "wrong-value.yml", "title": "_TZE200_aaaa TS0601: ", "file": "tuya_radar", "revision": "1a2b3c4d", "ha": HA_VERSION,
        "diagnostics": "\n".join([
            "xquirks 0.0.0, firmware 0x00000046", "", "| Entity | Value |", "| --- | --- |", "| number 1-fading_time | disabled |", "| select 1-mode | no state |",
            "| sensor 1-1026 | 21.5 °C |", "| switch 1-led\\|indicator | off |",
        ]),
    }
    # A folder in the config folder shows as a path relative to it.
    hass.config.config_dir = str(quirks_dir.parent)
    assert (await diagnostics.async_get_config_entry_diagnostics(hass, entry))["folder"] == "custom_zha_quirks"
    # GitHub fills in the form's fields by these ids.
    form = yaml.safe_load(FORM.read_text())
    assert set(fields) - {"template", "title"} <= {item["id"] for item in form["body"] if "id" in item}


async def test_the_download_holds_no_address_and_no_name(hass: HomeAssistant, hass_client: ClientSessionGenerator, zha: Zha, quirks_dir: Path):
    """The file as Home Assistant's diagnostics view builds it, with the integration's issues: the notice names the
    devices."""
    area = ar.async_get(hass).async_create("Grandma's bedroom")
    for ieee, name, quirk in ((IEEE_A, "Living room radar", label("tuya_radar")), (IEEE_B, "Hallway lamp", "zhaquirks.tuya:Lamp")):
        device_id = zha.add_device(ieee, "_TZE200_aaaa", "TS0601", name, quirk)
        dr.async_get(hass).async_update_device(device_id, area_id=area.id, name_by_user=f"{name} of Erika")
        entity_id = add_entity(hass, zha, device_id, "sensor", f"{ieee}-1-1026", "21.5", "°C")
        er.async_get(hass).async_update_entity(entity_id, name="Erika's temperature")
    # A device ZHA has without a registry entry: the notice names its address.
    dr.async_get(hass).async_remove_device(zha.add_device(IEEE_C, "_TZE200_cccc", "TS0601", "Attic sensor", label("tuya_c")))
    await install.async_install(hass, quirks_dir)
    await install.async_set_options(hass, quirks_dir, excluded=[IEEE_B], high_risk=False)
    entry = await setup_entry(hass)
    shown = our_issues(hass)[const.CHANGED].translation_placeholders["devices"]
    assert "Living room radar of Erika" in shown and IEEE_C in shown
    payload = await _get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert const.CHANGED in [issue["issue_id"] for issue in payload["issues"]]
    text = json.dumps(payload)
    for secret in (IEEE_A, IEEE_B, IEEE_C, IEEE_A.replace(":", ""), "Living room", "Hallway", "Attic", "Erika", "Grandma", "sensor.", area.id):
        assert secret.lower() not in text.lower(), secret
    assert "tuya_radar" in text


async def test_a_long_report_counts_the_rows_past_the_limit(hass: HomeAssistant, zha: Zha):
    device_id = zha.add_device(IEEE_A, "_TZE200_aaaa", "TS0601", "Radar", label("tuya_radar"))
    for number in range(400):
        add_entity(hass, zha, device_id, "sensor", f"{IEEE_A}-1-value_{number:03}", str(number * 1000))
    entry = await setup_entry(hass)
    (device,) = (await diagnostics.async_get_config_entry_diagnostics(hass, entry))["devices"]
    assert len(device["values"]) == 400 and len(device["report"]) <= diagnostics.URL_LIMIT
    rows = query_of(device["report"])["diagnostics"].splitlines()
    listed = [row for row in rows if row.startswith("| sensor 1-value_")]
    assert listed and rows[-1] == f"| and {400 - len(listed)} more | in the diagnostics of xquirks |"


class OnDemand:
    def __init__(self, state: str) -> None:
        self.state = state


def entry_with(label_text: str, *filters) -> SimpleNamespace:
    return SimpleNamespace(source=SimpleNamespace(label=label_text), device_match=SimpleNamespace(filters=filters))


async def test_the_loader_says_what_it_registered_and_what_failed(hass: HomeAssistant, zha: Zha, monkeypatch: pytest.MonkeyPatch):
    """The loader ZHA imported through the stub: alias entries share a label, a stand-in carries its build state."""
    loader = types.ModuleType("xquirks_loader")
    loader.OnDemand = OnDemand
    loader.DEVICE_REGISTRY = [
        entry_with("xquirks:alias_a 11111111"), entry_with("xquirks:alias_a 11111111"), entry_with("xquirks:tuya_b 22222222", OnDemand("pending")),
        entry_with("xquirks:tuya_c 33333333", OnDemand("failed")), entry_with("xquirks:tuya_d 44444444", OnDemand("built")), entry_with("z2m_own:(None / X)"),
    ]
    monkeypatch.setitem(sys.modules, "xquirks_loader", loader)
    entry = await setup_entry(hass)
    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    assert result["loader"] == {"loaded": True, "definitions": 4, "built": 2, "failed": ["tuya_c 33333333"]}
