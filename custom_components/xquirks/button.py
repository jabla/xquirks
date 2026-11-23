"""The buttons on the page of each ZHA device that runs an xquirks definition: "Report a problem (xquirks)", and
"Confirm it works (xquirks)" while the definition's revision is not verified. A press asks GitHub's issue search for
open issues of the definition the device runs at that moment and shows a notification: the open issues for a
thumbs-up, the issue form filled in when there are none, both links when the search fails. Only the definition's id
and revision go to GitHub."""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import re
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import translation
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.loader import async_get_integration
from homeassistant.util.json import json_loads

from . import NOTIFICATIONS, XquirksConfigEntry
from .const import DOMAIN, REPORT, REPOSITORY, WORKS
from .devices import definition, label_of, registry_device, zha_devices
from .diagnostics import ISSUES, entity_values, report_url, search_url, search_words

_LOGGER = logging.getLogger(__name__)

DEFINITIONS = pathlib.Path(__file__).with_name("definitions.jsonl")
SEARCH_API = "https://api.github.com/search/issues"
# The User-Agent in place of the shared session's, which names the versions of Home Assistant, aiohttp and Python.
SEARCH_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "xquirks"}
# Seconds a press waits for the search.
SEARCH_TIMEOUT = 10
# Bytes of an answer at most. Its one issue holds its whole text, which GitHub caps at 65,536 characters.
MAX_ANSWER = 1024 * 1024
# The id and the revision of a definition as words of the search: no qualifier, operator or quote can widen it.
WORD = re.compile(r"[a-z0-9_]{1,100}")


def verified_revisions(path: pathlib.Path) -> dict[str, str]:
    """Definition id -> its revision verified on a device, from the definitions this release ships. Only lines that name
    verified are parsed. Empty without the file."""
    try:
        with path.open("rb") as file:
            lines = [json.loads(line) for line in file if b'"verified"' in line]
    except (OSError, ValueError):
        return {}
    return {item["id"]: item["rev"] for item in lines if item.get("verified") is True}


async def async_find(hass: HomeAssistant, kind: str, ident: str, rev: str) -> str | None:
    """GitHub's search without a token for the open issues of the kind for the definition in REPOSITORY, by the words of
    the search link and with the advanced search of its page (the default search finds the words of a phrase in any
    order and with other words between them): the link to the issue it finds, to the search list for several, "" for
    none. None when the search fails: no answer within SEARCH_TIMEOUT, another status than 200 (403 and 429 for the
    rate limit of 10 searches a minute), more than MAX_ANSWER bytes or no complete answer of the expected types. Raises
    nothing but CancelledError."""
    if not (WORD.fullmatch(ident) and WORD.fullmatch(rev)):
        return None
    params = {"q": f"repo:{REPOSITORY} {search_words(kind, ident, rev)}", "per_page": "1", "advanced_search": "true"}
    try:
        async with asyncio.timeout(SEARCH_TIMEOUT):
            async with async_get_clientsession(hass).get(SEARCH_API, params=params, headers=SEARCH_HEADERS, allow_redirects=False) as response:
                if response.status != 200:
                    _LOGGER.debug("GitHub's issue search answered HTTP %s", response.status)
                    return None
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body += chunk
                    if len(body) > MAX_ANSWER:
                        _LOGGER.debug("GitHub's issue search answered more than %s bytes", MAX_ANSWER)
                        return None
        answer = json_loads(body)
    except Exception as err:  # noqa: BLE001 - whatever failed, the notification offers both links
        _LOGGER.debug("GitHub's issue search failed: %r", err)
        return None
    return _link(kind, ident, rev, answer)


def _link(kind: str, ident: str, rev: str, answer: object) -> str | None:
    """What an answer of one item per page says (async_find), from total_count, incomplete_results and the item's number
    only. The links are built here, none is taken from the answer."""
    if not isinstance(answer, dict) or answer.get("incomplete_results") is not False:
        return None
    total, items = answer.get("total_count"), answer.get("items")
    if not _is_int(total) or total < 0 or not isinstance(items, list) or len(items) != min(total, 1):
        return None
    if total == 0:
        return ""
    if total > 1:
        return search_url(kind, ident, rev)
    number = items[0].get("number") if isinstance(items[0], dict) else None
    return f"{ISSUES}/{number}" if _is_int(number) and 0 < number < 2**31 else None


def _is_int(value: object) -> bool:
    """An int that is no bool."""
    return isinstance(value, int) and not isinstance(value, bool)


