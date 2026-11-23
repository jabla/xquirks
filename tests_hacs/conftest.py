"""Fixtures: a stand-in for ZHA (its YAML config, its loaded entry, its devices with the quirk each runs, its cluster
command service) and the xquirks entry."""

from __future__ import annotations

import asyncio
import json
import string
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import zigpy.device
import zigpy.types as t
from homeassistant.components.zha.helpers import HAZHAData
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, MockModule, mock_integration

from .integration import DOMAIN, FOLDER, const

STRINGS = json.loads((FOLDER / "strings.json").read_text())
# zha's quirk_class of a device without a quirk: its zigpy class path.
NO_QUIRK = "zigpy.device.Device"


def label(ident: str, rev: str = "1a2b3c4d") -> str:
    """zha's quirk_class of a device on that definition: the stub's module, then the loader's label."""
    return f"xquirks:{const.LABEL_PREFIX}{ident} {rev}"


def zigpy_device(ieee: str, manufacturer: str, model: str) -> zigpy.device.Device:
    """The zigpy device behind ZHA's device: endpoint 1 with the Basic cluster."""
    device = zigpy.device.Device(MagicMock(), t.EUI64.convert(ieee), 0x1234)
    endpoint = device.add_endpoint(1)
    endpoint.profile_id, endpoint.device_type = 260, 0x0100
    endpoint.add_input_cluster(0x0000)
    device.manufacturer, device.model = manufacturer, model
    return device


@pytest.fixture(autouse=True)
def config_dir(hass: HomeAssistant, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A config folder of the test's own (outside tmp_path)."""
    folder = tmp_path_factory.mktemp("config")
    hass.config.config_dir = str(folder)
    return folder


@pytest.fixture
def quirks_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "custom_zha_quirks"
    folder.mkdir()
    return folder


def render(template: str, placeholders: dict | None) -> str:
    """A translation filled in as the frontend fills it; KeyError when a placeholder is missing."""
    return template.format(**(placeholders or {}))


def fields(template: str) -> set[str]:
    return {name for _literal, name, _spec, _conv in string.Formatter().parse(template) if name}


@pytest.fixture(autouse=True)
def issues_render(hass: HomeAssistant):
    """Every issue a test leaves behind has a translation that renders with its placeholders."""
    yield
    for (domain, _issue_id), issue in ir.async_get(hass).issues.items():
        if domain == DOMAIN and issue.active:
            texts = STRINGS["issues"][issue.translation_key]
            render(texts["title"], issue.translation_placeholders)
            if not issue.is_fixable:
                render(texts["description"], issue.translation_placeholders)


def check_page(texts: dict, result: dict) -> dict:
    """A form, menu or abort renders with the placeholders it got, its fields and options have texts, no placeholder
    goes unused."""
    if result["type"] == "abort":
        # The frontend takes the text from translation_domain when there is one (Home Assistant's single_instance_allowed).
        if result.get("translation_domain") in (None, DOMAIN):
            render(texts["abort"][result["reason"]], result.get("description_placeholders"))
        return result
    if result["type"] not in ("form", "menu"):
        return result
    step = texts["step"][result["step_id"]]
    render(step["description"], result["description_placeholders"])
    assert not set(result.get("description_placeholders") or {}) - fields(step["description"])
    for option in result.get("menu_options", []):
        assert step["menu_options"][option]
    if result["type"] == "form" and result.get("data_schema"):
        for name in result["data_schema"].schema:
            assert step["data"][str(name)]
    return result


@dataclass
class Zha:
    """The parts of ZHA the integration reads: its YAML config, its loaded entry, its devices and the cluster command
    service. A reload is played with reload() (or start_reload and finish_reload); the integration's requests only
    count in reloads."""

    hass: HomeAssistant
    entry: MockConfigEntry
    data: HAZHAData
    quirks: dict[str, str] = field(default_factory=dict)  # ieee -> quirk_class
    names: dict[str, tuple[str, str]] = field(default_factory=dict)  # ieee -> manufacturer and model as zigpy has them
    tuya: set[str] = field(default_factory=set)  # devices with zhaquirks' Tuya MCU cluster on endpoint 1
    zigpy: dict[str, Any] = field(default_factory=dict)  # ieee -> the zigpy device behind each device but the Tuya ones
    unavailable: set[str] = field(default_factory=set)
    errors: dict[str, Exception] = field(default_factory=dict)  # ieee -> what the cluster command service raises
    calls: list[dict] = field(default_factory=list)  # the data of each cluster command service call
    hold: asyncio.Event | None = None  # the cluster command service waits for it
    reloads: int = 0

    def add_device(self, ieee: str, manufacturer: str = "Acme", model: str = "A-1", name: str | None = None, quirk: str = NO_QUIRK, *, tuya: bool = False) -> str:
        """As ZHA adds a device: its device first, then its registry device."""
        self.quirks[ieee], self.names[ieee] = quirk, (manufacturer, model)
        if tuya:
            self.tuya.add(ieee)
        else:
            self.zigpy[ieee] = zigpy_device(ieee, manufacturer, model)
        self._publish()
        device = dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id, identifiers={("zha", ieee)}, manufacturer=manufacturer, model=model, name=name or f"{manufacturer} {model}",
        )
        return device.id

    def remove_device(self, ieee: str) -> None:
        """As ZHA removes a device: its device first, then its registry device."""
        del self.quirks[ieee]
        self._publish()
        registry = dr.async_get(self.hass)
        registry.async_remove_device(registry.async_get_device_by_identifier(("zha", ieee), self.entry.entry_id).id)

    def swap(self, ieee: str, quirk: str, names: tuple[str, str] | None = None) -> None:
        """As ZHA when another quirk matches at a re-interview (Reconfigure, an OTA update): it swaps the device object
        without a start. The device registry sees at most an update."""
        self.quirks[ieee], self.names[ieee] = quirk, names or self.names[ieee]
        if ieee in self.zigpy:
            self.zigpy[ieee].manufacturer, self.zigpy[ieee].model = self.names[ieee]
        self._publish()

    def _publish(self, *, loading: bool = False) -> None:
        if loading or self.entry.state is ConfigEntryState.LOADED:
            self.data.gateway_proxy = SimpleNamespace(device_proxies={ieee: SimpleNamespace(device=self._device(ieee)) for ieee in self.quirks})

    def _device(self, ieee: str) -> SimpleNamespace:
        from zhaquirks.tuya.mcu import TuyaMCUCluster

        manufacturer, model = self.names[ieee]
        device = SimpleNamespace(ieee=ieee, quirk_class=self.quirks[ieee], manufacturer=manufacturer, model=model, available=ieee not in self.unavailable)
        if ieee in self.tuya:
            cluster = SimpleNamespace(server_commands=TuyaMCUCluster.server_commands)
            device.device = SimpleNamespace(endpoints={0: SimpleNamespace(), 1: SimpleNamespace(in_clusters={0xEF00: cluster})})
        else:
            device.device = self.zigpy[ieee]
        return device

    def start_reload(self) -> None:
        """ZHA unloads: no gateway until finish_reload."""
        self.data.gateway_proxy = None
        self.entry.mock_state(self.hass, ConfigEntryState.NOT_LOADED)

    def finish_reload(self) -> None:
        """ZHA is loaded again with the quirks as they stand now: its setup sets the devices up, then the entry is
        loaded (each state change tells the integration)."""
        self.entry.mock_state(self.hass, ConfigEntryState.SETUP_IN_PROGRESS)
        self._publish(loading=True)
        self.entry.mock_state(self.hass, ConfigEntryState.LOADED)

    async def reload(self, quirks: dict[str, str] | None = None) -> None:
        """A whole reload of ZHA that applies these quirks (ieee -> quirk_class)."""
        self.start_reload()
        self.quirks.update(quirks or {})
        self.finish_reload()
        await self.hass.async_block_till_done(wait_background_tasks=True)

    async def _command(self, call: ServiceCall) -> None:
        self.calls.append(dict(call.data))
        if self.hold is not None:
            await self.hold.wait()
        if (error := self.errors.get(call.data["ieee"])) is not None:
            raise error


