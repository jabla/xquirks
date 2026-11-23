"""ZHA's devices as xquirks reads them: the definition or quirk each one runs, the definitions the high-risk option
holds back, a device's name and its Tuya data query."""

from __future__ import annotations

import copy
import dataclasses
import sys
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from zha.quirks import DEVICE_REGISTRY

from .const import LABEL_PREFIX, TUYA_CLUSTER, TUYA_QUERY_DATA, ZHA_DOMAIN

# The module the stub loads loader.py as.
LOADER = "xquirks_loader"


@callback
def zha_devices(hass: HomeAssistant) -> dict[str, Any] | None:
    """IEEE address -> ZHA's device; None while ZHA is not loaded or its devices cannot be read."""
    if not hass.config_entries.async_loaded_entries(ZHA_DOMAIN):
        return None
    try:
        return {str(ieee): proxy.device for ieee, proxy in hass.data[ZHA_DOMAIN].gateway_proxy.device_proxies.items()}
    except (AttributeError, KeyError, TypeError):
        return None


def label_of(device: Any) -> str | None:
    """The definition ZHA applied to the device as "xquirks:<id> <rev>"; None for another quirk or none."""
    quirk = getattr(device, "quirk_class", None)
    label = quirk.partition(":")[2] if isinstance(quirk, str) else ""
    return label if label.startswith(LABEL_PREFIX) else None


def definition(label: str) -> tuple[str, str]:
    """The id and revision of a label."""
    ident, _, rev = label.removeprefix(LABEL_PREFIX).partition(" ")
    return ident, rev


def other_quirk(device: Any) -> str | None:
    """Whose quirk the device runs when it is no xquirks definition: "zha" or "own" (the user's). None for none: ZHA
    then names the device's zigpy class, not "<module>:<label>" of a quirk."""
    quirk = getattr(device, "quirk_class", None)
    if not isinstance(quirk, str) or ":" not in quirk or label_of(device) is not None:
        return None
    return "zha" if quirk.partition(":")[0].split(".")[0] == "zhaquirks" else "own"


@callback
def loader_entries() -> dict[tuple[str | None, str | None], list[Any]]:
    """(manufacturer, model) -> the loader's entries registered under it in ZHA's quirk registry (None matches any)."""
    entries: dict[tuple[str | None, str | None], list[Any]] = {}
    for entry in DEVICE_REGISTRY:
        label = getattr(getattr(entry, "source", None), "label", None)
        if isinstance(label, str) and label.startswith(LABEL_PREFIX):
            for pair in entry.device_match.applies_to:
                entries.setdefault(tuple(pair), []).append(entry)
    return entries


@callback
def loader_checks(entries: dict[tuple[str | None, str | None], list[Any]], device: Any) -> list[tuple[Any, Any]]:
    """The loader's check (its StepBack) of each definition registered under the zigpy device's names, with the rest of
    that definition's match. A definition ZHA did not try yet is built now, one that fails to build has none."""
    loader, zigpy = sys.modules.get(LOADER), getattr(device, "device", None)
    if loader is None or zigpy is None:
        return []
    checks = []
    for key in ((zigpy.manufacturer, zigpy.model), (zigpy.manufacturer, None), (None, zigpy.model)):
        for entry in entries.get(key, ()):
            match = entry.device_match
            if (holder := next((f for f in match.filters if isinstance(f, loader.OnDemand)), None)) is not None:
                if (built := holder.entry()) is None:
                    continue
                match = built.device_match
            if (check := next((f for f in match.filters if isinstance(f, loader.StepBack)), None)) is not None:
                checks.append((dataclasses.replace(match, filters=tuple(f for f in match.filters if f is not check)), check))
    return checks


def needs_high_risk(checks: list[tuple[Any, Any]], device: Any) -> bool:
    """Whether only the high-risk option holds a definition back from the device: its match and the loader's own check,
    run with the option on, let it apply. Never on a quirk of the user's, on one of ZHA's only for a take-over."""
    if not checks or (quirk := other_quirk(device)) == "own":
        return False
    options = sys.modules[LOADER].Options
    for match, check in checks:
        if not check.high_risk or check.options.high_risk or (quirk == "zha" and not check.take_over):
            continue
        probe = copy.copy(check)
        probe.options = options(check.options.excluded, True)
        try:
            if match.matches(device.device) and probe.reason(device.device) is None:
                return True
        except Exception:  # noqa: BLE001 - as in the loader: a check that fails steps back
            continue
    return False


@callback
def registry_device(hass: HomeAssistant, ieee: str) -> dr.DeviceEntry | None:
    registry = dr.async_get(hass)
    for entry in hass.config_entries.async_entries(ZHA_DOMAIN):
        if (device := registry.async_get_device_by_identifier((ZHA_DOMAIN, ieee), entry.entry_id)) is not None:
            return device
    return None


@callback
def name_of(hass: HomeAssistant, ieee: str) -> str:
    entry = registry_device(hass, ieee)
    return (entry.name_by_user or entry.name or ieee) if entry else ieee


def query_endpoint(device: Any) -> int | None:
    """The endpoint whose Tuya MCU cluster takes the data query: the applied quirk names the command query_data. None
    for a device without one."""
    endpoints = getattr(getattr(device, "device", None), "endpoints", None)
    if not isinstance(endpoints, dict):
        return None
    for ep_id in sorted(ep for ep in endpoints if isinstance(ep, int) and ep):
        commands = getattr(getattr(endpoints[ep_id], "in_clusters", {}).get(TUYA_CLUSTER), "server_commands", None)
        if isinstance(commands, dict) and getattr(commands.get(TUYA_QUERY_DATA), "name", None) == "query_data":
            return ep_id
    return None


@callback
def reload_zha(hass: HomeAssistant) -> bool:
    """Reload ZHA: it imports its custom quirks again. A setup in progress imported them already, it reloads once done.
    ZHA not loaded imports them at its next start (each retry of a failed setup too): False, nothing to reload."""
    reloading = False
    for entry in hass.config_entries.async_entries(ZHA_DOMAIN):
        if entry.state in (ConfigEntryState.LOADED, ConfigEntryState.SETUP_IN_PROGRESS):
            hass.config_entries.async_schedule_reload(entry.entry_id)
            reloading = True
    return reloading