async def async_setup_entry(hass: HomeAssistant, entry: XquirksConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    entry.runtime_data.buttons = Buttons(hass, entry, async_add_entities, await hass.async_add_executor_job(verified_revisions, DEFINITIONS))
    entry.runtime_data.buttons.sync()


class Buttons:
    """The buttons of the entry: synced at each ZHA start and when a device joins or goes (__init__.py)."""

    def __init__(self, hass: HomeAssistant, entry: XquirksConfigEntry, add: AddConfigEntryEntitiesCallback, verified: dict[str, str]) -> None:
        self.hass, self.entry, self._add, self._verified = hass, entry, add, verified
        # Unique id -> the button added since setup (a button the user disabled stays here, not in Home Assistant).
        self._buttons: dict[str, XquirksButton] = {}

    @callback
    def sync(self) -> None:
        """Adds the buttons the devices on a definition lack and removes from the entity registry every other button of
        the entry. While ZHA's devices cannot be read, only the buttons whose device is gone go."""
        devices = zha_devices(self.hass)
        wanted: dict[str, tuple[str, str, dr.DeviceEntry]] = {}
        for ieee, device in (devices or {}).items():
            if (label := label_of(device)) is None or (registered := registry_device(self.hass, ieee)) is None:
                continue
            ident, rev = definition(label)
            for kind in (REPORT, WORKS) if self._verified.get(ident) != rev else (REPORT,):
                wanted[f"{ieee}-{kind}"] = (ieee, kind, registered)
        registry, device_registry = er.async_get(self.hass), dr.async_get(self.hass)
        for item in er.async_entries_for_config_entry(registry, self.entry.entry_id):
            # A device removed leaves the entities of other entries without one.
            gone = item.device_id is None or device_registry.async_get(item.device_id) is None
            if gone or (devices is not None and item.unique_id not in wanted):
                self._buttons.pop(item.unique_id, None)
                registry.async_remove(item.entity_id)
        new = []
        for unique_id, (ieee, kind, registered) in wanted.items():
            if unique_id not in self._buttons:
                self._buttons[unique_id] = XquirksButton(ieee, kind, registered)
                new.append(self._buttons[unique_id])
        if new:
            self._add(new)


class XquirksButton(ButtonEntity):
    """A button of this entry on the page of ZHA's device (device_entry): HA links it to a device it does not own."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, ieee: str, kind: str, device: dr.DeviceEntry) -> None:
        self._attr_unique_id = f"{ieee}-{kind}"
        self._attr_translation_key = kind
        self.device_entry = device
        self.ieee, self.kind = ieee, kind
        self._searching = False

    async def async_press(self) -> None:
        """One notification per button: a new press replaces it with the definition and the values of that moment and
        what the search finds then. A press while the button still searches does nothing: the notification of the
        press before comes within SEARCH_TIMEOUT. A re-interview may have put the device on another definition or none:
        the buttons only follow at the next ZHA start. On none or while ZHA reloads, the press fails with a message."""
        if self._searching:
            return
        if self.registry_entry is None:
            # Gone before the press began: a call to several buttons starts each press later, in a task of its own.
            return
        zha_device = (zha_devices(self.hass) or {}).get(self.ieee)
        if zha_device is None or (label := label_of(zha_device)) is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_definition")
        self._searching = True
        try:
            await self._async_notify(zha_device, label)
        finally:
            self._searching = False

    async def _async_notify(self, zha_device: Any, label: str) -> None:
        ident, rev = definition(label)
        manufacturer, model = str(zha_device.manufacturer), str(zha_device.model)
        device = dr.async_get(self.hass).async_get(self.registry_entry.device_id or "")
        firmware = device.sw_version if device else None
        values = entity_values(self.hass, self.ieee, device.id) if device else {}
        version = (await async_get_integration(self.hass, DOMAIN)).version
        form = report_url(self.kind, manufacturer, model, ident, rev, f"xquirks {version}, firmware {firmware or 'unknown'}", values)
        found = await async_find(self.hass, self.kind, ident, rev)
        texts = await translation.async_get_translations(self.hass, self.hass.config.language, "common", {DOMAIN})
        if (registry_entry := self.registry_entry) is None or self.hass.config_entries.async_get_entry(registry_entry.config_entry_id or "") is None:
            # The button (with its device or at a ZHA start) or the entry (with its notifications) went during the
            # search or while the texts loaded.
            return
        key, notification_id = f"component.{DOMAIN}.common.{self.kind}", f"{DOMAIN}_{registry_entry.id}"
        if found is None:
            message = texts[f"{key}_unknown"].format(search=search_url(self.kind, ident, rev), form=form)
        elif found:
            message = texts[f"{key}_found"].format(link=found)
        else:
            message = texts[f"{key}_new"].format(form=form)
        self.hass.data.setdefault(NOTIFICATIONS, set()).add(notification_id)
        persistent_notification.async_create(self.hass, message, texts[f"{key}_title"].format(definition=ident), notification_id)
