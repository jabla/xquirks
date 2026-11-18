"""The firmware watch: a device whose firmware changed from one known version to another is watched for a day again, as
after an install, while its file is not verified. Its notice asks only while the device's variant and new firmware are
below the threshold ("looks off" always comes). Unknown to a version is no update, each pair of device and firmware is
watched once, a device that leaves or a file that changes ends its watch. No test reaches the network."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.common import flush_store
from zigpy.exceptions import DeliveryError

from .conftest import NO_QUIRK, REPOS, STRINGS, Zha, apply_list, continue_fix, issues_of, manager_of, start_fix
from .integration import PACKAGE, const
from .test_checks import ACME_C, IEEE_A, IEEE_C, NAME, TUYA_A, LiveDevice, acme, checks, conclude, hard_restart, installed, radar, settle, watch_for
from .test_confirm_search import FIRMWARE_A, FIRMWARE_B, HALL, IEEE_B, NEW_KEY, answer, fine_values, hall, hit, keyed, prepare, same_revision, searches, set_firmware, title
from .test_content import restart_with
from .test_data_query import queries
from .test_self_check_flow import Search, answers, ask

FIRMWARE_C = "0x00000071"
FILE = "z2m_tuya_a.py"
NOTE = checks.FIRMWARE_NOTE
QUERY = const.TUYA_QUERY_DATA


@pytest.fixture
def started():
    """The firmware watches started: (device id, firmware)."""
    calls: list[tuple[str, str]] = []
    original = checks.Checks._watch_firmware

    def spy(self, device_id: str, name: str, version: str) -> None:
        calls.append((device_id, version))
        original(self, device_id, name, version)

    with patch.object(checks.Checks, "_watch_firmware", spy):
        yield calls


async def done_installed(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, firmware: str | None = FIRMWARE_A, *,
                         manifest: dict | None = None, second: bool = False) -> tuple[list[LiveDevice], Search]:
    """z2m_tuya_a.py installed for the kitchen radar (this firmware) and, with second, the hall radar (firmware B). Its
    self-check is over (nothing came: an unproven notice, the search found no confirmation)."""
    prepare(repo_dir, manifest)
    devices = [radar(hass, zha)]
    set_firmware(hass, devices[0].device_id, firmware)
    quirks = {IEEE_A: TUYA_A}
    if second:
        devices.append(hall(hass, zha))
        set_firmware(hass, devices[1].device_id, FIRMWARE_B)
        quirks[IEEE_B] = TUYA_A
    search = Search(aioclient_mock)
    await installed(hass, zha, repo_dir, quirks)
    await conclude(hass)
    assert self_check(hass)["result"] != "watching"
    return devices, search


async def update(hass: HomeAssistant, device: LiveDevice, firmware: str | None) -> None:
    """ZHA writes the device's new firmware into the device registry. The refresh after it ran."""
    set_firmware(hass, device.device_id, firmware)
    await settle(hass)


def self_check(hass: HomeAssistant, name: str = FILE) -> dict | None:
    return manager_of(hass).summary()["self_check"].get(name)


def kept(hass: HomeAssistant) -> dict:
    return manager_of(hass).checks.firmware


def watch_of(hass: HomeAssistant, device: LiveDevice) -> dict | None:
    record = kept(hass).get(device.device_id)
    return record.get("watch") if record is not None else None


def firmware_notices(hass: HomeAssistant) -> list[ir.IssueEntry]:
    return [issue for kind in checks.KINDS for issue in issues_of(hass, kind) if "device" in (issue.data or {})]


def file_notices(hass: HomeAssistant) -> list[ir.IssueEntry]:
    return [issue for kind in checks.KINDS for issue in issues_of(hass, kind) if "device" not in (issue.data or {})]


def shape_ok(firmware: object) -> None:
    """The kept versions in the shape the integration writes them."""
    assert isinstance(firmware, dict) and len(firmware) <= checks.MAX_FIRMWARE
    for device_id, record in firmware.items():
        assert isinstance(device_id, str) and isinstance(record, dict)
        assert set(record) <= {"file", "revision", "version", "watched", "watch"}
        assert isinstance(record["file"], str)
        assert record["revision"] is None or isinstance(record["revision"], str)
        assert record["version"] is None or checks.firmware_version(record["version"]) == record["version"]
        assert isinstance(record["watched"], list) and len(record["watched"]) <= checks.MAX_VERSIONS
        assert all(checks.firmware_version(version) == version for version in record["watched"])
        assert len(set(record["watched"])) == len(record["watched"])
        if "watch" in record:
            watch = record["watch"]
            assert checks.Checks._valid(watch) and watch["file"] == record["file"] and watch["device"] == device_id
            assert list(watch["devices"]) == [device_id] and checks.firmware_version(watch["firmware"]) == watch["firmware"]


# What counts as a firmware update


@pytest.mark.parametrize("value", [None, 5, "", "   ", "\ud800", "x"])
def test_a_version_is_a_nonempty_text(value):
    expected = "x" if value == "x" else None
    assert checks.firmware_version(value) == expected


def test_an_overlong_version_is_kept_short_and_distinct():
    one, other = checks.firmware_version("x" * 500), checks.firmware_version("x" * 499 + "y")
    assert one != other
    assert len(one) <= checks.MAX_VERSION_LENGTH and len(other) <= checks.MAX_VERSION_LENGTH
    # The short form reads the same again (a stored version stays as it is).
    assert checks.firmware_version(one) == one


