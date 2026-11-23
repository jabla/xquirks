"""The buttons on the page of each device on an xquirks definition: when they come and go. The notification a press
creates from GitHub's search for open issues of the definition: the issue, the filled-in form or both."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
import yaml
from aiohttp import web
from homeassistant.components import persistent_notification
from homeassistant.components.persistent_notification import UpdateType
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker, AiohttpClientMockResponse

from .conftest import NO_QUIRK, STRINGS, Zha, label, setup_entry
from .integration import DOMAIN, FOLDER, ROOT, button, const, diagnostics

IEEE_A = "00:11:22:33:44:55:66:01"
IEEE_B = "00:11:22:33:44:55:66:02"
REV = "1a2b3c4d"
NEW_REV = "5e6f7a8b"
BOTH = ("report", "works")
API = "https://api.github.com/search/issues"
ISSUE_7 = f"{diagnostics.ISSUES}/7"
NO_ISSUE = {"total_count": 0, "incomplete_results": False, "items": []}


@pytest.fixture(autouse=True)
def shipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The definitions file the buttons read the verified revisions from. verify(id, rev) adds one."""
    path = tmp_path / "definitions.jsonl"
    path.write_text(json.dumps({"id": "acme_plain", "rev": REV, "risk": "low", "chain": []}) + "\n")
    monkeypatch.setattr(button, "DEFINITIONS", path)

    def verify(ident: str, rev: str) -> None:
        with path.open("a") as file:
            file.write(json.dumps({"id": ident, "rev": rev, "risk": "medium", "verified": True, "chain": []}) + "\n")

    return verify


@pytest.fixture(autouse=True)
def no_errors(caplog: pytest.LogCaptureFixture):
    """Nothing logs an error: a button added twice, for one, is refused with one."""
    yield
    assert [record.getMessage() for record in caplog.get_records("call") if record.levelno >= logging.ERROR] == []


