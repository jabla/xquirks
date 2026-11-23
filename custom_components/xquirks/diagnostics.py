"""Diagnostics without personal data: no IEEE address, no name of a device or an area. Each device on an xquirks
definition comes with its values in Home Assistant and a link to the "Wrong or missing value" form filled in with
them. The buttons on the devices' pages (button.py) fill in their forms the same way."""

from __future__ import annotations

import os
import sys
from typing import Any
from urllib.parse import quote, urlencode

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_integration

from . import XquirksConfigEntry, install
from .const import DOMAIN, LABEL_PREFIX, REPORT, REPOSITORY, WORKS, ZHA_DOMAIN
from .devices import definition, label_of, registry_device, zha_devices

ISSUES = f"https://github.com/{REPOSITORY}/issues"
NEW_ISSUE = f"{ISSUES}/new"
# The issue form of each kind of report, the title it suggests and the label GitHub gives its issues.
FORMS = {REPORT: ("wrong-value.yml", "{}: ", "bug"), WORKS: ("works.yml", "{}: works", "verify")}
# GitHub refuses much longer links: the rows past it are counted instead of listed.
URL_LIMIT = 6000


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: XquirksConfigEntry) -> dict[str, Any]:
    version = str((await async_get_integration(hass, DOMAIN)).version)
    folder = install.custom_quirks_dir(hass)
    try:
        excluded, high_risk = await install.async_read_options(hass, folder) if folder else ([], False)
        options: dict[str, Any] | str = {"excluded_devices": len(excluded), "high_risk": high_risk}
    except OSError:
        # Without the error, which names the path.
        options = "unreadable"
    devices = zha_devices(hass)
    on_xquirks = [_device(hass, ieee, device, label, version) for ieee, device in (devices or {}).items() if (label := label_of(device))]
    return {
        "version": version,
        # Relative to the config folder: an absolute path may hold the name of the system's user.
        "folder": None if folder is None else os.path.relpath(folder, hass.config.config_dir) if folder.is_relative_to(hass.config.config_dir) else "outside the config folder",
        "stub": await install.async_stub_state(hass, folder) if folder else None,
        "options": options,
        "zha_devices": None if devices is None else len(devices),
        "loader": _loader(),
        "devices": sorted(on_xquirks, key=lambda item: (item["definition"], item["manufacturer"], item["model"])),
        "tuya_data_queries": dict(sorted(entry.runtime_data.queries.items())),
    }


def _loader() -> dict[str, Any]:
    """The loader ZHA imported through the stub: the definitions it registered, those that failed to build."""
    if (loader := sys.modules.get("xquirks_loader")) is None:
        return {"loaded": False}
    states: dict[str, str] = {}
    try:
        for item in loader.DEVICE_REGISTRY:
            label = getattr(item.source, "label", None)
            if isinstance(label, str) and label.startswith(LABEL_PREFIX):
                holder = next((f for f in item.device_match.filters if isinstance(f, loader.OnDemand)), None)
                states[label.removeprefix(LABEL_PREFIX)] = holder.state if holder else "built"
    except Exception as err:  # noqa: BLE001 - ZHA may be loading its quirks right now
        return {"loaded": True, "error": f"{type(err).__name__}: {err}"}
    return {"loaded": True, "definitions": len(states), "built": sum(state == "built" for state in states.values()),
            "failed": sorted(label for label, state in states.items() if state == "failed")}


@callback
def _device(hass: HomeAssistant, ieee: str, device: Any, label: str, version: str) -> dict[str, Any]:
    ident, rev = definition(label)
    entry = registry_device(hass, ieee)
    firmware = entry.sw_version if entry else None
    values = entity_values(hass, ieee, entry.id) if entry else {}
    manufacturer, model = str(device.manufacturer), str(device.model)
    return {
        "definition": ident, "revision": rev, "manufacturer": manufacturer, "model": model, "firmware": firmware, "values": values,
        "report": report_url(REPORT, manufacturer, model, ident, rev, f"xquirks {version}, firmware {firmware or 'unknown'}", values),
    }


@callback
def entity_values(hass: HomeAssistant, ieee: str, device_id: str) -> dict[str, str]:
    """Each ZHA entity of the device by its platform and unique id without the IEEE address: its state and unit."""
    values = {}
    for item in er.async_entries_for_device(er.async_get(hass), device_id, include_disabled_entities=True):
        if item.platform != ZHA_DOMAIN or not item.unique_id.startswith(f"{ieee}-"):
            continue
        state = hass.states.get(item.entity_id)
        if item.disabled_by is not None or state is None:
            value = "disabled" if item.disabled_by is not None else "no state"
        else:
            value = " ".join(str(part) for part in (state.state, state.attributes.get("unit_of_measurement")) if part is not None)
        values[f"{item.domain} {item.unique_id.removeprefix(f'{ieee}-')}"] = value
    return dict(sorted(values.items()))


def report_url(kind: str, manufacturer: str, model: str, ident: str, rev: str, versions: str, values: dict[str, str]) -> str:
    """The form of the kind filled in: the definition, Home Assistant's version and a table of the values, each value
    bounded. What is wrong is left to the user."""
    form, title, _label = FORMS[kind]
    names = f"{_line(manufacturer, 64)} {_line(model, 64)}"
    fields = {"template": form, "title": title.format(names), "file": _line(ident, 100), "revision": _line(rev, 16), "ha": HA_VERSION}
    rows = [f"| {_cell(key)} | {_cell(value)} |" for key, value in values.items()]
    for listed in range(len(rows), -1, -1):
        more = [f"| and {len(rows) - listed} more | in the diagnostics of xquirks |"] if listed < len(rows) else []
        table = "\n".join([_line(versions, 150), "", "| Entity | Value |", "| --- | --- |", *rows[:listed], *more])
        url = f"{NEW_ISSUE}?{urlencode(fields | {'diagnostics': table}, quote_via=quote, safe='')}"
        if len(url) <= URL_LIMIT:
            break
    return url


def search_words(kind: str, ident: str, rev: str) -> str:
    """The search for the open issues of the kind for the definition: the form's fields Definition and Revision as
    GitHub renders them in an issue's body, each label a heading over its value, as one phrase. A works report only at
    the revision: it confirms no other. A problem at any revision: it may outlive one. The heading Revision then ends
    the id, which GitHub's search splits at its underscores."""
    phrase = ["Definition", _line(ident, 100), "Revision", *([_line(rev, 16)] if kind == WORKS else [])]
    return " ".join(["is:issue", "is:open", f"label:{FORMS[kind][2]}", "in:body", f'"{" ".join(phrase)}"'])


def search_url(kind: str, ident: str, rev: str) -> str:
    """The list of the open issues of the kind for the definition (search_words)."""
    return f"{ISSUES}?{urlencode({'q': search_words(kind, ident, rev)}, quote_via=quote, safe='')}"


def _line(text: object, limit: int) -> str:
    """The text on one line, cut to limit characters."""
    line = " ".join(str(text).split())
    return line if len(line) <= limit else f"{line[:limit - 3]}..."


def _cell(text: str) -> str:
    """The text as a cell of a markdown table."""
    return _line(text, 100).replace("\\", "\\\\").replace("|", "\\|")