@pytest.mark.parametrize("unknown", [None, ""])
async def test_unknown_to_a_version_is_only_recorded(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started, unknown):
    """Many devices report their version only after their first image query: that is no update."""
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock, unknown)
    await update(hass, device, FIRMWARE_A)
    assert started == [] and watch_of(hass, device) is None
    assert kept(hass)[device.device_id]["version"] == FIRMWARE_A
    # From there on a new version is an update.
    await update(hass, device, FIRMWARE_B)
    assert started == [(device.device_id, FIRMWARE_B)]


async def test_a_version_that_goes_away_is_not_forgotten(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    """None or empty never overwrites the version known before: back to it is no update, another one is."""
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    for value in (None, "", FIRMWARE_A):
        await update(hass, device, value)
    assert started == [] and kept(hass)[device.device_id]["version"] == FIRMWARE_A
    await update(hass, device, None)
    await update(hass, device, FIRMWARE_B)
    assert started == [(device.device_id, FIRMWARE_B)]


async def test_an_update_without_a_firmware_change_is_ignored(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    """A registry change of another field, or a firmware back where it was before the refresh: nothing to watch."""
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    dr.async_get(hass).async_update_device(device.device_id, name_by_user="Radar by the door")
    await settle(hass)
    set_firmware(hass, device.device_id, FIRMWARE_B)
    set_firmware(hass, device.device_id, FIRMWARE_A)
    await settle(hass)
    await manager_of(hass).async_refresh()
    assert started == [] and watch_of(hass, device) is None


async def test_a_long_version_change_is_seen(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock, "x" * 500)
    await update(hass, device, "x" * 499 + "y")
    assert len(started) == 1 and len(kept(hass)[device.device_id]["version"]) <= checks.MAX_VERSION_LENGTH


# The watch and its notice


async def test_a_new_version_watches_that_device_only(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    (kitchen, hallway), _search = await done_installed(hass, zha, repo_dir, aioclient_mock, second=True)
    (before,) = file_notices(hass)
    await update(hass, kitchen, FIRMWARE_C)
    assert started == [(kitchen.device_id, FIRMWARE_C)]
    assert watch_of(hass, hallway) is None
    watch = watch_of(hass, kitchen)
    assert watch["file"] == FILE and watch["firmware"] == FIRMWARE_C and list(watch["devices"]) == [kitchen.device_id] and not watch.get("done")
    # The file's self-check is over and stays so.
    assert self_check(hass)["result"] == "unproven"
    summary = manager_of(hass).summary()["firmware_checks"]
    assert summary == [{"device": "_TZE200_aaaa TS0601", "file": FILE, "revision": watch["revision"], "firmware": FIRMWARE_C, "observed_hours": 0.0,
                        "result": "watching", "device_check_done": False}]
    # Values come from the kitchen radar only.
    fine_values(kitchen)
    await conclude(hass)
    (issue,) = firmware_notices(hass)
    assert issue.translation_key == checks.FINE and issue.is_fixable and issue.is_persistent
    assert issue.data == {"kind": checks.FINE, "file": FILE, "revision": watch["revision"], "devices": [kitchen.device_id], "device": kitchen.device_id}
    lines = issue.translation_placeholders["devices"].splitlines()
    assert lines[0] == f"- {NAME} (\\_TZE200\\_aaaa TS0601)" and HALL not in issue.translation_placeholders["devices"]
    assert f"    - {NOTE}" in lines
    assert issue.issue_id != before.issue_id and [i.issue_id for i in file_notices(hass)] == [before.issue_id]
    assert watch_of(hass, kitchen)["done"] == "fine"
    # It stays through a refresh and a restart.
    await manager_of(hass).async_refresh()
    await restart_with(hass, hass.config_entries.async_entries(const.DOMAIN)[0], repo_dir)
    await conclude(hass)
    assert [i.issue_id for i in firmware_notices(hass)] == [issue.issue_id]


async def test_the_notice_opens_the_guided_check_and_do_not_check_closes_it(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    await update(hass, device, FIRMWARE_B)
    await conclude(hass)
    (issue,) = firmware_notices(hass)
    (file_notice,) = file_notices(hass)
    flow = await start_fix(hass, issue)
    assert flow["type"] is FlowResultType.MENU and flow["step_id"] == "confirm" and flow["menu_options"] == ["check", "skip"]
    assert NOTE in flow["description_placeholders"]["devices"]
    # Its text names both reasons for a day of watching.
    assert "new firmware" in STRINGS["issues"][issue.translation_key]["fix_flow"]["step"]["confirm"]["description"]
    flow = await continue_fix(hass, flow, {"next_step_id": "skip"})
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "skipped"
    assert firmware_notices(hass) == [] and [i.issue_id for i in file_notices(hass)] == [file_notice.issue_id]
    assert watch_of(hass, device)["closed"] is True
    await manager_of(hass).async_refresh()
    await conclude(hass)
    assert firmware_notices(hass) == []


async def test_a_device_check_from_the_notice_closes_it(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    await update(hass, device, FIRMWARE_B)
    await conclude(hass)
    (issue,) = firmware_notices(hass)
    answers(device)
    flow = await start_fix(hass, issue)
    flow = await ask(hass, flow, {"next_step_id": "check"})
    assert flow["type"] is FlowResultType.MENU and flow["step_id"] == "values", flow
    flow = await continue_fix(hass, flow, {"next_step_id": "all_match"})
    # The last page: one button that opens the form.
    assert flow["type"] is FlowResultType.FORM and flow["step_id"] == "report" and flow["data_schema"] is None
    assert firmware_notices(hass) == []
    assert watch_of(hass, device)["checked"] is True
    assert manager_of(hass).summary()["firmware_checks"][0]["device_check_done"] is True


@pytest.mark.parametrize(("thumbs", "notice"), [(1, False), (0, True)])
async def test_the_notice_asks_only_below_the_threshold(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, thumbs: int, notice: bool):
    """Threshold 2: the oldest open issue of the new firmware with one thumbs-up reaches it. Issues of the old firmware
    count nothing for the new one."""
    (device,), search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    search.reply = answer(hit(3, title(FIRMWARE_C), thumbs=thumbs), hit(4, title(FIRMWARE_A), thumbs=0))
    await update(hass, device, FIRMWARE_C)
    fine_values(device)
    await conclude(hass)
    assert watch_of(hass, device)["done"] == "fine"
    assert len(firmware_notices(hass)) == (1 if notice else 0)


async def test_looks_off_comes_past_the_threshold(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    (device,), search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    search.reply = answer(hit(3, title(FIRMWARE_B), thumbs=40))
    await update(hass, device, FIRMWARE_B)
    device.value("temperature", "3276.7", device_class="temperature", unit_of_measurement="°C")
    await conclude(hass)
    (issue,) = firmware_notices(hass)
    assert issue.translation_key == checks.OFF and issue.data["device"] == device.device_id


async def test_a_running_self_check_of_the_file_takes_the_change(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    """The file's self-check watches the device already: no second watch. Its end decides on the firmware the device
    runs then. Both versions count as watched."""
    prepare(repo_dir)
    device = radar(hass, zha)
    set_firmware(hass, device.device_id, FIRMWARE_A)
    Search(aioclient_mock)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    await update(hass, device, FIRMWARE_B)
    assert started == [] and watch_of(hass, device) is None
    assert sorted(kept(hass)[device.device_id]["watched"]) == [FIRMWARE_A, FIRMWARE_B]
    await conclude(hass)
    assert len(file_notices(hass)) == 1 and firmware_notices(hass) == []
    await update(hass, device, FIRMWARE_A)
    assert started == []
    await update(hass, device, FIRMWARE_C)
    assert started == [(device.device_id, FIRMWARE_C)]


async def test_unknown_to_a_version_during_a_self_check_counts_as_watched(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    prepare(repo_dir)
    device = radar(hass, zha)
    Search(aioclient_mock)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    await update(hass, device, FIRMWARE_A)
    await conclude(hass)
    await update(hass, device, FIRMWARE_B)
    await update(hass, device, FIRMWARE_A)
    assert started == [(device.device_id, FIRMWARE_B)]


@pytest.mark.parametrize("covered", [True, False], ids=["first_version_watched", "first_version_only_recorded"])
async def test_a_flipping_firmware_is_watched_once_per_version(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started, covered: bool):
    """A device that flips between two versions brings one watch per version, not a notice a day."""
    if covered:
        # The file's self-check watched it on A.
        (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock)
        expected = [(device.device_id, FIRMWARE_B)]
    else:
        # A came after the file's self-check: only recorded.
        (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock, None)
        await update(hass, device, FIRMWARE_A)
        expected = [(device.device_id, FIRMWARE_B), (device.device_id, FIRMWARE_A)]
    notices = set()
    for firmware in (FIRMWARE_B, FIRMWARE_A) * 3:
        await update(hass, device, firmware)
        await conclude(hass)
        notices |= {issue.issue_id for issue in firmware_notices(hass)}
        # A day passes.
        await conclude(hass)
    assert started == expected
    assert len(notices) == len(expected)
    assert len(firmware_notices(hass)) <= 1


async def test_a_new_version_replaces_the_watch_of_the_last_one(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    """The watch of B is over with its notice, the device gets C: the notice of B goes, C is watched for a day."""
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    await update(hass, device, FIRMWARE_B)
    await conclude(hass)
    (first,) = firmware_notices(hass)
    set_firmware(hass, device.device_id, FIRMWARE_C)
    # Gone with the watch, not only at the end of a refresh that brings the notices up to date (a held list does not).
    manager_of(hass).checks.sync()
    assert ir.async_get(hass).async_get_issue(const.DOMAIN, first.issue_id) is None
    await settle(hass)
    assert watch_of(hass, device)["firmware"] == FIRMWARE_C and not watch_of(hass, device).get("done")
    # C while B is still watched: a new day for C.
    await update(hass, device, "0x00000099")
    assert watch_of(hass, device)["firmware"] == "0x00000099"
    assert started == [(device.device_id, FIRMWARE_B), (device.device_id, FIRMWARE_C), (device.device_id, "0x00000099")]


async def test_a_watch_lasts_a_day_of_zha_running(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, freezer):
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    await update(hass, device, FIRMWARE_B)
    await watch_for(hass, freezer, 10)
    (summary,) = manager_of(hass).summary()["firmware_checks"]
    assert summary["result"] == "watching" and 9 <= summary["observed_hours"] <= 11
    await watch_for(hass, freezer, 15)
    (summary,) = manager_of(hass).summary()["firmware_checks"]
    assert summary["result"] == "unproven" and len(firmware_notices(hass)) == 1


async def test_a_file_self_check_and_a_firmware_watch_of_the_file_search_each(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    """The hall radar came to the file after its self-check started: its firmware watch runs beside it. Both end at one
    count, each with its own search."""
    prepare(repo_dir)
    kitchen = radar(hass, zha)
    set_firmware(hass, kitchen.device_id, FIRMWARE_A)
    search = Search(aioclient_mock)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    hallway = hall(hass, zha)
    set_firmware(hass, hallway.device_id, FIRMWARE_B)
    zha.apply_quirks({IEEE_B: TUYA_A})
    await settle(hass)
    assert len(self_check(hass)["devices"]) == 1
    await update(hass, hallway, FIRMWARE_C)
    assert started == [(hallway.device_id, FIRMWARE_C)]
    await conclude(hass)
    assert search.calls == 2
    assert self_check(hass)["result"] == "unproven" and watch_of(hass, hallway)["done"] == "unproven"
    assert len(file_notices(hass)) == 1 and len(firmware_notices(hass)) == 1


@pytest.mark.parametrize("watching", [False, True], ids=["no_watch_before", "watch_before"])
async def test_no_watch_while_a_change_of_the_file_waits_for_zha(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started, watching: bool):
    """The update of the file is written, ZHA has not loaded it: a watch of the revision before ends at once, none
    starts. The self-check of the new revision after the reload watches the device on its new firmware."""
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    if watching:
        await update(hass, device, FIRMWARE_C)
    prepare(repo_dir, version=2)
    REPOS[repo_dir].publish()
    await manager_of(hass).async_check()
    await apply_list(hass)
    assert manager_of(hass).data["pending"]["changes"]
    await manager_of(hass).async_refresh()
    assert watch_of(hass, device) is None
    await update(hass, device, FIRMWARE_B)
    assert len(started) == (1 if watching else 0) and watch_of(hass, device) is None
    zha.reload()
    await settle(hass)
    assert self_check(hass)["result"] == "watching" and kept(hass)[device.device_id]["watched"] == [FIRMWARE_B]
    assert len(started) == (1 if watching else 0)


# Restarts


async def test_a_restart_during_a_watch_resumes_it(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    await update(hass, device, FIRMWARE_B)
    fields = ("file", "device", "firmware", "revision", "started", "devices")
    before = {field: copy.deepcopy(watch_of(hass, device)[field]) for field in fields}
    await restart_with(hass, hass.config_entries.async_entries(const.DOMAIN)[0], repo_dir)
    await manager_of(hass).async_refresh()
    assert {field: watch_of(hass, device)[field] for field in fields} == before and not watch_of(hass, device).get("done")
    assert started == [(device.device_id, FIRMWARE_B)]
    await conclude(hass)
    assert len(firmware_notices(hass)) == 1
    await restart_with(hass, hass.config_entries.async_entries(const.DOMAIN)[0], repo_dir)
    await conclude(hass)
    assert len(firmware_notices(hass)) == 1 and started == [(device.device_id, FIRMWARE_B)]


@pytest.mark.parametrize("on_disk", [True, False], ids=["saved", "lost"])
async def test_a_hard_stop_during_a_watch_keeps_one_watch(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, hass_storage, started, on_disk: bool):
    """A watch is on disk as soon as it starts. Lost with the version (a hard stop before the write), the next start sees
    the change again and watches once."""
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    await flush_store(manager_of(hass).checks._store)
    written = copy.deepcopy(hass_storage[checks.STORAGE_KEY])
    await update(hass, device, FIRMWARE_B)
    if on_disk:
        written = copy.deepcopy(hass_storage[checks.STORAGE_KEY])
        assert written["data"]["firmware"][device.device_id]["watch"]["firmware"] == FIRMWARE_B
    await hard_restart(hass, entry, hass_storage, written)
    await manager_of(hass).async_refresh()
    assert watch_of(hass, device)["firmware"] == FIRMWARE_B and not watch_of(hass, device).get("done")
    assert len(started) == (1 if on_disk else 2)
    await conclude(hass)
    await manager_of(hass).async_refresh()
    assert len(firmware_notices(hass)) == 1 and len(started) == (1 if on_disk else 2)


# Ends


@pytest.mark.parametrize("state", ["watching", "notice"])
@pytest.mark.parametrize("event", ["removed", "left_out", "no_quirk"])
async def test_a_device_that_leaves_drops_its_watch_and_versions(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started, state: str, event: str):
    (kitchen, hallway), _search = await done_installed(hass, zha, repo_dir, aioclient_mock, second=True)
    await update(hass, kitchen, FIRMWARE_C)
    if state == "notice":
        await conclude(hass)
        assert len(firmware_notices(hass)) == 1
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    if event == "removed":
        zha.remove_device(kitchen.device_id)
        await settle(hass)
    elif event == "left_out":
        hass.config_entries.async_update_entry(entry, options={const.CONF_EXCLUDED: [kitchen.device_id]})
        await hass.async_block_till_done(wait_background_tasks=True)
    else:
        zha.apply_quirks({IEEE_A: NO_QUIRK})
        await manager_of(hass).async_refresh()
    await manager_of(hass).async_refresh()
    assert kitchen.device_id not in kept(hass) and hallway.device_id in kept(hass)
    assert firmware_notices(hass) == []
    await conclude(hass)
    assert firmware_notices(hass) == [] and manager_of(hass).summary()["firmware_checks"] == []
    if event == "left_out":
        # Taken back in, it starts afresh: its version is recorded, not watched.
        hass.config_entries.async_update_entry(entry, options={})
        await hass.async_block_till_done(wait_background_tasks=True)
        await manager_of(hass).async_refresh()
        assert kept(hass)[kitchen.device_id]["version"] == FIRMWARE_C and watch_of(hass, kitchen) is None
        assert len(started) == 1


@pytest.mark.parametrize("state", ["watching", "notice"])
@pytest.mark.parametrize("event", ["updated", "deleted", "installed_again"])
async def test_a_file_updated_or_removed_drops_its_watches(hass: HomeAssistant, zha: Zha, repo_dir: Path, quirks_dir: Path, aioclient_mock, started,
                                                           state: str, event: str):
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    old = self_check(hass)["revision"]
    await update(hass, device, FIRMWARE_B)
    if state == "notice":
        await conclude(hass)
        assert len(firmware_notices(hass)) == 1
    if event == "updated":
        prepare(repo_dir, version=2)
        REPOS[repo_dir].publish()
        await manager_of(hass).async_check()
        await apply_list(hass)
        zha.reload()
        await settle(hass)
    elif event == "deleted":
        (quirks_dir / FILE).unlink()
        await manager_of(hass).async_refresh()
    else:
        # Left out, the file goes. Taken back in, the same revision comes back: its self-check watches the device.
        for excluded, quirk in (([device.device_id], NO_QUIRK), ([], TUYA_A)):
            hass.config_entries.async_update_entry(entry, options={const.CONF_EXCLUDED: excluded})
            await hass.async_block_till_done(wait_background_tasks=True)
            await apply_list(hass)
            zha.reload({IEEE_A: quirk})
            await settle(hass)
    await manager_of(hass).async_refresh()
    assert watch_of(hass, device) is None and firmware_notices(hass) == []
    if event == "deleted":
        assert device.device_id not in kept(hass)
    else:
        # The file's self-check of the new (or the same) revision watches the device on B.
        assert self_check(hass)["result"] == "watching" and (self_check(hass)["revision"] != old) is (event == "updated")
        assert kept(hass)[device.device_id]["watched"] == [FIRMWARE_B]
    await conclude(hass)
    assert firmware_notices(hass) == [] and len(started) == 1


@pytest.mark.parametrize("state", ["watching", "fine_notice", "off_notice"])
async def test_a_file_verified_during_a_watch_ends_it_without_a_notice(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started, state: str):
    """The next content state calls the file verified: a running watch ends without a notice, a fine notice goes. "Looks
    off" stays."""
    (device,), search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    await update(hass, device, FIRMWARE_B)
    if state != "watching":
        if state == "off_notice":
            device.value("temperature", "3276.7", device_class="temperature", unit_of_measurement="°C")
        else:
            fine_values(device)
        await conclude(hass)
        (issue,) = firmware_notices(hass)
    prepare(repo_dir, keyed(tuya_a={"status": "verified"}))
    REPOS[repo_dir].publish()
    await manager_of(hass).async_check()
    await hass.async_block_till_done(wait_background_tasks=True)
    before = search.calls
    if state == "watching":
        assert watch_of(hass, device)["done"] == "verified"
        fine_values(device)
        await conclude(hass)
        assert firmware_notices(hass) == [] and search.calls == before
    else:
        await manager_of(hass).async_refresh()
        stays = state == "off_notice"
        assert (ir.async_get(hass).async_get_issue(const.DOMAIN, issue.issue_id) is not None) is stays
    # No new watch for a verified file.
    await update(hass, device, FIRMWARE_C)
    assert started == [(device.device_id, FIRMWARE_B)]


async def test_a_watch_whose_file_is_verified_at_its_end_brings_no_notice(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """Not even "looks off": the firmware watch matters only until the file is verified."""
    (device,), search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    await update(hass, device, FIRMWARE_B)
    device.value("temperature", "3276.7", device_class="temperature", unit_of_measurement="°C")
    before = search.calls
    with patch.object(checks.Checks, "file_verified", return_value=True):
        await conclude(hass)
    assert watch_of(hass, device)["done"] == "verified" and firmware_notices(hass) == [] and search.calls == before


# Ignored


async def test_a_device_without_a_file_of_ours_is_ignored(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    other = acme(hass, zha)
    set_firmware(hass, other.device_id, FIRMWARE_A)
    await settle(hass)
    await update(hass, other, FIRMWARE_B)
    assert other.device_id not in kept(hass) and started == []


async def test_a_verified_file_is_ignored(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock, manifest=keyed(tuya_a={"status": "verified"}))
    await update(hass, device, FIRMWARE_B)
    assert started == [] and watch_of(hass, device) is None
    assert kept(hass)[device.device_id]["version"] == FIRMWARE_B


async def newer_revision(hass: HomeAssistant, repo_dir: Path, **fields) -> str:
    """The next content state ships z2m_tuya_a.py at a new revision with the same key (a new docstring), the update is
    not applied. The revision installed."""
    installed_revision = self_check(hass)["revision"]
    prepare(repo_dir, keyed(tuya_a=fields), version=2)
    REPOS[repo_dir].publish()
    await manager_of(hass).async_check()
    await hass.async_block_till_done(wait_background_tasks=True)
    source = manager_of(hass).source
    assert source.revisions[FILE] != installed_revision and source.keys[FILE] == "1a2b3c4d"
    return installed_revision


async def test_a_file_verified_at_a_newer_revision_is_ignored(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    """The state in use ships the file verified at a new revision, the update waits: the key its self-check kept says it
    is the same file. No watch, no notice."""
    (device,), search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    await newer_revision(hass, repo_dir, status="verified")
    await update(hass, device, FIRMWARE_B)
    fine_values(device)
    await conclude(hass)
    assert started == [] and watch_of(hass, device) is None and firmware_notices(hass) == []


async def test_a_watch_while_an_update_waits_searches_with_the_kept_key(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    """The state in use ships the file at a new revision (not verified), the update waits: the watch keeps the key of the
    file's self-check and searches. A later state that ships the file verified ends it."""
    (device,), search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    installed_revision = await newer_revision(hass, repo_dir)
    await update(hass, device, FIRMWARE_B)
    watch = watch_of(hass, device)
    assert started == [(device.device_id, FIRMWARE_B)] and watch["revision"] == installed_revision and watch["key"] == "1a2b3c4d"
    search.reply = answer(hit(3, title(FIRMWARE_B), thumbs=1))
    before = search.calls
    fine_values(device)
    await conclude(hass)
    assert search.calls == before + 1 and watch_of(hass, device)["done"] == "fine" and firmware_notices(hass) == []
    # Watched again on the next version, then the file is verified (another revision again): the watch ends.
    await update(hass, device, FIRMWARE_C)
    assert not watch_of(hass, device).get("done")
    prepare(repo_dir, keyed(tuya_a={"status": "verified"}), version=3)
    REPOS[repo_dir].publish()
    await manager_of(hass).async_check()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert watch_of(hass, device)["done"] == "verified"


async def test_a_watch_searches_with_the_state_key_of_its_revision(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    """The state in use states another key for the revision a watch is for (it kept KEY_A): the watch searches with the
    state's key, so does a watch started after it."""
    (device,), search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    await update(hass, device, FIRMWARE_B)
    assert watch_of(hass, device)["key"] == "1a2b3c4d"
    revision = await same_revision(hass, repo_dir, NEW_KEY)
    before = len(searches(aioclient_mock))
    fine_values(device)
    await conclude(hass)
    (url,) = searches(aioclient_mock)[before:]
    assert url.query["q"].endswith(f" {NEW_KEY}")
    assert manager_of(hass).checks.check_key(FILE, revision) == NEW_KEY
    await update(hass, device, FIRMWARE_C)
    assert watch_of(hass, device)["key"] == NEW_KEY


# Bounds


async def test_the_versions_watched_are_bounded_per_device(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    """At most MAX_VERSIONS per device, the oldest goes first: a version dropped is watched again."""
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    versions = [f"0x000001{n:02x}" for n in range(checks.MAX_VERSIONS + 2)]
    for version in versions:
        await update(hass, device, version)
    assert kept(hass)[device.device_id]["watched"] == versions[-checks.MAX_VERSIONS:]
    assert len(started) == len(versions)
    await update(hass, device, FIRMWARE_A)
    assert started[-1] == (device.device_id, FIRMWARE_A)
    await update(hass, device, versions[-1])
    assert len(started) == len(versions) + 1
    shape_ok(kept(hass))


async def test_the_devices_kept_are_bounded(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    """Past MAX_FIRMWARE devices a further one is not recorded (and not watched) until one goes: no churn."""
    with patch.object(checks, "MAX_FIRMWARE", 1):
        (kitchen, hallway), _search = await done_installed(hass, zha, repo_dir, aioclient_mock, second=True)
        assert len(kept(hass)) == 1
        (first,) = kept(hass)
        other = hallway if first == kitchen.device_id else kitchen
        await update(hass, other, FIRMWARE_C)
        assert list(kept(hass)) == [first] and started == []
        zha.remove_device(first)
        await settle(hass)
        await manager_of(hass).async_refresh()
        assert list(kept(hass)) == [other.device_id] and kept(hass)[other.device_id]["version"] == FIRMWARE_C


def hostile_records(device_id: str, revision: str) -> list[object]:
    good = {"file": FILE, "revision": revision, "version": FIRMWARE_A, "watched": [FIRMWARE_A]}
    watch = {"file": FILE, "device": device_id, "firmware": FIRMWARE_B, "revision": revision, "started": 1.0, "observed": 0.0,
             "devices": {device_id: {"fresh": [], "frame": False, "answered": False, "unmapped": [], "findings": {}}}}
    return [
        [], "x", 5, None,
        {device_id: "x"},
        {device_id: {"file": 5}},
        {device_id: {"version": FIRMWARE_A}},
        {device_id: {**good, "version": 7, "watched": "abc", "revision": []}},
        {device_id: {**good, "watched": [1, None, FIRMWARE_B, FIRMWARE_B, "x" * 10_000, {"a": 1}] + [f"v{n}" for n in range(50)]}},
        {device_id: {**good, "version": "x" * 10_000}},
        {device_id: {**good, "watch": "x"}},
        {device_id: {**good, "watch": {**watch, "devices": "x"}}},
        {device_id: {**good, "watch": {**watch, "devices": {"another": watch["devices"][device_id]}}}},
        {device_id: {**good, "watch": {**watch, "devices": {device_id: watch["devices"][device_id], "another": watch["devices"][device_id]}}}},
        {device_id: {**good, "watch": {**watch, "firmware": 5}}},
        {device_id: {**good, "watch": {**watch, "firmware": "x" * 10_000}}},
        {device_id: {**good, "watch": {**watch, "device": "another"}}},
        {device_id: {**good, "watch": {**watch, "file": "z2m_other.py"}}},
        {device_id: {**good, "watch": {**watch, "revision": 5}}},
        {device_id: {**good, "watch": {**watch, "started": "yesterday"}}},
        {device_id: {**good, "watch": {key: value for key, value in watch.items() if key != "observed"}}},
        {device_id: {**good, "watch": {**watch, "key": ["x"], "done": 5, "notice": 7, "closed": "x"}}},
        {f"device{n}": dict(good) for n in range(1000)},
    ]


@pytest.mark.parametrize("index", range(len(hostile_records("d", "r"))))
def test_hostile_kept_versions_are_cut_to_shape(index: int):
    shape_ok(checks.Checks._load_firmware(hostile_records("d", "r")[index]))


async def test_a_stored_watch_beside_the_files_self_check_of_the_device_goes(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, hass_storage):
    """Never both: a store that holds a firmware watch of a device the file's running self-check watches keeps the
    file's."""
    prepare(repo_dir)
    device = radar(hass, zha)
    set_firmware(hass, device.device_id, FIRMWARE_A)
    Search(aioclient_mock)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    await flush_store(manager_of(hass).checks._store)
    written = copy.deepcopy(hass_storage[checks.STORAGE_KEY])
    revision = self_check(hass)["revision"]
    written["data"]["firmware"][device.device_id]["watch"] = {
        "file": FILE, "device": device.device_id, "firmware": FIRMWARE_A, "revision": revision, "started": 1.0, "observed": 0.0,
        "devices": {device.device_id: {"fresh": [], "frame": False, "answered": False, "unmapped": [], "findings": {}}}}
    await hard_restart(hass, entry, hass_storage, written)
    await manager_of(hass).async_refresh()
    assert watch_of(hass, device) is None and self_check(hass)["result"] == "watching"


async def test_without_the_quirks_folder_the_versions_stay(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    """While ZHA names no custom quirks folder nothing tells which file a device runs: nothing is dropped or started. The
    change is seen when the folder is back."""
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    await update(hass, device, FIRMWARE_B)
    folder = zha.data.yaml_config.pop("custom_quirks_path")
    await manager_of(hass).async_refresh()
    assert watch_of(hass, device)["firmware"] == FIRMWARE_B
    await update(hass, device, FIRMWARE_C)
    assert watch_of(hass, device)["firmware"] == FIRMWARE_B and len(started) == 1
    zha.data.yaml_config["custom_quirks_path"] = folder
    await manager_of(hass).async_refresh()
    assert watch_of(hass, device)["firmware"] == FIRMWARE_C and len(started) == 2


@pytest.mark.parametrize("index", range(len(hostile_records("d", "r"))))
async def test_hostile_kept_versions_are_dropped_on_load(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, hass_storage, index: int):
    """Wrong types, missing fields and oversized parts in the store: dropped or cut when the integration loads, no crash.
    What is left works as before."""
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    await flush_store(manager_of(hass).checks._store)
    written = copy.deepcopy(hass_storage[checks.STORAGE_KEY])
    written["data"]["firmware"] = hostile_records(device.device_id, self_check(hass)["revision"])[index]
    await hard_restart(hass, entry, hass_storage, written)
    shape_ok(kept(hass))
    json.dumps(manager_of(hass).summary())
    await manager_of(hass).async_refresh()
    await update(hass, device, FIRMWARE_C)
    await conclude(hass)
    shape_ok(kept(hass))
    assert len(firmware_notices(hass)) <= 1
    await flush_store(manager_of(hass).checks._store)
    assert len(json.dumps(hass_storage[checks.STORAGE_KEY])) < 100_000


# The Tuya data query at the start of a watch


async def test_a_watch_asks_its_tuya_device_only(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    """A Tuya device sends its values by itself only on a change: the device whose watch starts gets the data query an
    install sends, the file's other devices are not asked again. Its answer counts for the watch."""
    (kitchen, hallway), _search = await done_installed(hass, zha, repo_dir, aioclient_mock, second=True)
    tuya, other = kitchen.clusters[0xEF00], hallway.clusters[0xEF00]
    assert tuya.sent == other.sent == [QUERY]
    tuya.answer = lambda: tuya.frame(1, 2, 3)
    manager_of(hass).queries.clear()
    await update(hass, kitchen, FIRMWARE_C)
    assert started == [(kitchen.device_id, FIRMWARE_C)]
    assert tuya.sent == [QUERY, QUERY] and other.sent == [QUERY]
    assert queries(hass) == {FILE: ["_TZE200_aaaa TS0601: sent"]}
    assert watch_of(hass, kitchen)["devices"][kitchen.device_id]["answered"] is True
    # Once: neither a refresh nor a reload of ZHA asks again.
    await manager_of(hass).async_refresh()
    zha.reload()
    await settle(hass)
    assert tuya.sent == [QUERY, QUERY] and other.sent == [QUERY]
    assert not watch_of(hass, kitchen).get("done")


async def test_a_watch_on_a_device_without_a_tuya_cluster_asks_nothing(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    """A ZCL device on a ZCL file: nothing to ask, nothing listed."""
    prepare(repo_dir)
    device = acme(hass, zha)
    set_firmware(hass, device.device_id, FIRMWARE_A)
    Search(aioclient_mock)
    await installed(hass, zha, repo_dir, {IEEE_C: ACME_C})
    await conclude(hass)
    manager_of(hass).queries.clear()
    await update(hass, device, FIRMWARE_B)
    assert started == [(device.device_id, FIRMWARE_B)] and not watch_of(hass, device).get("done")
    assert all(cluster.sent == [] for cluster in device.clusters.values())
    assert queries(hass) == {}


async def test_a_watch_whose_quirk_has_no_tuya_query_asks_nothing(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    """The device has a Tuya MCU cluster, but the file's quirk gives it no data query: listed as skipped, as after an
    install."""
    prepare(repo_dir)
    device = radar(hass, zha, handled=None)
    set_firmware(hass, device.device_id, FIRMWARE_A)
    Search(aioclient_mock)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    await conclude(hass)
    manager_of(hass).queries.clear()
    await update(hass, device, FIRMWARE_B)
    assert started == [(device.device_id, FIRMWARE_B)] and not watch_of(hass, device).get("done")
    assert device.clusters[0xEF00].sent == []
    assert queries(hass) == {FILE: ["_TZE200_aaaa TS0601: skipped, its quirk has no Tuya data query"]}


async def test_a_watch_on_an_unavailable_device_does_not_ask(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started):
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    zha.unavailable.add(IEEE_A)
    zha._publish()
    manager_of(hass).queries.clear()
    await update(hass, device, FIRMWARE_B)
    assert started == [(device.device_id, FIRMWARE_B)] and not watch_of(hass, device).get("done")
    assert device.clusters[0xEF00].sent == [QUERY]
    assert queries(hass) == {FILE: ["_TZE200_aaaa TS0601: skipped, ZHA reports it unavailable"]}


@pytest.mark.parametrize("error", [DeliveryError("MAC transaction expired"), asyncio.TimeoutError()], ids=["not_delivered", "timeout"])
async def test_a_query_that_fails_leaves_the_watch_running(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started,
                                                           caplog: pytest.LogCaptureFixture, error: Exception):
    """A sleepy device may miss the query: best effort, not repeated, nothing above debug. The watch goes on and ends
    with its notice."""
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    tuya = device.clusters[0xEF00]

    async def failing(command_id: int, *args, **kwargs) -> None:
        tuya.sent.append(command_id)
        raise error

    tuya.command = failing
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        await update(hass, device, FIRMWARE_B)
    assert started == [(device.device_id, FIRMWARE_B)] and tuya.sent == [QUERY, QUERY]
    assert queries(hass) == {FILE: [f"_TZE200_aaaa TS0601: not delivered ({str(error) or type(error).__name__})"]}
    ours = [record for record in caplog.records if record.name.startswith(PACKAGE)]
    assert all(record.levelno <= logging.DEBUG for record in ours), [record.getMessage() for record in ours if record.levelno > logging.DEBUG]
    watch = watch_of(hass, device)
    assert not watch.get("done") and watch["devices"][device.device_id]["answered"] is False
    fine_values(device)
    await conclude(hass)
    (issue,) = firmware_notices(hass)
    assert issue.translation_key == checks.FINE and watch_of(hass, device)["done"] == "fine"
    assert tuya.sent == [QUERY, QUERY]


async def test_a_restart_during_a_watch_asks_nothing_again_like_the_files_self_check(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock,
                                                                                     started):
    """The query goes out when a self-check starts, not when a running one resumes after a restart: the file's self-check
    and a firmware watch alike."""
    prepare(repo_dir)
    device = radar(hass, zha)
    tuya = device.clusters[0xEF00]
    set_firmware(hass, device.device_id, FIRMWARE_A)
    Search(aioclient_mock)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    assert tuya.sent == [QUERY] and self_check(hass)["result"] == "watching"
    await restart_with(hass, entry, repo_dir)
    await manager_of(hass).async_refresh()
    assert tuya.sent == [QUERY] and self_check(hass)["result"] == "watching"
    await conclude(hass)
    await update(hass, device, FIRMWARE_B)
    assert tuya.sent == [QUERY, QUERY]
    await restart_with(hass, entry, repo_dir)
    await manager_of(hass).async_refresh()
    assert tuya.sent == [QUERY, QUERY] and started == [(device.device_id, FIRMWARE_B)]
    assert watch_of(hass, device)["firmware"] == FIRMWARE_B and not watch_of(hass, device).get("done")


@pytest.mark.parametrize("on_disk", [True, False], ids=["saved", "lost"])
async def test_a_hard_stop_during_a_watch_asks_again_only_when_the_watch_starts_again(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock,
                                                                                      hass_storage, started, on_disk: bool):
    """Saved, the watch resumes without a query. Lost with the version, the next start watches anew and asks once more."""
    (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock)
    tuya = device.clusters[0xEF00]
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    await flush_store(manager_of(hass).checks._store)
    written = copy.deepcopy(hass_storage[checks.STORAGE_KEY])
    await update(hass, device, FIRMWARE_B)
    if on_disk:
        written = copy.deepcopy(hass_storage[checks.STORAGE_KEY])
    await hard_restart(hass, entry, hass_storage, written)
    await manager_of(hass).async_refresh()
    assert len(started) == (1 if on_disk else 2)
    assert tuya.sent == [QUERY] * (2 if on_disk else 3)


@pytest.mark.parametrize("case", ["watched_before", "self_check_runs", "verified", "unknown_before"])
async def test_no_new_watch_asks_nothing(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, started, case: str):
    """No query without a watch that starts: a version watched before (the flip back and forth), a change the file's
    running self-check takes, a verified file, a version after none."""
    if case == "self_check_runs":
        prepare(repo_dir)
        device = radar(hass, zha)
        set_firmware(hass, device.device_id, FIRMWARE_A)
        Search(aioclient_mock)
        await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    else:
        manifest = keyed(tuya_a={"status": "verified"}) if case == "verified" else None
        (device,), _search = await done_installed(hass, zha, repo_dir, aioclient_mock, None if case == "unknown_before" else FIRMWARE_A, manifest=manifest)
    tuya = device.clusters[0xEF00]
    if case == "watched_before":
        await update(hass, device, FIRMWARE_B)
        assert started == [(device.device_id, FIRMWARE_B)]
        started.clear()
    before = list(tuya.sent)
    for firmware in [FIRMWARE_A, FIRMWARE_B] if case == "watched_before" else [FIRMWARE_A if case == "unknown_before" else FIRMWARE_B]:
        await update(hass, device, firmware)
    assert started == [] and tuya.sent == before