@pytest.fixture(autouse=True)
def github(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """GitHub's search API as Home Assistant's shared session sees it: HTTP 503 unless a test answers otherwise. No
    request leaves the test."""
    aioclient_mock.get(API, status=503)
    return aioclient_mock


def answer(github: AiohttpClientMocker, **response) -> None:
    """What GitHub's search API answers from now on (json, content, status, headers, exc or side_effect)."""
    github.clear_requests()
    github.get(API, **response)


def issues(total: int, *numbers: object) -> dict:
    """A search answer: each item with links of its own that the notification must not take."""
    return {"total_count": total, "incomplete_results": False, "items": [
        {"number": number, "html_url": "https://example.com/issue", "url": "https://example.com/api", "title": "x"} for number in numbers]}


def asked(github: AiohttpClientMocker) -> list[tuple[str, str, dict[str, str], dict]]:
    """Each request: its method, its URL without the query, the query and the headers."""
    return [(method, str(url.with_query(None)), dict(url.query), headers) for method, url, _data, headers in github.mock_calls]


@pytest.fixture
def notifications(hass: HomeAssistant) -> dict[str, dict]:
    """The persistent notifications by id, as the frontend gets them: in the event loop, before press() returns."""
    shown: dict[str, dict] = {}

    @callback
    def updated(kind: UpdateType, items: dict) -> None:
        for notification_id, item in items.items():
            if kind is UpdateType.REMOVED:
                shown.pop(notification_id, None)
            else:
                shown[notification_id] = dict(item)

    persistent_notification.async_register_callback(hass, updated)
    return shown


def buttons(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, er.RegistryEntry]:
    """The entry's entities in the entity registry by unique id."""
    return {item.unique_id: item for item in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}


def ids(ieee: str, *kinds: str) -> set[str]:
    return {f"{ieee}-{kind}" for kind in kinds}


async def press(hass: HomeAssistant, entry: MockConfigEntry, unique_id: str) -> None:
    await hass.services.async_call("button", "press", {"entity_id": buttons(hass, entry)[unique_id].entity_id}, blocking=True)


def links(message: str) -> list[str]:
    """The targets of the markdown links: a target holds no space, no bracket and no line break."""
    return re.findall(r"\]\(([^()\s]+)\)", message)


def fields(url: str) -> dict[str, str]:
    parts = urlsplit(url)
    return {key: values[0] for key, values in parse_qs(parts.query, keep_blank_values=True).items()} | {"": f"{parts.scheme}://{parts.netloc}{parts.path}"}


async def test_a_device_on_a_definition_gets_both_buttons_on_its_page(hass: HomeAssistant, zha: Zha):
    device_id = zha.add_device(IEEE_A, "_TZE200_aaaa", "TS0601", "Radar", label("tuya_radar"))
    zha.add_device(IEEE_B, name="Lamp", quirk="zhaquirks.tuya.ts0601_switch:TuyaSwitch")
    entry = await setup_entry(hass)
    found = buttons(hass, entry)
    assert set(found) == ids(IEEE_A, *BOTH)
    for kind in BOTH:
        item = found[f"{IEEE_A}-{kind}"]
        assert (item.domain, item.device_id, item.entity_category, item.translation_key, item.disabled_by) == ("button", device_id, EntityCategory.DIAGNOSTIC, kind, None)
        assert hass.states.get(item.entity_id).state == "unknown"
    names = {hass.states.get(item.entity_id).attributes["friendly_name"] for item in found.values()}
    assert names == {"Radar Report a problem (xquirks)", "Radar Confirm it works (xquirks)"}


async def test_works_only_while_the_revision_is_not_verified(hass: HomeAssistant, zha: Zha, shipped):
    shipped("tuya_radar", REV)
    zha.add_device(IEEE_A, quirk=label("tuya_radar", REV))
    zha.add_device(IEEE_B, quirk=label("acme_plain", REV))
    entry = await setup_entry(hass)
    assert set(buttons(hass, entry)) == ids(IEEE_A, "report") | ids(IEEE_B, *BOTH)
    # An update brings a revision nobody verified yet, then a release verifies it.
    await zha.reload({IEEE_A: label("tuya_radar", NEW_REV)})
    assert set(buttons(hass, entry)) == ids(IEEE_A, *BOTH) | ids(IEEE_B, *BOTH)
    shipped("tuya_radar", NEW_REV)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert set(buttons(hass, entry)) == ids(IEEE_A, "report") | ids(IEEE_B, *BOTH)


def test_the_verified_revisions_are_those_of_the_shipped_file(tmp_path: Path):
    shipped = [json.loads(line) for line in (FOLDER / "definitions.jsonl").read_text().splitlines()]
    assert button.verified_revisions(FOLDER / "definitions.jsonl") == {item["id"]: item["rev"] for item in shipped if item.get("verified") is True}
    path = tmp_path / "definitions.jsonl"
    lines = [{"id": "a", "rev": "11111111", "verified": True}, {"id": "b", "rev": "22222222", "verified": False}, {"id": "c", "rev": "33333333"}]
    path.write_text("".join(json.dumps(item, indent=None, separators=(", ", ": ")) + "\n" for item in lines))
    assert button.verified_revisions(path) == {"a": "11111111"}
    assert button.verified_revisions(tmp_path / "missing.jsonl") == {}


@pytest.mark.parametrize("quirk", [NO_QUIRK, "zhaquirks.tuya.ts0601_switch:TuyaSwitch", "z2m_own:(None / X)"])
async def test_the_buttons_go_with_the_definition(hass: HomeAssistant, zha: Zha, quirk: str):
    """Excluded in the options, taken by a quirk of ZHA or of the user, its definition gone: at the next ZHA start the
    device runs no definition."""
    zha.add_device(IEEE_A, quirk=label("tuya_a"))
    zha.add_device(IEEE_B, quirk=label("acme_b"))
    entry = await setup_entry(hass)
    gone = [buttons(hass, entry)[unique_id].entity_id for unique_id in ids(IEEE_A, *BOTH)]
    # Kept while ZHA reloads: its devices cannot be read.
    zha.start_reload()
    await hass.async_block_till_done()
    assert set(buttons(hass, entry)) == ids(IEEE_A, *BOTH) | ids(IEEE_B, *BOTH)
    zha.quirks[IEEE_A] = quirk
    zha.finish_reload()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert set(buttons(hass, entry)) == ids(IEEE_B, *BOTH)
    assert [hass.states.get(entity_id) for entity_id in gone] == [None, None]


async def test_the_buttons_go_with_the_device(hass: HomeAssistant, zha: Zha):
    """Removed from ZHA or only from the device registry, also while ZHA is not loaded: none stays without its device."""
    zha.add_device(IEEE_A, quirk=label("tuya_a"))
    other = zha.add_device(IEEE_B, quirk=label("acme_b"))
    entry = await setup_entry(hass)
    zha.remove_device(IEEE_A)
    await hass.async_block_till_done()
    assert set(buttons(hass, entry)) == ids(IEEE_B, *BOTH)
    zha.start_reload()
    dr.async_get(hass).async_remove_device(other)
    await hass.async_block_till_done()
    assert buttons(hass, entry) == {} and hass.states.async_entity_ids("button") == []


async def test_a_device_that_joins_gets_its_buttons(hass: HomeAssistant, zha: Zha):
    entry = await setup_entry(hass)
    assert buttons(hass, entry) == {}
    device_id = zha.add_device(IEEE_A, quirk=label("tuya_a"))
    await hass.async_block_till_done()
    assert {item.device_id for item in buttons(hass, entry).values()} == {device_id} and set(buttons(hass, entry)) == ids(IEEE_A, *BOTH)


async def test_a_button_the_user_disabled_stays_disabled(hass: HomeAssistant, zha: Zha):
    zha.add_device(IEEE_A, quirk=label("tuya_a"))
    entry = await setup_entry(hass)
    works = buttons(hass, entry)[f"{IEEE_A}-works"].entity_id
    er.async_get(hass).async_update_entity(works, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()
    await zha.reload()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert buttons(hass, entry)[f"{IEEE_A}-works"].disabled_by is er.RegistryEntryDisabler.USER
    assert hass.states.get(works) is None and len(buttons(hass, entry)) == 2


async def test_after_a_restart_the_same_buttons_come_back_and_stale_ones_go(hass: HomeAssistant, zha: Zha):
    """Home Assistant restores an unavailable state for each entry of the entity registry until its entity is back."""
    zha.add_device(IEEE_A, quirk=label("tuya_a"))
    zha.add_device(IEEE_B, quirk=label("acme_b"))
    entry = await setup_entry(hass)
    before = {unique_id: item.entity_id for unique_id, item in buttons(hass, entry).items()}
    assert await hass.config_entries.async_unload(entry.entry_id)
    for item in buttons(hass, entry).values():
        item.write_unavailable_state(hass)
    zha.quirks[IEEE_B] = NO_QUIRK
    zha._publish()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert {unique_id: item.entity_id for unique_id, item in buttons(hass, entry).items()} == {key: before[key] for key in ids(IEEE_A, *BOTH)}
    for unique_id, entity_id in before.items():
        state = hass.states.get(entity_id)
        assert (state is not None and "restored" not in state.attributes) if unique_id.startswith(IEEE_A) else state is None


async def test_no_button_comes_or_goes_while_the_entry_unloads(hass: HomeAssistant, zha: Zha):
    """A sync the registry asked for before the unload that runs during it changes nothing."""
    zha.add_device(IEEE_A, quirk=label("tuya_a"))
    entry = await setup_entry(hass)
    zha.add_device(IEEE_B, quirk=label("acme_b"))
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert set(buttons(hass, entry)) == ids(IEEE_A, *BOTH)
    assert {hass.states.get(item.entity_id).state for item in buttons(hass, entry).values()} == {"unavailable"}


async def test_unload_reload_and_removal_of_the_entry(hass: HomeAssistant, zha: Zha):
    zha.add_device(IEEE_A, quirk=label("tuya_a"))
    entry = await setup_entry(hass)
    entity_ids = sorted(item.entity_id for item in buttons(hass, entry).values())
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert {hass.states.get(entity_id).state for entity_id in entity_ids} == {"unavailable"}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert sorted(item.entity_id for item in buttons(hass, entry).values()) == entity_ids == sorted(hass.states.async_entity_ids("button"))
    assert {hass.states.get(entity_id).state for entity_id in entity_ids} == {"unknown"}
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) == [] and hass.states.async_entity_ids("button") == []


async def test_without_an_answer_from_github_a_press_links_the_search_and_the_form(hass: HomeAssistant, zha: Zha, notifications: dict):
    """GitHub's search fails (the fixture's HTTP 503): the notification links the open issues of the definition and the
    form filled in with the values of that moment."""
    device_id = zha.add_device(IEEE_A, "_TZE200_aaaa", "TS0601", "Radar", label("tuya_radar"))
    dr.async_get(hass).async_update_device(device_id, sw_version="0x00000046")
    sensor = er.async_get(hass).async_get_or_create("sensor", "zha", f"{IEEE_A}-1-1026", config_entry=zha.entry, device_id=device_id)
    hass.states.async_set(sensor.entity_id, "21.5", {"unit_of_measurement": "°C"})
    entry = await setup_entry(hass)
    shown = {}
    for kind in BOTH:
        await press(hass, entry, f"{IEEE_A}-{kind}")
        (notification_id,) = set(notifications) - set(shown)
        shown[notification_id] = notifications[notification_id]
    report, works = shown.values()
    assert report["title"] == "Report a problem: tuya_radar" and works["title"] == "Confirm it works: tuya_radar"
    table = "\n".join(["xquirks 0.0.0, firmware 0x00000046", "", "| Entity | Value |", "| --- | --- |", "| sensor 1-1026 | 21.5 °C |"])
    common = {"": diagnostics.NEW_ISSUE, "file": "tuya_radar", "revision": REV, "diagnostics": table}
    search, form = links(report["message"])
    assert report["message"] == STRINGS["common"]["report_unknown"].format(search=search, form=form)
    assert fields(form).items() >= (common | {"template": "wrong-value.yml", "title": "_TZE200_aaaa TS0601: "}).items()
    assert fields(search) == {"": diagnostics.ISSUES, "q": 'is:issue is:open label:bug in:body "Definition tuya_radar Revision"'}
    search, form = links(works["message"])
    assert works["message"] == STRINGS["common"]["works_unknown"].format(search=search, form=form)
    assert fields(form).items() >= (common | {"template": "works.yml", "title": "_TZE200_aaaa TS0601: works"}).items()
    assert fields(search) == {"": diagnostics.ISSUES, "q": f'is:issue is:open label:verify in:body "Definition tuya_radar Revision {REV}"'}
    # A second press replaces the notification with the values of that moment.
    hass.states.async_set(sensor.entity_id, "22.0", {"unit_of_measurement": "°C"})
    await press(hass, entry, f"{IEEE_A}-report")
    assert len(notifications) == 2 and "22.0" in fields(links(notifications[next(iter(shown))]["message"])[1])["diagnostics"]


async def test_a_press_reports_the_definition_the_device_runs_now(hass: HomeAssistant, zha: Zha, notifications: dict, github: AiohttpClientMocker):
    """An OTA update or Reconfigure brought another definition without a ZHA start: the buttons stay until the next one,
    a press searches and reports what the device runs at that moment."""
    device_id = zha.add_device(IEEE_A, "_TZE200_aaaa", "TS0601", "Radar", label("tuya_radar"))
    entry = await setup_entry(hass)
    zha.swap(IEEE_A, label("tuya_radar_b", NEW_REV), ("_TZE204_bbbb", "TS0601"))
    dr.async_get(hass).async_update_device(device_id, sw_version="0x00000050")
    await hass.async_block_till_done()
    assert set(buttons(hass, entry)) == ids(IEEE_A, *BOTH)
    for kind in BOTH:
        await press(hass, entry, f"{IEEE_A}-{kind}")
    report, works = notifications.values()
    assert report["title"] == "Report a problem: tuya_radar_b" and works["title"] == "Confirm it works: tuya_radar_b"
    search, form = map(fields, links(report["message"]))
    assert form.items() >= {"file": "tuya_radar_b", "revision": NEW_REV, "title": "_TZE204_bbbb TS0601: "}.items()
    assert form["diagnostics"].startswith("xquirks 0.0.0, firmware 0x00000050\n")
    assert search["q"] == 'is:issue is:open label:bug in:body "Definition tuya_radar_b Revision"'
    search, form = map(fields, links(works["message"]))
    assert form.items() >= {"file": "tuya_radar_b", "revision": NEW_REV, "title": "_TZE204_bbbb TS0601: works"}.items()
    assert search["q"] == f'is:issue is:open label:verify in:body "Definition tuya_radar_b Revision {NEW_REV}"'
    assert [query["q"] for _method, _url, query, _headers in asked(github)] == [
        f'repo:{const.REPOSITORY} is:issue is:open label:bug in:body "Definition tuya_radar_b Revision"',
        f'repo:{const.REPOSITORY} is:issue is:open label:verify in:body "Definition tuya_radar_b Revision {NEW_REV}"',
    ]


@pytest.mark.parametrize("quirk", [NO_QUIRK, "zhaquirks.tuya.ts0601_switch:TuyaSwitch", None])
async def test_a_press_without_a_definition_hands_out_no_link(hass: HomeAssistant, zha: Zha, notifications: dict, github: AiohttpClientMocker, quirk: str | None):
    """No definition since a re-interview or while ZHA reloads (None): the press fails with a message and asks GitHub
    nothing. The notification of a press before stays as it was."""
    zha.add_device(IEEE_A, quirk=label("tuya_radar"))
    entry = await setup_entry(hass)
    await press(hass, entry, f"{IEEE_A}-report")
    before = dict(notifications)
    if quirk is None:
        zha.start_reload()
    else:
        zha.swap(IEEE_A, quirk)
    for kind in BOTH:
        with pytest.raises(ServiceValidationError) as raised:
            await press(hass, entry, f"{IEEE_A}-{kind}")
        assert (raised.value.translation_domain, raised.value.translation_key) == (DOMAIN, "no_definition")
        assert str(raised.value) == STRINGS["exceptions"]["no_definition"]["message"].rstrip(".")
    assert notifications == before and github.call_count == 1


async def test_the_notifications_go_with_the_entry_only(hass: HomeAssistant, zha: Zha, notifications: dict):
    """They stay through a reload and when a device's buttons go: the user may still file that report."""
    zha.add_device(IEEE_A, quirk=label("tuya_a"))
    zha.add_device(IEEE_B, quirk=label("acme_b"))
    persistent_notification.async_create(hass, "Another integration's", notification_id="other")
    entry = await setup_entry(hass)
    for unique_id in ids(IEEE_A, *BOTH) | ids(IEEE_B, "report"):
        await press(hass, entry, unique_id)
    ours = set(notifications) - {"other"}
    assert len(ours) == 3
    await zha.reload({IEEE_A: NO_QUIRK})
    assert set(buttons(hass, entry)) == ids(IEEE_B, *BOTH)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert set(notifications) == ours | {"other"}
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert set(notifications) == {"other"}


def test_the_forms_take_the_fields_the_links_fill_in():
    """GitHub fills in a form's fields by their ids, labels the issue and opens the form by its file name."""
    for kind, (form, _title, issue_label) in diagnostics.FORMS.items():
        template = yaml.safe_load((ROOT / ".github" / "ISSUE_TEMPLATE" / form).read_text())
        assert template["labels"] == [issue_label], kind
        assert {"file", "revision", "ha", "diagnostics"} <= {item["id"] for item in template["body"] if "id" in item}, kind


def words(text: str) -> list[str]:
    """The words GitHub's search sees in a text: lowercase, split at anything but a letter or a digit (an underscore too)."""
    return re.findall(r"[a-z0-9]+", text.lower())


def submitted(kind: str, ident: str, rev: str, text: str) -> str:
    """The body of an issue from the form of the kind as GitHub renders it: each field but a markdown block as a heading
    of its label over its value, the text in each field the button does not fill in."""
    template = yaml.safe_load((ROOT / ".github" / "ISSUE_TEMPLATE" / diagnostics.FORMS[kind][0]).read_text())
    values = {"file": ident, "revision": rev}
    return "\n\n".join(f"### {item['attributes']['label']}\n\n{values.get(item.get('id'), text)}" for item in template["body"] if item["type"] != "markdown")


@pytest.mark.parametrize("kind", BOTH)
def test_the_search_finds_the_definition_in_the_fields_of_the_form(kind: str):
    """GitHub's advanced search finds a quoted phrase as words in that order without a gap, with in:body in the body
    only: not in the title or a comment. It splits an id at its underscores, so the heading after the id ends it. The
    phrase finds the form of the definition (of the revision for a works report), not that of an id that extends it,
    nor another one that mentions the definition."""
    (phrase,) = re.findall(r'in:body "([^"]+)"', diagnostics.search_words(kind, "tuya_radar", REV))

    def found(body: str) -> bool:
        haystack, needle = words(body), words(phrase)
        return any(haystack[start:start + len(needle)] == needle for start in range(len(haystack)))

    assert found(submitted(kind, "tuya_radar", REV, "x"))
    assert found(submitted(kind, "tuya_radar", NEW_REV, "x")) is (kind == "report")
    for other in ("tuya_radar_b", "tuya_radar2", "acme_tuya_radar", "tuya"):
        assert not found(submitted(kind, other, REV, f"Same as tuya_radar {REV}")), other


async def test_the_notification_follows_the_language_of_home_assistant(hass: HomeAssistant, zha: Zha, notifications: dict):
    hass.config.language = "de"
    zha.add_device(IEEE_A, quirk=label("tuya_radar"))
    entry = await setup_entry(hass)
    await press(hass, entry, f"{IEEE_A}-works")
    (shown,) = notifications.values()
    assert shown["title"] == "Funktioniert bestätigen: tuya_radar" and len(links(shown["message"])) == 2


async def test_the_notification_and_the_search_hold_no_address_and_no_name(hass: HomeAssistant, zha: Zha, notifications: dict, github: AiohttpClientMocker):
    area = ar.async_get(hass).async_create("Grandma's bedroom")
    device_id = zha.add_device(IEEE_A, "_TZE200_aaaa", "TS0601", "Living room radar", label("tuya_radar"))
    dr.async_get(hass).async_update_device(device_id, area_id=area.id, name_by_user="Erika's radar")
    sensor = er.async_get(hass).async_get_or_create("sensor", "zha", f"{IEEE_A}-1-1026", config_entry=zha.entry, device_id=device_id)
    er.async_get(hass).async_update_entity(sensor.entity_id, name="Erika's temperature")
    hass.states.async_set(sensor.entity_id, "21.5")
    entry = await setup_entry(hass)
    for kind in BOTH:
        await press(hass, entry, f"{IEEE_A}-{kind}")
    shown = [[item["title"], item["message"], notification_id, *map(fields, links(item["message"]))] for notification_id, item in notifications.items()]
    text = json.dumps(shown, ensure_ascii=False)
    secrets = (IEEE_A, IEEE_A.replace(":", ""), "Living room", "Erika", "Grandma", area.id, sensor.entity_id, "radar_")
    for secret in secrets:
        assert secret.lower() not in text.lower(), secret
    # GitHub's search gets the definition's id and revision only: not even the names or values the form holds.
    sent = json.dumps([str(call) for call in github.mock_calls], ensure_ascii=False)
    for secret in (*secrets, "_TZE200_aaaa", "TS0601", "21.5"):
        assert secret.lower() not in sent.lower(), secret
    assert len(notifications) == 2 and github.call_count == 2


async def test_a_press_asks_github_for_the_definition_only(hass: HomeAssistant, zha: Zha, notifications: dict, github: AiohttpClientMocker):
    """Only a press asks, once: GitHub's search API through Home Assistant's shared session, without a token or a
    redirect, for the words of the search link in the repository, with the advanced search of the link's issue page.
    Nothing runs in the background, nothing is kept (and the test harness blocks every socket)."""
    zha.add_device(IEEE_A, "_TZE200_aaaa", "TS0601", "Radar", label("tuya_radar"))
    entry = await setup_entry(hass)
    await zha.reload()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert github.call_count == 0
    shared, options = aiohttp_client.async_get_clientsession(hass), []

    class Shared:
        """The shared session, the options of each request noted."""

        def get(self, url: str, **kwargs):
            options.append({key: value for key, value in kwargs.items() if key not in ("params", "headers")})
            return shared.get(url, **kwargs)

    with patch.object(button, "async_get_clientsession", lambda given: Shared() if given is hass else None), \
            patch("homeassistant.helpers.httpx_client.get_async_client", side_effect=AssertionError("no httpx")):
        for kind in (*BOTH, "report"):
            await press(hass, entry, f"{IEEE_A}-{kind}")
    await hass.async_block_till_done(wait_background_tasks=True)
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "xquirks"}
    search = {"per_page": "1", "advanced_search": "true"}
    report = ("GET", API, {"q": f'repo:{const.REPOSITORY} is:issue is:open label:bug in:body "Definition tuya_radar Revision"'} | search, headers)
    works = ("GET", API, {"q": f'repo:{const.REPOSITORY} is:issue is:open label:verify in:body "Definition tuya_radar Revision {REV}"'} | search, headers)
    assert asked(github) == [report, works, report] and options == [{"allow_redirects": False}] * 3
    # The words of the search link, so the search finds what the link shows.
    for (_method, _url, query, _headers), kind in zip(asked(github), BOTH, strict=False):
        assert query["q"] == f"repo:{const.REPOSITORY} {fields(diagnostics.search_url(kind, 'tuya_radar', REV))['q']}"