@pytest.fixture
async def zha(hass: HomeAssistant, quirks_dir: Path):
    """ZHA stand-in: keeps its YAML config in hass.data as the real async_setup does, its entry loaded."""

    async def async_setup(hass: HomeAssistant, config: dict) -> bool:
        hass.data["zha"] = HAZHAData(yaml_config=config.get("zha", {}))
        return True

    mock_integration(hass, MockModule("zha", async_setup=async_setup))
    assert await async_setup_component(hass, "zha", {"zha": {"custom_quirks_path": str(quirks_dir)}})
    assert await async_setup_component(hass, "repairs", {})
    entry = MockConfigEntry(domain="zha")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    zha = Zha(hass, entry, hass.data["zha"])
    zha._publish()
    hass.services.async_register("zha", "issue_zigbee_cluster_command", zha._command)
    schedule_reload = hass.config_entries.async_schedule_reload

    def reload(entry_id: str) -> None:
        if entry_id == entry.entry_id:
            zha.reloads += 1
        else:
            schedule_reload(entry_id)

    with patch.object(hass.config_entries, "async_schedule_reload", reload):
        yield zha


async def setup_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="xquirks")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


def our_issues(hass: HomeAssistant) -> dict[str, ir.IssueEntry]:
    """The issues the user sees in Repairs: active and not ignored."""
    return {issue_id: issue for (domain, issue_id), issue in ir.async_get(hass).issues.items() if domain == DOMAIN and issue.active and issue.dismissed_version is None}


async def start_fix(hass: HomeAssistant, issue_id: str) -> dict:
    result = await hass.data["repairs"]["flow_manager"].async_init(DOMAIN, context={"issue_id": issue_id})
    return check_page(STRINGS["issues"][issue_id]["fix_flow"], result)


async def continue_fix(hass: HomeAssistant, flow: dict, issue_id: str, user_input: dict | None = None) -> dict:
    result = await hass.data["repairs"]["flow_manager"].async_configure(flow["flow_id"], user_input)
    return check_page(STRINGS["issues"][issue_id]["fix_flow"], result)
