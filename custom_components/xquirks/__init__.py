"""xquirks: device definitions for ZHA. A stub in ZHA's custom quirks folder loads them when ZHA imports its custom
quirks (install.py, loader.py, definitions.jsonl).

The integration keeps the stub and its options, raises the repair issues and notes after each ZHA start which devices
run another definition than at the start before or wait for the high-risk option since then. A Tuya device on a new or
changed definition gets one data query: such a device sends its values by itself only when it joins. Each device on a
definition gets the report buttons (button.py)."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntry, ConfigEntryChange, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import translation
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from . import install
from .const import CHANGED, DOMAIN, LABEL_PREFIX, NO_FOLDER, OPTIONS_ISSUE, OPTIONS_PAGE, QUIRKS_OFF, STUB_ISSUE, TUYA_CLUSTER, TUYA_QUERY_DATA, ZHA_DOMAIN
from .devices import definition, label_of, loader_checks, loader_entries, name_of, needs_high_risk, other_quirk, query_endpoint, registry_device, zha_devices

if TYPE_CHECKING:
    from .button import Buttons

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = [Platform.BUTTON]
STORAGE_KEY = f"{DOMAIN}.labels"
STORAGE_VERSION = 1
# The notifications the buttons showed since Home Assistant started (it keeps none across a restart). They stay through
# a reload and when a device's buttons go, until the entry is removed.
NOTIFICATIONS: HassKey[set[str]] = HassKey(f"{DOMAIN}_notifications")
# What the notice says of a device: a definition it runs since the start before, or why it runs none (the store keeps
# that as its label now). Each ends the translation key of the device's line, common.notice_<what>.
NEW = "new"
WHY = ("high_risk", "own", "zha", "excluded", "gone")

type XquirksConfigEntry = ConfigEntry[Xquirks]


async def async_setup_entry(hass: HomeAssistant, entry: XquirksConfigEntry) -> bool:
    entry.runtime_data = Xquirks(hass, entry)
    await entry.runtime_data.async_setup()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: XquirksConfigEntry) -> bool:
    # No button comes or goes from here on.
    entry.runtime_data.buttons = None
    await entry.runtime_data.async_save()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: XquirksConfigEntry) -> None:
    """The stub, the options and the notifications of the buttons go: ZHA's next start loads no definition. The issues
    and the buttons go with the entry."""
    for notification_id in hass.data.pop(NOTIFICATIONS, set()):
        persistent_notification.async_dismiss(hass, notification_id)
    if (folder := install.custom_quirks_dir(hass)) is not None:
        try:
            await install.async_remove(hass, folder)
        except OSError as err:
            _LOGGER.warning("Could not delete the xquirks files in %s: %s", folder, err)
    await Store(hass, STORAGE_VERSION, STORAGE_KEY).async_remove()


class Xquirks:
    """The entry at runtime."""

    def __init__(self, hass: HomeAssistant, entry: XquirksConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        # IEEE address -> label at the last ZHA start; -> [label before, label now or why it runs none] for each device the
        # open notice lists. The devices the high-risk option held back at the last ZHA start.
        self._labels: dict[str, str] = {}
        self._changes: dict[str, list[str | None]] = {}
        self._held: set[str] = set()
        # The texts of the notice's lines in Home Assistant's language at setup: while the language changes, Home
        # Assistant has none cached for the new one.
        self._texts: dict[str, str] = {}
        self._zha_up = False
        # The data queries of the last ZHA start while they run. Definition id -> what became of the last data query to
        # each of its devices (by manufacturer and model only).
        self._query: asyncio.Task | None = None
        self.queries: dict[str, list[str]] = {}
        # The buttons on the devices' pages while their platform is set up.
        self.buttons: Buttons | None = None

    async def async_setup(self) -> None:
        stored = await self._store.async_load() or {}
        self._labels, self._changes = dict(stored.get("labels", {})), dict(stored.get("changes", {}))
        self._held = set(stored.get("held_back", []))
        self._texts = await translation.async_get_translations(self.hass, self.hass.config.language, "common", {DOMAIN})
        if self._changes:
            self._notice()
        self.entry.async_on_unload(async_dispatcher_connect(self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, self._entry_changed))
        self.entry.async_on_unload(self.hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._device_changed))
        await self.async_check_files()
        self._zha_up = self._zha_loaded()

    @callback
    def _device_changed(self, event: Event[dr.EventDeviceRegistryUpdatedData]) -> None:
        """A device that joins gets its buttons, one removed takes them along. The sync waits until the event is through:
        only then does Home Assistant tell the entities of a removed device. An entity removed before would fail on it."""
        if event.data["action"] != "update":
            self.hass.loop.call_soon(self._sync_buttons)

    @callback
    def _sync_buttons(self) -> None:
        if self.buttons is not None:
            self.buttons.sync()

    async def async_check_files(self) -> None:
        """The issues of the custom quirks folder: none configured, quirks off in ZHA, a broken options file, the stub
        missing or not the one this release ships."""
        folder = install.custom_quirks_dir(self.hass)
        self._issue(NO_FOLDER, folder is None, ir.IssueSeverity.ERROR)
        self._issue(QUIRKS_OFF, not install.quirks_enabled(self.hass), ir.IssueSeverity.ERROR)
        error = ""
        if folder is not None:
            try:
                await install.async_read_options(self.hass, folder)
            except OSError as err:
                error = str(err)
        self._issue(OPTIONS_ISSUE, bool(error), ir.IssueSeverity.ERROR, placeholders={"error": error})
        stub = "current" if folder is None else await install.async_stub_state(self.hass, folder)
        self._issue(STUB_ISSUE, stub != "current", ir.IssueSeverity.ERROR, fixable=True)

    @callback
    def _issue(self, issue_id: str, active: bool, severity: ir.IssueSeverity, *, fixable: bool = False, placeholders: dict[str, str] | None = None) -> None:
        if active:
            ir.async_create_issue(
                self.hass, DOMAIN, issue_id, config_entry_id=self.entry.entry_id, is_fixable=fixable, severity=severity, translation_key=issue_id,
                translation_placeholders=placeholders,
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    @callback
    def _entry_changed(self, change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if entry.domain != ZHA_DOMAIN:
            return
        if entry.state is not ConfigEntryState.LOADED:
            # The queries of the start before would go to the devices of a gateway that is gone.
            self._zha_up = False
            if self._query is not None:
                self._query.cancel()
        elif not self._zha_up and (processed := self._zha_loaded()):
            self._zha_up = processed
            self.entry.async_create_background_task(self.hass, self.async_check_files(), "xquirks file check")

    @callback
    def _zha_loaded(self) -> bool:
        """ZHA imported its custom quirks and set its devices up: the notice of the devices that run another definition
        than at the ZHA start before or that the high-risk option holds back since then, a data query to each Tuya device
        on a new or changed definition, the buttons. False while ZHA is not loaded or its devices cannot be read."""
        if (devices := zha_devices(self.hass)) is None:
            return False
        labels = {ieee: label for ieee, device in devices.items() if (label := label_of(device)) is not None}
        # For each device on none, the loader's checks of the definitions registered for it: the options the loader read,
        # and whether only the high-risk option holds one back.
        entries = loader_entries()
        checks = {ieee: loader_checks(entries, device) for ieee, device in devices.items() if ieee not in labels}
        held = {ieee for ieee, found in checks.items() if needs_high_risk(found, devices[ieee])}
        notice = ir.async_get(self.hass).async_get_issue(DOMAIN, CHANGED)
        is_open = notice is not None and notice.active and notice.dismissed_version is None
        # An open notice keeps its devices, each from its label before that notice.
        before = {ieee: pair[0] for ieee, pair in self._changes.items()} if is_open else {}
        for ieee in self._labels.keys() | labels.keys():
            if self._labels.get(ieee) != labels.get(ieee):
                before.setdefault(ieee, self._labels.get(ieee))
        # Held back now, not at the start before: the first start, a device that joined.
        for ieee in held - self._held:
            before.setdefault(ieee, None)
        # A device back on its label before drops out, so does one ZHA no longer has. One held back stays.
        self._changes = {
            ieee: [was, labels.get(ieee) or _why(ieee, devices[ieee], checks[ieee], held)]
            for ieee, was in before.items()
            if ieee in devices and (was != labels.get(ieee) or ieee in held)
        }
        if self._changes:
            if not is_open:
                ir.async_delete_issue(self.hass, DOMAIN, CHANGED)
            self._notice()
        elif is_open:
            ir.async_delete_issue(self.hass, DOMAIN, CHANGED)
        due = [(ieee, devices[ieee], label) for ieee, label in sorted(labels.items()) if label != self._labels.get(ieee)]
        self._labels, self._held = labels, held
        self._store.async_delay_save(self._stored, 1)
        if due:
            self._query = self.entry.async_create_background_task(self.hass, self._async_query(due), "xquirks Tuya data query")
        self._sync_buttons()
        return True

    def _stored(self) -> dict[str, Any]:
        return {"labels": self._labels, "held_back": sorted(self._held), "changes": self._changes}

    async def async_save(self) -> None:
        """What the next notice compares with, saved now (a delayed save may be pending)."""
        await self._store.async_save(self._stored())

    @callback
    def _notice(self) -> None:
        """The notice of the devices in _changes. Not persistent: the diagnostics download holds the texts of a persistent
        issue. Setup raises it again from the store until OK in its dialog closes it (repairs.NoticeFlow). A dismissed
        one stays dismissed."""
        ir.async_create_issue(
            self.hass, DOMAIN, CHANGED, config_entry_id=self.entry.entry_id, is_fixable=True, severity=ir.IssueSeverity.WARNING, translation_key=CHANGED,
            translation_placeholders={"devices": self._notice_lines()},
        )

    @callback
    def async_close_notice(self) -> None:
        """OK in the notice: deletes the issue and empties the changes in the store. Only a change at a later ZHA start
        raises a new notice."""
        self._changes = {}
        ir.async_delete_issue(self.hass, DOMAIN, CHANGED)
        self._store.async_delay_save(self._stored, 0)

    @callback
    def _notice_lines(self) -> str:
        """One line per device: its name and in a few words what happened."""
        rows = sorted(((name_of(self.hass, ieee), now) for ieee, (_was, now) in self._changes.items()), key=lambda row: row[0].casefold())
        return "\n".join(self._line(name, now) for name, now in rows)

    def _line(self, name: str, now: str | None) -> str:
        """A label now is a new definition, anything else says why the device runs none."""
        what = NEW if now and now.startswith(LABEL_PREFIX) else now if now in WHY else "gone"
        texts = translation.async_get_cached_translations(self.hass, self.hass.config.language, "common", DOMAIN) or self._texts
        return texts[f"component.{DOMAIN}.common.notice_{what}"].format(name=_plain(name), options=OPTIONS_PAGE)

    async def _async_query(self, due: list[tuple[str, Any, str]]) -> None:
        """One data query to each Tuya device in due, one after the other. Best effort: a sleeping device may miss it
        and shows its values when it next reports."""
        asked: set[str] = set()
        for ieee, device, label in due:
            endpoint, entry = query_endpoint(device), registry_device(self.hass, ieee)
            if endpoint is None or entry is None or entry.disabled_by is not None:
                continue
            ident, what = definition(label)[0], f"{device.manufacturer} {device.model}"
            if ident not in asked:
                asked.add(ident)
                self.queries[ident] = []
            if not getattr(device, "available", False):
                self.queries[ident].append(f"{what}: skipped, ZHA reports it unavailable")
                continue
            data = {"ieee": ieee, "endpoint_id": endpoint, "cluster_id": TUYA_CLUSTER, "cluster_type": "in", "command": TUYA_QUERY_DATA, "command_type": "server", "params": {}}
            try:
                await self.hass.services.async_call(ZHA_DOMAIN, "issue_zigbee_cluster_command", data, blocking=True)
            except Exception as err:  # noqa: BLE001 - whatever went wrong, the next device is asked all the same
                _LOGGER.debug("Tuya data query to %s not delivered: %s", ieee, err)
                # Its kind only: an error's text may hold the address (ZHA's "Device with IEEE ... not found").
                self.queries[ident].append(f"{what}: not delivered ({type(err).__name__})")
            else:
                _LOGGER.debug("Sent a Tuya data query to %s", ieee)
                self.queries[ident].append(f"{what}: sent")


def _why(ieee: str, device: Any, checks: list[tuple[Any, Any]], held: set[str]) -> str:
    """Why a device runs no definition: excluded in the options the loader read, held back, the quirk it runs."""
    if any(ieee in check.options.excluded for _match, check in checks):
        return "excluded"
    return "high_risk" if ieee in held else other_quirk(device) or "gone"


def _plain(text: str) -> str:
    """A name as plain text in markdown, also at the start of a line: "1. OG" or "- Lamp" starts no list."""
    return re.sub(r"^(\d*)([-+.)])", r"\1\\\2", re.sub(r"([\\`*_\[\]<>#|~])", r"\\\1", " ".join(text.split())))