class TestOnTheWire:
    """Home Assistant's shared session itself, not the mock: a server on 127.0.0.1, the one host the test harness lets a
    socket reach, stands in for GitHub."""

    @pytest.fixture(autouse=True)
    def github(self) -> None:
        """The shared session as Home Assistant creates it."""

    async def test_github_gets_the_name_of_xquirks_and_no_versions(self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, socket_enabled: None):
        """Each header of the search but Host and Accept-Encoding: the User-Agent names xquirks, not the versions of
        Home Assistant, aiohttp and Python that the shared session sends by default."""
        seen: list[dict[str, str]] = []

        async def search(request: web.Request) -> web.Response:
            seen.append({key.lower(): value for key, value in request.headers.items()})
            return web.json_response(NO_ISSUE)

        app = web.Application()
        app.router.add_get("/search/issues", search)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            host, port = runner.addresses[0][:2]
            monkeypatch.setattr(button, "SEARCH_API", f"http://{host}:{port}/search/issues")
            assert await button.async_find(hass, "report", "tuya_radar", REV) == ""
        finally:
            await runner.cleanup()
        (headers,) = seen
        assert {key: value for key, value in headers.items() if key not in ("host", "accept-encoding")} == {
            "user-agent": "xquirks", "accept": "application/vnd.github+json", "x-github-api-version": "2022-11-28"}


@pytest.mark.parametrize("kind", BOTH)
async def test_one_open_issue_gets_the_thumbs_up_only(hass: HomeAssistant, zha: Zha, notifications: dict, github: AiohttpClientMocker, kind: str):
    """The link is built from the repository and the issue's number, none of the answer's links is taken."""
    zha.add_device(IEEE_A, quirk=label("tuya_radar"))
    entry = await setup_entry(hass)
    answer(github, json=issues(1, 7))
    await press(hass, entry, f"{IEEE_A}-{kind}")
    (shown,) = notifications.values()
    assert shown["message"] == STRINGS["common"][f"{kind}_found"].format(link=ISSUE_7) and links(shown["message"]) == [ISSUE_7]


@pytest.mark.parametrize("kind", BOTH)
async def test_several_open_issues_get_the_search_list(hass: HomeAssistant, zha: Zha, notifications: dict, github: AiohttpClientMocker, kind: str):
    zha.add_device(IEEE_A, quirk=label("tuya_radar"))
    entry = await setup_entry(hass)
    answer(github, json=issues(3, 7))
    await press(hass, entry, f"{IEEE_A}-{kind}")
    (shown,) = notifications.values()
    search = diagnostics.search_url(kind, "tuya_radar", REV)
    assert shown["message"] == STRINGS["common"][f"{kind}_found"].format(link=search) and links(shown["message"]) == [search]


@pytest.mark.parametrize("kind", BOTH)
async def test_no_open_issue_gets_the_form_only(hass: HomeAssistant, zha: Zha, notifications: dict, github: AiohttpClientMocker, kind: str):
    zha.add_device(IEEE_A, "_TZE200_aaaa", "TS0601", quirk=label("tuya_radar"))
    entry = await setup_entry(hass)
    answer(github, json=NO_ISSUE)
    await press(hass, entry, f"{IEEE_A}-{kind}")
    (shown,) = notifications.values()
    (form,) = links(shown["message"])
    assert shown["message"] == STRINGS["common"][f"{kind}_new"].format(form=form)
    assert fields(form).items() >= {"": diagnostics.NEW_ISSUE, "template": diagnostics.FORMS[kind][0], "file": "tuya_radar", "revision": REV}.items()


def unknown(notifications: dict) -> bool:
    """Asserts that the one notification is that of a failed search for a problem report: the search list and the form."""
    (shown,) = notifications.values()
    search, form = links(shown["message"])
    assert shown["message"] == STRINGS["common"]["report_unknown"].format(search=search, form=form)
    assert search == diagnostics.search_url("report", "tuya_radar", REV) and fields(form)["template"] == "wrong-value.yml"
    return True


FAILED = {
    "timeout": {"exc": TimeoutError()},
    "no network": {"exc": aiohttp.ClientConnectionError("Cannot connect to host api.github.com:443")},
    "connection reset": {"exc": aiohttp.ServerDisconnectedError()},
    "another error": {"exc": RuntimeError("Session is closed")},
    "rate limit 403": {"status": 403, "json": {"message": "API rate limit exceeded"}, "headers": {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1795368333"}},
    "rate limit 429": {"status": 429, "json": issues(1, 7), "headers": {"Retry-After": "60"}},
    "server error": {"status": 500, "json": issues(1, 7)},
    "not found": {"status": 404},
    "validation failed": {"status": 422, "json": {"message": "Validation Failed"}},
    "redirect": {"status": 301, "headers": {"Location": "https://example.com/search"}},
    "no content": {"status": 204},
    "html": {"text": "<html><body>Rate limited</body></html>"},
    "cut off": {"text": json.dumps(issues(1, 7))[:-5]},
    "empty": {"content": b""},
    "not utf-8": {"content": b'{"total_count": 0, "incomplete_results": false, "items": [], "x": "\xff"}'},
    "too deep": {"text": "[" * 5000 + "]" * 5000},
    "two documents": {"text": json.dumps(NO_ISSUE) * 2},
}


@pytest.mark.parametrize("response", FAILED.values(), ids=FAILED.keys())
async def test_a_failed_search_gets_both_links(hass: HomeAssistant, zha: Zha, notifications: dict, github: AiohttpClientMocker, response: dict):
    """No network, a timeout, a rate limit or any other status than 200, a body that is no JSON: the press goes through
    without an error and the notification links the search and the form as without GitHub."""
    zha.add_device(IEEE_A, quirk=label("tuya_radar"))
    entry = await setup_entry(hass)
    answer(github, **response)
    await press(hass, entry, f"{IEEE_A}-report")
    assert unknown(notifications) and github.call_count == 1


WRONG = [
    None, [], "found", 1, {}, issues(1, 7)["items"],
    issues(1, 7) | {"total_count": "1"}, issues(1, 7) | {"total_count": True}, issues(1, 7) | {"total_count": 1.0}, issues(1, 7) | {"total_count": None},
    NO_ISSUE | {"total_count": -1}, issues(2, 7) | {"total_count": 2.5},
    issues(1, 7) | {"incomplete_results": True}, issues(1, 7) | {"incomplete_results": "false"}, issues(1, 7) | {"incomplete_results": None},
    {key: value for key, value in issues(1, 7).items() if key != "incomplete_results"}, NO_ISSUE | {"incomplete_results": True},
    {key: value for key, value in issues(2, 7).items() if key != "items"}, issues(1, 7) | {"items": {"number": 7}}, issues(2, 7) | {"items": "7"},
    issues(1), issues(1, 7, 8), issues(0, 7), issues(2), issues(2, 7, 8),
    issues(1) | {"items": [7]}, issues(1) | {"items": [None]}, issues(1) | {"items": [[7]]}, issues(1) | {"items": [{"html_url": ISSUE_7}]},
    issues(1, "7"), issues(1, True), issues(1, 7.0), issues(1, None), issues(1, 0), issues(1, -7), issues(1, 2**31), issues(1, 10**30), issues(1, [7]), issues(1, {"7": 7}),
]


@pytest.mark.parametrize("wrong", WRONG, ids=range(len(WRONG)))
async def test_an_answer_of_unexpected_shape_or_types_gets_both_links(hass: HomeAssistant, zha: Zha, notifications: dict, github: AiohttpClientMocker, wrong: object):
    """Only total_count, incomplete_results and the first item's number are read. Anything but a complete answer of the
    expected types, with as many items as asked for, counts as a failed search."""
    zha.add_device(IEEE_A, quirk=label("tuya_radar"))
    entry = await setup_entry(hass)
    answer(github, text=json.dumps(wrong))
    await press(hass, entry, f"{IEEE_A}-report")
    assert unknown(notifications)


async def test_an_answer_beyond_the_size_bound_counts_as_a_failed_search(hass: HomeAssistant, zha: Zha, notifications: dict, github: AiohttpClientMocker):
    """One issue of an answer holds its whole text: a larger answer than the bound is not read to its end."""
    zha.add_device(IEEE_A, quirk=label("tuya_radar"))
    entry = await setup_entry(hass)
    assert button.MAX_ANSWER == 1024 * 1024
    answer(github, content=json.dumps(issues(1, 7)).encode().ljust(button.MAX_ANSWER))
    await press(hass, entry, f"{IEEE_A}-report")
    assert links(next(iter(notifications.values()))["message"]) == [ISSUE_7]
    answer(github, content=json.dumps(issues(1, 7)).encode().ljust(button.MAX_ANSWER + 1))
    await press(hass, entry, f"{IEEE_A}-report")
    assert unknown(notifications)


async def test_a_search_that_hangs_ends_at_the_time_limit(hass: HomeAssistant, zha: Zha, notifications: dict, github: AiohttpClientMocker, monkeypatch: pytest.MonkeyPatch):
    zha.add_device(IEEE_A, quirk=label("tuya_radar"))
    entry = await setup_entry(hass)
    assert button.SEARCH_TIMEOUT == 10
    monkeypatch.setattr(button, "SEARCH_TIMEOUT", 0.05)

    async def hang(method: str, url: object, data: object) -> None:
        await asyncio.Event().wait()

    answer(github, side_effect=hang)
    async with asyncio.timeout(5):
        await press(hass, entry, f"{IEEE_A}-report")
    assert unknown(notifications) and github.call_count == 1


class SlowGitHub:
    """A search API that answers only once release() was called. asked(n) waits for its n-th request."""

    def __init__(self, github: AiohttpClientMocker, answer_json: dict) -> None:
        self._requests, self._asked, self._release, self._json = 0, asyncio.Condition(), asyncio.Event(), answer_json
        answer(github, side_effect=self._answer)

    async def _answer(self, method: str, url: object, data: object) -> AiohttpClientMockResponse:
        async with self._asked:
            self._requests += 1
            self._asked.notify_all()
        await self._release.wait()
        return AiohttpClientMockResponse(method, url, json=self._json)

    async def asked(self, count: int) -> None:
        async with asyncio.timeout(5), self._asked:
            await self._asked.wait_for(lambda: self._requests >= count)

    def release(self) -> None:
        self._release.set()


async def test_a_press_while_the_same_button_searches_does_nothing(hass: HomeAssistant, zha: Zha, notifications: dict, github: AiohttpClientMocker):
    """The press before brings its notification within the time limit. The event loop runs on meanwhile: the second press
    and the other button go through."""
    zha.add_device(IEEE_A, quirk=label("tuya_radar"))
    entry = await setup_entry(hass)
    slow = SlowGitHub(github, issues(1, 7))
    first = hass.async_create_task(press(hass, entry, f"{IEEE_A}-report"))
    await slow.asked(1)
    async with asyncio.timeout(5):
        await press(hass, entry, f"{IEEE_A}-report")
    assert github.call_count == 1 and notifications == {} and not first.done()
    other = hass.async_create_task(press(hass, entry, f"{IEEE_A}-works"))
    await slow.asked(2)
    slow.release()
    async with asyncio.timeout(5):
        await asyncio.gather(first, other)
    assert [links(item["message"]) for item in notifications.values()] == [[ISSUE_7], [ISSUE_7]] and github.call_count == 2
    # Through, the next press asks again: nothing is kept.
    await press(hass, entry, f"{IEEE_A}-report")
    assert github.call_count == 3


async def test_a_search_the_entry_does_not_outlive_leaves_no_notification(hass: HomeAssistant, zha: Zha, notifications: dict, github: AiohttpClientMocker):
    """The notifications go with the entry: one whose search ends after the entry went is not shown."""
    zha.add_device(IEEE_A, quirk=label("tuya_radar"))
    entry = await setup_entry(hass)
    slow = SlowGitHub(github, NO_ISSUE)
    first = hass.async_create_task(press(hass, entry, f"{IEEE_A}-report"))
    await slow.asked(1)
    await hass.config_entries.async_remove(entry.entry_id)
    slow.release()
    async with asyncio.timeout(5):
        await first
    await hass.async_block_till_done()
    assert notifications == {}


async def test_texts_that_load_while_the_entry_goes_leave_no_notification(hass: HomeAssistant, zha: Zha, notifications: dict, github: AiohttpClientMocker, monkeypatch: pytest.MonkeyPatch):
    """The first press in a language loads the notification's texts after the search: the entry may go meanwhile too."""
    zha.add_device(IEEE_A, quirk=label("tuya_radar"))
    entry = await setup_entry(hass)
    answer(github, json=NO_ISSUE)
    loading, loaded, load = asyncio.Event(), asyncio.Event(), button.translation.async_get_translations

    async def held(*args, **kwargs) -> dict[str, str]:
        loading.set()
        await loaded.wait()
        return await load(*args, **kwargs)

    monkeypatch.setattr(button, "translation", SimpleNamespace(async_get_translations=held))
    first = hass.async_create_task(press(hass, entry, f"{IEEE_A}-report"))
    async with asyncio.timeout(5):
        await loading.wait()
    await hass.config_entries.async_remove(entry.entry_id)
    loaded.set()
    async with asyncio.timeout(5):
        await first
    await hass.async_block_till_done()
    assert notifications == {} and github.call_count == 1


@pytest.mark.parametrize("gone", ["device", "definition"])
async def test_a_search_the_button_does_not_outlive_leaves_no_notification(hass: HomeAssistant, zha: Zha, notifications: dict, github: AiohttpClientMocker, gone: str):
    """The buttons go during the search, with their device or at a ZHA start that puts it on no definition: the press
    ends without a notification and without an error."""
    zha.add_device(IEEE_A, quirk=label("tuya_radar"))
    entry = await setup_entry(hass)
    slow = SlowGitHub(github, NO_ISSUE)
    first = hass.async_create_task(press(hass, entry, f"{IEEE_A}-report"))
    await slow.asked(1)
    if gone == "device":
        zha.remove_device(IEEE_A)
    else:
        zha.start_reload()
        zha.quirks[IEEE_A] = NO_QUIRK
        zha.finish_reload()
    # Not hass.async_block_till_done(): it would wait for the press too.
    async with asyncio.timeout(5):
        while hass.states.async_entity_ids("button"):
            await asyncio.sleep(0)
    assert buttons(hass, entry) == {}
    slow.release()
    async with asyncio.timeout(5):
        await first
    await hass.async_block_till_done(wait_background_tasks=True)
    assert notifications == {} and github.call_count == 1


async def test_a_button_gone_before_its_press_begins_does_nothing(hass: HomeAssistant, zha: Zha, notifications: dict, github: AiohttpClientMocker):
    """A call to several buttons starts each press later, in a task of its own: a button may go before its press
    begins. The other button's press goes through."""
    zha.add_device(IEEE_A, quirk=label("tuya_radar"))
    entry = await setup_entry(hass)
    answer(github, json=NO_ISSUE)
    report, works = (buttons(hass, entry)[f"{IEEE_A}-{kind}"].entity_id for kind in BOTH)
    first = hass.async_create_task(hass.services.async_call("button", "press", {"entity_id": [report, works]}, blocking=True))
    assert not first.done() and github.call_count == 0
    er.async_get(hass).async_remove(report)
    async with asyncio.timeout(5):
        await first
    (shown,) = notifications.values()
    assert shown["title"] == "Confirm it works: tuya_radar" and github.call_count == 1


@pytest.mark.parametrize("ident", ["repo:other/xquirks", "tuya_radar OR is:closed", "-tuya_radar", "Tuya_radar", "tuya-radar", 'tuya"radar', "x" * 101])
async def test_a_definition_that_is_no_plain_word_is_not_searched(hass: HomeAssistant, zha: Zha, notifications: dict, github: AiohttpClientMocker, ident: str):
    """Ids are lowercase letters, digits and underscores: anything else could leave the repository or change the search."""
    zha.add_device(IEEE_A, quirk=label(ident))
    entry = await setup_entry(hass)
    await press(hass, entry, f"{IEEE_A}-report")
    (shown,) = notifications.values()
    assert len(links(shown["message"])) == 2 and github.call_count == 0


HOSTILE = 'a "quote"\nnew line | pipe \\| back ) bracket ] [x](y) <b>bold</b> ' + "\u00e4\u4e2d\U0001f600" * 3000


def pipes(row: str) -> int:
    """The pipes of a markdown table row that separate cells: a backslash escapes the character after it."""
    return re.findall(r"\\.|\||[^\\|]+", row).count("|")


@pytest.mark.parametrize("hostile", [HOSTILE, "\U0001f600" * 5000])
def test_hostile_values_are_bounded_and_encoded(hostile: str):
    """Device-supplied names, firmware and states: each one bounded, on one line, the whole link within GitHub's limit
    and encoded so that it cannot end a markdown link."""
    values = {f"sensor 1-{hostile}-{number}": hostile for number in range(50)}
    for kind in BOTH:
        url = diagnostics.report_url(kind, hostile, hostile, hostile, hostile, f"xquirks 1.0.0, firmware {hostile}", values)
        assert len(url) <= diagnostics.URL_LIMIT and re.fullmatch(r"[A-Za-z0-9%:/?&=._~-]+", url), kind
        found = fields(url)
        assert found["title"].endswith(": works" if kind == "works" else "...: ") and len(found["title"]) < 140 and "\n" not in found["title"]
        assert len(found["file"]) == 100 and len(found["revision"]) == 16 and "\n" not in found["file"] + found["revision"]
        rows = found["diagnostics"].splitlines()
        assert rows[0].startswith("xquirks 1.0.0, firmware ") and len(rows[0]) == 150
        assert rows[1:4] == ["", "| Entity | Value |", "| --- | --- |"] and rows[-1].startswith("| and ") and rows[-1].endswith(" more | in the diagnostics of xquirks |")
        assert all(pipes(row) == 3 and len(row) < 250 for row in rows[2:]), rows[2:6]
        search = diagnostics.search_url(kind, hostile, hostile)
        assert len(search) < 2000 and re.fullmatch(r"[A-Za-z0-9%:/?&=._~-]+", search)
