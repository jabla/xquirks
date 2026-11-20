"""A self-check notice and the device check it hands over to.

The notice's fix flow is its first page: what the notice found, "Check the device now" or "Do not check". Before it
shows, one search (short timeout) tells whether every device of the notice is past the threshold now: then the notice
goes. "Check the device now" hands over to the device check, a subentry flow of the entry (source "notice"): the fix flow
ends with an abort that names it as the next flow, the notice stays, the frontend opens the device check's dialog. The
device check is the one of the integration page's button: the device picker (limited to the notice's devices, skipped
for one), the query with its progress step, the values. Then "All values match" (a thumbs-up on the open issue of the
variant, else the filled-in form) or "Something does not match" (the list, then the problem report). Each last page has
one button that opens its link as an external step: the subentry dialog shows our text there (no title, no Close). The
notice closes only with that button (or with "Do not check"): the dialog's X on any page leaves it, the next opening
starts on its first page. No test reaches the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components.repairs.const import FlowType
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData, UnknownFlow, UnknownStep
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMockResponse
from yarl import URL

from . import integration
from .conftest import REPOS, STRINGS, Zha, check_device_check_texts, continue_check, continue_fix, issue_of, issues_of, manager_of, setup_entry, start_fix
from .integration import confirm, const, diagnostics, pictures
from .test_checks import ACME_C, IEEE_A, IEEE_C, NAME, TUYA_A, LiveDevice, acme, checks, conclude, form_ids, installed, link_fields, open_check, radar, report_link, run_check, text
from .test_confirm_search import (
    FIRMWARE_A,
    FIRMWARE_B,
    HALL,
    IEEE_B,
    KEY_A,
    NEW_KEY,
    Gate,
    answer,
    apply_now,
    fine_values,
    hall,
    hit,
    keyed,
    prepare,
    same_revision,
    searches,
    set_firmware,
    settle_now,
    title,
)
from .test_content import restart_with
from .test_pictures import png, stored

repairs = importlib.import_module(f"{integration.PACKAGE}.repairs")
config_flow = importlib.import_module(f"{integration.PACKAGE}.config_flow")
links = importlib.import_module(f"{integration.PACKAGE}.links")

FINE, OFF, UNPROVEN = checks.FINE, checks.OFF, checks.UNPROVEN
ISSUE_LINK = f"https://github.com/{const.REPOSITORY}/issues/{{number}}"
# The first page of every kind (its text differs).
FIRST = "confirm"
# The pages a device check ends with: one button each, which opens a link.
LAST_PAGES = ("report", "report_wrong", "thumbs_up")
# The texts of the device check.
CHECK = STRINGS["config_subentries"]["device_check"]


class Search:
    """GitHub's search as the tests answer it: reply is a JSON body, a status (int), an exception, a Gate or a coroutine
    function called as a Gate is."""

    def __init__(self, aioclient_mock, reply: object = None) -> None:
        self.reply = answer() if reply is None else reply
        self.calls = 0
        aioclient_mock.get(const.SEARCH_URL, side_effect=self)

    async def __call__(self, method, url, data):
        self.calls += 1
        reply = self.reply
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, int):
            return AiohttpClientMockResponse(method, URL(url), status=reply)
        if callable(reply):
            return await reply(method, url, data)
        return AiohttpClientMockResponse(method, URL(url), json=reply)


async def notice(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, kind: str = FINE, *, threshold: object = 3, manifest: dict | None = None,
                 second: bool = False) -> tuple[list[LiveDevice], Search]:
    """A self-check notice of this kind for z2m_tuya_a.py on the kitchen radar (firmware A) and, with second, the hall
    radar (firmware B). The search at its end found no issue."""
    prepare(repo_dir, manifest, threshold=threshold)
    devices = [radar(hass, zha)]
    set_firmware(hass, devices[0].device_id, FIRMWARE_A)
    quirks = {IEEE_A: TUYA_A}
    if second:
        devices.append(hall(hass, zha))
        set_firmware(hass, devices[1].device_id, FIRMWARE_B)
        quirks[IEEE_B] = TUYA_A
    search = Search(aioclient_mock)
    await installed(hass, zha, repo_dir, quirks)
    for device in devices:
        if kind == FINE:
            fine_values(device)
        elif kind == OFF:
            device.value("temperature", "3276.7", device_class="temperature", unit_of_measurement="°C")
    await conclude(hass)
    issue = issue_of(hass, kind)
    assert issue.is_fixable and issue.is_persistent
    return devices, search


def answers(device: LiveDevice) -> None:
    """The device answers a data query with values that look right."""
    device.clusters[0xEF00].answer = lambda: fine_values(device)


def device_checks(hass: HomeAssistant) -> list[dict]:
    """The device checks in progress."""
    return [flow for flow in hass.config_entries.subentries.async_progress() if flow["handler"][1] == "device_check"]


async def check_now(hass: HomeAssistant, first: dict) -> dict:
    """"Check the device now" on the first page. A device check that cannot start ends the fix flow with its reason (no
    device check left). Otherwise the fix flow ends with an abort that names the device check as the next flow: the
    notice stays as it was (Home Assistant deletes it only after a create_entry). The device check as the frontend's
    dialog fetches it: a GET runs its current step again without input."""
    issue_id = hass.data["repairs"]["flow_manager"].async_get(first["flow_id"])["context"]["issue_id"]
    before = {flow["flow_id"] for flow in device_checks(hass)}
    handed = await continue_fix(hass, first, {"next_step_id": "check"})
    assert handed["type"] is FlowResultType.ABORT, handed
    if handed["reason"] != "check_started":
        assert "next_flow" not in handed and {flow["flow_id"] for flow in device_checks(hass)} == before
        return handed
    flow_type, flow_id = handed["next_flow"]
    assert flow_type is FlowType.CONFIG_SUBENTRIES_FLOW and handed["result"] is hass.config_entries.async_entries(const.DOMAIN)[0]
    (started,) = [flow for flow in device_checks(hass) if flow["flow_id"] not in before]
    assert started["flow_id"] == flow_id and started["handler"] == (handed["result"].entry_id, "device_check") and started["context"]["source"] == "notice"
    issue = ir.async_get(hass).async_get_issue(const.DOMAIN, issue_id)
    assert issue is not None and issue.dismissed_version is None and issue.is_fixable
    return await continue_check(hass, {"flow_id": flow_id, "handler": started["handler"], "kind": first["kind"]})


async def ask(hass: HomeAssistant, flow: dict, user_input: dict) -> dict:
    """Go on into the device check ("Check the device now" on the first page, or the picker): its progress step to the
    end, the page after it."""
    with patch.object(checks, "CHECK_WAIT", 0.3), patch.object(checks, "CHECK_QUIET", 0.05), patch.object(checks, "CHECK_STEP", 0.05):
        if isinstance(flow["handler"], tuple):
            flow = await continue_check(hass, flow, user_input)
        else:
            assert user_input == {"next_step_id": "check"}
            flow = await check_now(hass, flow)
        if flow["type"] is not FlowResultType.SHOW_PROGRESS:
            return flow
        assert flow["step_id"] == "ask" and flow["progress_action"] == "asking"
        await hass.async_block_till_done(wait_background_tasks=True)
    return await continue_fix(hass, flow)


async def values_page(hass: HomeAssistant, kind: str = FINE) -> dict:
    """The notice's flow up to the values: "Check the device now" with one device."""
    flow = await start_fix(hass, issue_of(hass, kind))
    assert flow["type"] is FlowResultType.MENU and flow["step_id"] == FIRST, flow
    flow = await ask(hass, flow, {"next_step_id": "check"})
    assert flow["type"] is FlowResultType.MENU and flow["step_id"] == "values" and flow["menu_options"] == ["all_match", "mismatch"], flow
    return flow


async def click(hass: HomeAssistant, flow: dict, step: str) -> str:
    """The last page: a page without fields whose one button (its own text: Home Assistant shows no Close on it) opens
    the link in the browser (an external step, it stays there until the dialog is closed). The link."""
    assert flow["type"] is FlowResultType.FORM and flow["step_id"] == step and flow["data_schema"] is None, flow
    assert CHECK["step"][step]["submit"]
    opened = await continue_fix(hass, flow, {})
    assert opened["type"] is FlowResultType.EXTERNAL_STEP and opened["step_id"] == opened_step(step), opened
    return opened["url"]


def opened_step(step: str) -> str:
    """The external step after the button of a last page: its title and text say what opened (the form or the device
    test)."""
    return "open_test" if step == "thumbs_up" else "open"


def last_page(flow: dict) -> str | None:
    """The step id of a last page (None for any other result)."""
    return flow["step_id"] if flow["type"] is FlowResultType.FORM and flow["data_schema"] is None else None


def gone(hass: HomeAssistant, kind: str = FINE) -> bool:
    return issues_of(hass, kind) == []


def self_check(hass: HomeAssistant, name: str = "z2m_tuya_a.py") -> dict | None:
    return manager_of(hass).summary()["self_check"].get(name)


async def comes_back(hass: HomeAssistant, repo_dir: Path, kind: str = FINE) -> bool:
    """Whether a refresh, a restart and the next counts bring a notice of this kind back."""
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    await manager_of(hass).async_refresh()
    await restart_with(hass, entry, repo_dir)
    await conclude(hass)
    return not gone(hass, kind)


# The first page


@pytest.mark.parametrize("kind", [FINE, UNPROVEN, OFF])
async def test_the_first_page_says_what_the_notice_found_and_offers_the_check(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, kind: str):
    """The notice opens its flow (a fixable notice shows no description): the first page carries what the notice found.
    "All values match" never stands on it."""
    await notice(hass, zha, repo_dir, aioclient_mock, kind)
    issue = issue_of(hass, kind)
    flow = await start_fix(hass, issue)
    assert flow["type"] is FlowResultType.MENU and flow["step_id"] == FIRST and flow["menu_options"] == ["check", "skip"]
    keys = ("picture", "file", "revision", "devices") + (("link",) if kind == OFF else ())
    # Looks off: the open reports of the file before the form, built by the flow (not stored with the notice).
    reports = {"reports": text.reports_url("z2m_tuya_a.py")} if kind == OFF else {}
    assert flow["description_placeholders"] == {key: issue.translation_placeholders[key] for key in keys} | reports
    assert f"- {NAME} (\\_TZE200\\_aaaa TS0601)" in flow["description_placeholders"]["devices"]
    strings = STRINGS["issues"][kind]["fix_flow"]["step"][FIRST]
    assert "all_match" not in strings["menu_options"] and "All values match" not in strings["description"]
    # Nothing changed by opening it.
    assert issue_of(hass, kind)


@pytest.mark.parametrize("kind", [FINE, OFF])
async def test_the_first_page_shows_the_picture_of_this_run(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, hass_client_no_auth, kind: str):
    """The notice keeps the picture of the run that made it. The address of a thumbnail carries a token of each Home
    Assistant run: after a restart the stored one answers 404. The first page builds the picture of this run."""
    picture = png(256, 256)
    stored(hass, picture).parent.mkdir(parents=True)
    stored(hass, picture).write_bytes(png(128, 128))
    pin = hashlib.sha256(picture).hexdigest()
    await notice(hass, zha, repo_dir, aioclient_mock, kind, manifest=keyed(tuya_a={"picture": "A.png", "picture_sha256": pin}))
    before = issue_of(hass, kind).translation_placeholders["picture"]
    assert pin in before
    # The token of another run.
    hass.data[pictures.DATA_PICTURES].token = "f" * 32
    flow = await start_fix(hass, issue_of(hass, kind))
    shown = flow["description_placeholders"]["picture"]
    assert shown == manager_of(hass).picture("z2m_tuya_a.py") != before and "f" * 32 in shown
    client = await hass_client_no_auth()
    thumbnail = re.compile(r"!\[\]\((/api/xquirks/picture/[^)]+)\)")
    assert (await client.get(thumbnail.search(shown).group(1))).status == 200
    assert (await client.get(thumbnail.search(before).group(1))).status == 404


@pytest.mark.parametrize("kind", [FINE, OFF])
async def test_the_picture_of_a_self_check_is_fetched_without_another_notice(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, hass_client_no_auth,
                                                                              kind: str):
    """The change list fetches no picture. The self-check of an installed file names its devices in the notice it ends
    with: its picture is fetched while it watches, so the notice's first page shows it although no other notice named
    the file."""
    picture = png(256, 256)
    aioclient_mock.get(pictures.PICTURE_URL.format(name="A.png"), content=picture)
    pin = hashlib.sha256(picture).hexdigest()
    await notice(hass, zha, repo_dir, aioclient_mock, kind, manifest=keyed(tuya_a={"picture": "A.png", "picture_sha256": pin}))
    fetched = [url for _method, url, _data, _headers in aioclient_mock.mock_calls if str(url).startswith("https://www.zigbee2mqtt.io/")]
    assert len(fetched) == 1 and stored(hass, picture).exists()
    flow = await start_fix(hass, issue_of(hass, kind))
    shown = flow["description_placeholders"]["picture"]
    assert pin in shown
    client = await hass_client_no_auth()
    assert (await client.get(re.search(r"!\[\]\((/api/xquirks/picture/[^)]+)\)", shown).group(1))).status == 200


async def test_do_not_check_closes_the_notice_for_this_revision(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    await notice(hass, zha, repo_dir, aioclient_mock)
    flow = await start_fix(hass, issue_of(hass, FINE))
    flow = await continue_fix(hass, flow, {"next_step_id": "skip"})
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "skipped"
    assert gone(hass)
    assert self_check(hass)["result"] == "fine" and not self_check(hass)["device_check_done"]
    assert manager_of(hass).checks.reports == []
    assert not await comes_back(hass, repo_dir)


@pytest.mark.parametrize("kind", [FINE, UNPROVEN])
async def test_a_notice_past_the_threshold_now_goes_on_its_first_page(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, kind: str):
    """Others confirmed the variant since the notice came: the first page's search finds it past the threshold."""
    _devices, search = await notice(hass, zha, repo_dir, aioclient_mock, kind)
    search.reply = answer(hit(5, title(FIRMWARE_A), thumbs=2))
    flow = await start_fix(hass, issue_of(hass, kind))
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "confirmed"
    assert gone(hass, kind) and search.calls == 2
    assert not await comes_back(hass, repo_dir, kind)


async def test_a_notice_whose_file_is_verified_now_goes_on_its_first_page(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """Verified needs no search."""
    with patch.object(checks.Checks, "file_verified", return_value=False):
        _devices, search = await notice(hass, zha, repo_dir, aioclient_mock, manifest=keyed(tuya_a={"status": "verified"}))
    before = search.calls
    flow = await start_fix(hass, issue_of(hass, FINE))
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "confirmed"
    assert gone(hass) and search.calls == before


async def test_looks_off_stays_past_the_threshold_and_verified(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """"Looks off" always comes: its first page never closes it on a confirmation."""
    with patch.object(checks.Checks, "file_verified", return_value=False):
        _devices, search = await notice(hass, zha, repo_dir, aioclient_mock, OFF)
    search.reply = answer(hit(5, title(FIRMWARE_A), thumbs=50))
    flow = await start_fix(hass, issue_of(hass, OFF))
    assert flow["type"] is FlowResultType.MENU and flow["step_id"] == FIRST
    with patch.object(checks.Checks, "file_verified", return_value=True):
        flow = await start_fix(hass, issue_of(hass, OFF))
    assert flow["type"] is FlowResultType.MENU and flow["step_id"] == FIRST


async def test_only_some_devices_past_the_threshold_keep_the_notice(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    (_kitchen, _hallway), search = await notice(hass, zha, repo_dir, aioclient_mock, second=True)
    search.reply = answer(hit(5, title(FIRMWARE_A), thumbs=9), hit(6, title(FIRMWARE_B), thumbs=0))
    flow = await start_fix(hass, issue_of(hass, FINE))
    assert flow["type"] is FlowResultType.MENU and flow["step_id"] == FIRST


@pytest.mark.parametrize("reply", [403, 429, 500, TimeoutError(), [], {"items": "x"}], ids=["403", "429", "500", "timeout", "list", "items"])
async def test_an_unknown_search_goes_on_as_normal_and_ends_with_the_form(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, reply):
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock)
    search.reply = reply
    answers(device)
    flow = await values_page(hass)
    flow = await continue_fix(hass, flow, {"next_step_id": "all_match"})
    assert not gone(hass)
    assert link_fields(await click(hass, flow, "report"))["template"] == text.VERIFY_FORM
    assert gone(hass)


@pytest.mark.parametrize("thumbs", [2, 0])
async def test_the_first_page_reads_the_next_search_page(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, thumbs: int):
    """Ten open issues of other variants fill the first search page, the variant's issue (#57) is on the second. Past the
    threshold: "already confirmed". Below it: the thumbs-up on #57 at the end."""
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock)
    asked = []

    def reply(url) -> dict:
        page = int(URL(url).query.get("page", "1"))
        asked.append(page)
        if page == 1:
            return answer(*(hit(n, title(FIRMWARE_A, manufacturer=f"_TZE200_{n:04d}")) for n in range(1, 11)), total_count=11)
        return answer(hit(57, title(FIRMWARE_A), thumbs=thumbs), total_count=11)

    search.reply = Gate(reply, held=0)
    answers(device)
    if thumbs:
        flow = await start_fix(hass, issue_of(hass, FINE))
        assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "confirmed" and asked == [1, 2]
        return
    flow = await values_page(hass)
    assert asked == [1, 2]
    flow = await continue_fix(hass, flow, {"next_step_id": "all_match"})
    assert await click(hass, flow, "thumbs_up") == ISSUE_LINK.format(number=57)


async def test_the_wait_of_the_first_page_covers_every_search_page(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """The first search page answers at once, the second is slow: the wait is over, the answer is unknown (the form at
    the end), no further page is asked."""
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock)
    event = asyncio.Event()
    asked = []

    async def reply(method, url, data):
        page = int(URL(url).query.get("page", "1"))
        asked.append(page)
        if page == 2:
            await event.wait()
            return AiohttpClientMockResponse(method, URL(url), json=answer(hit(57, title(FIRMWARE_A), thumbs=9), total_count=11))
        return AiohttpClientMockResponse(method, URL(url), json=answer(*(hit(n, title(FIRMWARE_A, manufacturer=f"_TZE200_{n:04d}")) for n in range(1, 11)),
                                                                         total_count=11))

    search.reply = reply
    answers(device)
    with patch.object(repairs, "SEARCH_WAIT", 0.2):
        flow = await values_page(hass)
    assert asked == [1, 2]
    flow = await continue_fix(hass, flow, {"next_step_id": "all_match"})
    assert last_page(flow) == "report"
    event.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert asked == [1, 2]


async def test_a_slow_search_is_an_unknown_one(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """The first page waits a few seconds at most, also for a search a self-check runs."""
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock)
    gate = Gate(answer(hit(5, title(FIRMWARE_A), thumbs=1)))
    search.reply = gate
    answers(device)
    assert repairs.SEARCH_WAIT <= 5
    with patch.object(repairs, "SEARCH_WAIT", 0.05):
        flow = await values_page(hass)
    assert gate.calls == 1
    flow = await continue_fix(hass, flow, {"next_step_id": "all_match"})
    # The issue the slow answer held is not known: the form.
    assert last_page(flow) == "report"
    await gate.open(hass)


@pytest.mark.parametrize("event", ["skipped_elsewhere", "file_updated"])
async def test_a_notice_that_changes_while_the_first_page_searches(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, event: str):
    """Decided on what stands after the search: closed in another window meanwhile, or its file updated."""
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock)
    issue = issue_of(hass, FINE)
    other = await start_fix(hass, issue)
    gate = Gate(answer(hit(5, title(FIRMWARE_A), thumbs=50)))
    search.reply = gate
    # The update waits for every task, the first page too: it stops waiting for the search sooner then.
    with patch.object(repairs, "SEARCH_WAIT", 0.5 if event == "file_updated" else 5.0):
        opening = hass.async_create_task(start_fix(hass, issue))
        for _ in range(100):
            if gate.calls:
                break
            await asyncio.sleep(0.01)
        assert gate.calls == 1 and not opening.done()
        if event == "skipped_elsewhere":
            other = await continue_fix(hass, other, {"next_step_id": "skip"})
            assert other["reason"] == "skipped"
        else:
            search.reply = answer()
            await _update_file(hass, zha, repo_dir, device)
        await gate.open(hass)
        flow = await opening
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == ("gone" if event == "skipped_elsewhere" else "file_changed")


async def test_the_first_page_waits_for_a_running_self_check_search_only_briefly(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    await notice(hass, zha, repo_dir, aioclient_mock)
    checks_ = manager_of(hass).checks
    await checks_._search_lock.acquire()
    try:
        with patch.object(repairs, "SEARCH_WAIT", 0.05):
            flow = await start_fix(hass, issue_of(hass, FINE))
        assert flow["type"] is FlowResultType.MENU and flow["step_id"] == FIRST
    finally:
        checks_._search_lock.release()


@pytest.mark.parametrize(("key", "threshold"), [(None, 3), (KEY_A, ...)], ids=["no_key", "no_threshold"])
async def test_without_a_key_or_threshold_nothing_is_searched(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, key, threshold):
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock, manifest=keyed(tuya_a={"key": key}), threshold=threshold)
    before = search.calls
    answers(device)
    flow = await values_page(hass)
    flow = await continue_fix(hass, flow, {"next_step_id": "all_match"})
    assert search.calls == before
    fields = link_fields(await click(hass, flow, "report"))
    # Without a key the title is the old one, nobody searches for it.
    assert fields["title"] == ("Verify: Tuya A" if key is None else title(FIRMWARE_A))


# The device check


async def test_one_device_needs_no_picker_and_ends_with_the_form(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """No open issue for the variant: the filled-in form, its title with the fixed part the next search compares."""
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock)
    search.reply = answer(hit(7, title(FIRMWARE_B), thumbs=0), hit(8, title(FIRMWARE_A, key="deadbeef"), thumbs=0))
    answers(device)
    flow = await values_page(hass)
    placeholders = flow["description_placeholders"]
    assert placeholders["device"] == NAME and placeholders["file"] == "z2m_tuya_a.py"
    assert "| sensor: Temperature | 1-temperature |  | 21.5 | °C | now |  |" in placeholders["entities"].replace("\\_", "_").replace("\\-", "-")
    flow = await continue_fix(hass, flow, {"next_step_id": "all_match"})
    # Every value of the file marked as matching.
    assert flow["description_placeholders"] == {"matches": "3", "unchecked": "0"}
    fields = link_fields(await click(hass, flow, "report"))
    assert fields["template"] == text.VERIFY_FORM and set(fields) - {"template", "title"} <= form_ids(text.VERIFY_FORM)
    assert fields["title"] == title(FIRMWARE_A) == f"Verify: Tuya A [_TZE200_aaaa | TS0601 | {FIRMWARE_A} | {KEY_A}]"
    assert confirm.matches(fields["title"], manager_of(hass).checks.fixed_part(device.device_id, KEY_A))
    assert "| yes | sensor: Temperature |" in fields["report"]
    assert gone(hass)
    result = await diagnostics.async_get_config_entry_diagnostics(hass, hass.config_entries.async_entries(const.DOMAIN)[0])
    assert result["device_checks"][-1]["report"] == fields["report"]
    assert result["self_check"]["z2m_tuya_a.py"]["device_check_done"]
    assert not await comes_back(hass, repo_dir)


async def test_all_values_match_with_an_open_issue_asks_for_a_thumbs_up(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """The oldest open issue of this device's variant: its link (built from its number) with its count and the threshold."""
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock)
    search.reply = answer(hit(42, title(FIRMWARE_A), thumbs=1), hit(50, title(FIRMWARE_A), thumbs=9), hit(7, title(FIRMWARE_B), thumbs=5))
    answers(device)
    flow = await values_page(hass)
    flow = await continue_fix(hass, flow, {"next_step_id": "all_match"})
    assert flow["description_placeholders"] == {"count": "2", "threshold": "3"}
    # Finished like a report: the report is in the diagnostics. The notice goes with the button.
    assert not gone(hass) and len(manager_of(hass).checks.reports) == 1
    assert await click(hass, flow, "thumbs_up") == ISSUE_LINK.format(number=42)
    assert gone(hass)
    assert not await comes_back(hass, repo_dir)


@pytest.mark.parametrize("open_issue", [True, False], ids=["thumbs_up", "form"])
async def test_the_state_key_of_the_revision_names_the_search_and_the_form(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, open_issue: bool):
    """The state in use states another key for the notice's revision (the self-check kept KEY_A): the first page searches
    with the state's key, the thumbs-up goes to the open issue of that key, the form's title carries it."""
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock)
    await same_revision(hass, repo_dir, NEW_KEY, threshold=3)
    older = hit(41, title(FIRMWARE_A), thumbs=1)
    search.reply = answer(older, hit(42, title(FIRMWARE_A, key=NEW_KEY), thumbs=1)) if open_issue else answer(older)
    before = len(searches(aioclient_mock))
    answers(device)
    flow = await values_page(hass)
    (url,) = searches(aioclient_mock)[before:]
    assert url.query["q"].endswith(f" {NEW_KEY}")
    flow = await continue_fix(hass, flow, {"next_step_id": "all_match"})
    if open_issue:
        assert await click(hass, flow, "thumbs_up") == ISSUE_LINK.format(number=42)
    else:
        assert link_fields(await click(hass, flow, "report"))["title"] == title(FIRMWARE_A, key=NEW_KEY)


async def test_several_devices_offer_only_the_notices_devices(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    (kitchen, hallway), search = await notice(hass, zha, repo_dir, aioclient_mock, second=True)
    acme(hass, zha)
    search.reply = answer(hit(9, title(FIRMWARE_B), thumbs=0))
    answers(hallway)
    flow = await start_fix(hass, issue_of(hass, FINE))
    flow = await check_now(hass, flow)
    assert flow["type"] is FlowResultType.FORM and flow["step_id"] == "check"
    options = flow["data_schema"].schema["device"].config["options"]
    assert sorted(option["value"] for option in options) == sorted([kitchen.device_id, hallway.device_id])
    # A device the notice does not name cannot be picked.
    with pytest.raises(InvalidData):
        await continue_fix(hass, flow, {"device": "not-a-device"})
    flow = await ask(hass, flow, {"device": hallway.device_id})
    assert flow["type"] is FlowResultType.MENU and flow["step_id"] == "values"
    assert flow["description_placeholders"]["device"] == HALL
    flow = await continue_fix(hass, flow, {"next_step_id": "all_match"})
    assert flow["description_placeholders"] == {"count": "1", "threshold": "3"}
    assert await click(hass, flow, "thumbs_up") == ISSUE_LINK.format(number=9)


async def test_a_device_past_the_threshold_at_the_end_of_the_day_is_not_offered(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """The notice names only the device below the threshold: no picker for one device."""
    prepare(repo_dir)
    kitchen, hallway = radar(hass, zha), hall(hass, zha)
    set_firmware(hass, kitchen.device_id, FIRMWARE_A)
    set_firmware(hass, hallway.device_id, FIRMWARE_B)
    Search(aioclient_mock, answer(hit(3, title(FIRMWARE_A), thumbs=1), hit(4, title(FIRMWARE_B), thumbs=0)))
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A, IEEE_B: TUYA_A})
    fine_values(kitchen)
    fine_values(hallway)
    await conclude(hass)
    assert issue_of(hass, FINE).data["devices"] == [hallway.device_id]
    answers(hallway)
    flow = await values_page(hass)
    assert flow["description_placeholders"]["device"] == HALL


async def test_a_device_that_does_not_answer_says_so_on_the_values_page(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    (device,), _search = await notice(hass, zha, repo_dir, aioclient_mock)
    flow = await start_fix(hass, issue_of(hass, FINE))
    flow = await ask(hass, flow, {"next_step_id": "check"})
    assert flow["type"] is FlowResultType.MENU and flow["step_id"] == "values_no_answer" and flow["menu_options"] == ["all_match", "mismatch"]
    # zigpy's node descriptor says mains.
    device.zigpy.node_desc = SimpleNamespace(is_mains_powered=True)
    flow = await start_fix(hass, issue_of(hass, FINE))
    flow = await ask(hass, flow, {"next_step_id": "check"})
    assert flow["step_id"] == "values_no_answer_mains"
    flow = await continue_fix(hass, flow, {"next_step_id": "mismatch"})
    # Today's list, without the note a second time.
    assert flow["type"] is FlowResultType.FORM and flow["step_id"] == "results" and not flow["errors"]


@pytest.mark.parametrize("kind", [FINE, OFF])
async def test_something_does_not_match_leads_to_the_list_and_the_problem_report(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, kind: str):
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock, kind)
    search.reply = answer(hit(42, title(FIRMWARE_A), thumbs=1))
    answers(device)
    flow = await values_page(hass, kind)
    flow = await continue_fix(hass, flow, {"next_step_id": "mismatch"})
    assert flow["type"] is FlowResultType.FORM and flow["step_id"] == "results"
    assert set(flow["data_schema"].schema) == {"matches", "wrong"}
    # The list checks its input as in the options.
    flow = await continue_fix(hass, flow, {"matches": [], "wrong": []})
    assert flow["type"] is FlowResultType.FORM and flow["errors"] == {"base": "nothing"}
    flow = await continue_fix(hass, flow, {"matches": ["1-temperature"], "wrong": ["1-temperature"]})
    assert flow["errors"] == {"base": "both"}
    flow = await continue_fix(hass, flow, {"matches": ["1-humidity"], "wrong": ["1-temperature"]})
    assert flow["description_placeholders"] == {"wrong": "1", "matches": "1", "unchecked": "1"}
    fields = link_fields(await click(hass, flow, "report_wrong"))
    assert fields["template"] == text.PROBLEM_FORM
    assert fields["problem"] == "- sensor: Temperature: Home Assistant shows 21.5 °C, the device shows something else"
    assert gone(hass, kind)
    assert not await comes_back(hass, repo_dir, kind)


async def test_the_list_with_some_values_matching_never_ends_with_a_thumbs_up(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """Some marked matching, none wrong: the report with the values not checked, never a thumbs-up."""
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock)
    search.reply = answer(hit(42, title(FIRMWARE_A), thumbs=1))
    answers(device)
    flow = await values_page(hass)
    flow = await continue_fix(hass, flow, {"next_step_id": "mismatch"})
    assert len(flow["data_schema"].schema["matches"].config["options"]) == 3
    flow = await continue_fix(hass, flow, {"matches": ["1-temperature"], "wrong": []})
    assert flow["description_placeholders"] == {"matches": "1", "unchecked": "2"}
    assert link_fields(await click(hass, flow, "report"))["template"] == text.VERIFY_FORM
    assert "| yes | sensor: Temperature |" in manager_of(hass).checks.reports[-1]["report"]
    assert "| not checked | sensor: Humidity |" in manager_of(hass).checks.reports[-1]["report"]
    assert gone(hass)


async def test_every_value_marked_matching_in_the_list_is_all_values_match(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """The list with every value matching and none wrong goes on as "All values match" does: here to the thumbs-up."""
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock)
    search.reply = answer(hit(42, title(FIRMWARE_A), thumbs=1))
    answers(device)
    flow = await values_page(hass)
    flow = await continue_fix(hass, flow, {"next_step_id": "mismatch"})
    rows = [option["value"] for option in flow["data_schema"].schema["matches"].config["options"]]
    flow = await continue_fix(hass, flow, {"matches": rows, "wrong": []})
    assert flow["type"] is FlowResultType.FORM and flow["step_id"] == "thumbs_up", flow
    assert flow["description_placeholders"] == {"count": "2", "threshold": "3"}
    assert await click(hass, flow, "thumbs_up") == ISSUE_LINK.format(number=42)


@pytest.mark.parametrize("why", ["incomplete", "malformed"])
async def test_an_answer_that_may_lack_the_oldest_issue_gives_no_thumbs_up(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, why: str):
    """GitHub marks the answer incomplete, or an item before the match is malformed: the match may be a newer duplicate
    of the variant (only the oldest counts). The form, no thumbs-up on it, and the notice is not closed as confirmed."""
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock)
    duplicate = hit(31, title(FIRMWARE_A), thumbs=9)
    search.reply = answer(duplicate, incomplete_results=True) if why == "incomplete" else answer(hit(20, title(FIRMWARE_A), thumbs="9"), duplicate)
    answers(device)
    flow = await values_page(hass)
    flow = await continue_fix(hass, flow, {"next_step_id": "all_match"})
    assert last_page(flow) == "report"


async def test_looks_off_can_end_with_all_values_match(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    (device,), _search = await notice(hass, zha, repo_dir, aioclient_mock, OFF)
    answers(device)
    flow = await values_page(hass, OFF)
    flow = await continue_fix(hass, flow, {"next_step_id": "all_match"})
    assert last_page(flow) == "report"
    assert not gone(hass, OFF)
    await click(hass, flow, "report")
    assert gone(hass, OFF)


# The last page


async def last(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, step: str, kind: str = FINE) -> dict:
    """The flow of a notice of this kind up to this last page."""
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock, kind)
    if step == "thumbs_up":
        search.reply = answer(hit(42, title(FIRMWARE_A), thumbs=1))
    answers(device)
    return await to_last(hass, step, kind)


async def to_last(hass: HomeAssistant, step: str, kind: str = FINE) -> dict:
    """A new flow of the notice through the device check up to this last page."""
    flow = await values_page(hass, kind)
    if step == "report_wrong":
        flow = await continue_fix(hass, flow, {"next_step_id": "mismatch"})
        flow = await continue_fix(hass, flow, {"matches": ["1-humidity"], "wrong": ["1-temperature"]})
    else:
        flow = await continue_fix(hass, flow, {"next_step_id": "all_match"})
    assert last_page(flow) == step, flow
    return flow


def still_open(hass: HomeAssistant, kind: str = FINE) -> bool:
    """The notice is there as the user can open it: fixable, not ignored, its self-check not closed."""
    issue = issue_of(hass, kind)
    return issue.is_fixable and issue.dismissed_version is None and not manager_of(hass).checks.observations["z2m_tuya_a.py"].get("closed")


def x(hass: HomeAssistant, flow: dict) -> None:
    """The dialog's X. The frontend deletes a flow it started itself that did not end: the first page's fix flow, a
    device check from the button. A device check the notice handed over stays (the frontend deletes no flow it only
    continued): the notice's next handover ends it."""
    if not isinstance(flow["handler"], tuple):
        hass.data["repairs"]["flow_manager"].async_abort(flow["flow_id"])
    elif hass.config_entries.subentries.async_get(flow["flow_id"])["context"]["source"] != "notice":
        hass.config_entries.subentries.async_abort(flow["flow_id"])


@pytest.mark.parametrize("kind", [FINE, UNPROVEN, OFF])
@pytest.mark.parametrize("step", LAST_PAGES)
async def test_the_last_page_keeps_the_report_and_leaves_the_notice_open(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, step: str, kind: str):
    """The check is over when the last page shows: the report is kept for the diagnostics, the check counts as done. The
    notice stays until the page's button is clicked."""
    flow = await last(hass, zha, repo_dir, aioclient_mock, step, kind)
    checks_ = manager_of(hass).checks
    assert still_open(hass, kind) and len(checks_.reports) == 1 and self_check(hass)["device_check_done"]
    # Asked for the page again (a GET of the flow): the same page, nothing opened, nothing closed.
    again = await continue_fix(hass, flow, None)
    assert last_page(again) == step and again["description_placeholders"] == flow["description_placeholders"]
    assert still_open(hass, kind) and len(checks_.reports) == 1


@pytest.mark.parametrize("step", LAST_PAGES)
async def test_the_button_closes_the_notice_and_opens_its_link(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, step: str):
    """A page without fields shows one button with its own text (an abort page always shows Close). It closes the notice
    for this revision and opens the link (the form or the device test of the variant) as an external step: Home
    Assistant opens it in a new tab and offers it as a button too. The dialog stays on that step. Its X changes nothing
    more (the frontend deletes no check a notice handed over)."""
    flow = await last(hass, zha, repo_dir, aioclient_mock, step)
    url = await click(hass, flow, step)
    if step == "thumbs_up":
        assert url == ISSUE_LINK.format(number=42)
    else:
        assert url.startswith(f"https://github.com/{const.REPOSITORY}/issues/new?")
        assert link_fields(url)["template"] == (text.PROBLEM_FORM if step == "report_wrong" else text.VERIFY_FORM)
    checks_ = manager_of(hass).checks
    assert gone(hass) and checks_.observations["z2m_tuya_a.py"]["closed"] is True and len(checks_.reports) == 1
    before = checks_.summary()
    # Clicked again (a double submit), asked for again, or sent anything: the same external step, nothing more.
    for user_input in ({}, None, {"anything": 1}):
        opened = await continue_fix(hass, flow, user_input)
        assert opened["type"] is FlowResultType.EXTERNAL_STEP and opened["step_id"] == opened_step(step) and opened["url"] == url
    x(hass, flow)
    assert (await continue_fix(hass, flow, None))["url"] == url
    assert checks_.summary() == before and len(checks_.reports) == 1 and self_check(hass)["device_check_done"]
    assert not await comes_back(hass, repo_dir)


@pytest.mark.parametrize("step", LAST_PAGES)
async def test_closing_the_dialog_on_the_last_page_leaves_the_notice_open(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, step: str):
    """The dialog's X on the last page: the notice stays (through a refresh and a restart), so does the report. Opened
    again, the notice starts on its first page and the device is asked again: that handover ends the check before, which
    the frontend left. Its button then closes the notice."""
    flow = await last(hass, zha, repo_dir, aioclient_mock, step)
    left = flow
    x(hass, flow)
    assert still_open(hass) and len(manager_of(hass).checks.reports) == 1 and self_check(hass)["device_check_done"]
    assert await comes_back(hass, repo_dir)
    assert still_open(hass) and len(manager_of(hass).checks.reports) == 1
    first = await start_fix(hass, issue_of(hass, FINE))
    assert first["type"] is FlowResultType.MENU and first["step_id"] == FIRST and first["menu_options"] == ["check", "skip"]
    x(hass, first)
    flow = await to_last(hass, step)
    with pytest.raises(UnknownFlow):
        await continue_fix(hass, left, {})
    assert [found["flow_id"] for found in device_checks(hass)] == [flow["flow_id"]]
    assert still_open(hass) and len(manager_of(hass).checks.reports) == 2
    await click(hass, flow, step)
    assert gone(hass) and not await comes_back(hass, repo_dir)


# What the step before sends, sent again: its menu choice, or the list after "Something does not match".
AGAIN = {"all_match": {"next_step_id": "all_match"}, "results": {"matches": ["1-humidity"], "wrong": ["1-temperature"]}}


@pytest.mark.parametrize(("step", "again"), [("report", "all_match"), ("thumbs_up", "all_match"), ("report_wrong", "results")])
async def test_the_step_before_sent_again_is_no_click(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, step: str, again: str):
    """A doubled or retried POST of the step before reaches the last page (a page without fields takes any input). It
    shows the page again: only the button's empty input is the click."""
    flow = await last(hass, zha, repo_dir, aioclient_mock, step)
    for _ in range(2):
        shown = await continue_fix(hass, flow, AGAIN[again])
        assert last_page(shown) == step and shown["description_placeholders"] == flow["description_placeholders"], shown
    assert still_open(hass) and len(manager_of(hass).checks.reports) == 1
    await click(hass, shown, step)
    assert gone(hass)


async def test_a_double_submit_closes_once_and_opens_one_link(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """Two clicks on the button at once: the first closes the notice and answers the external step, the second finds the
    flow on that step and gets the same, nothing is closed twice."""
    flow = await last(hass, zha, repo_dir, aioclient_mock, "report")
    with patch.object(checks.Checks, "close_notice", autospec=True, side_effect=checks.Checks.close_notice) as closing:
        first, second = await asyncio.gather(continue_fix(hass, flow, {}), continue_fix(hass, flow, {}))
    assert first["type"] is second["type"] is FlowResultType.EXTERNAL_STEP and first["url"] == second["url"]
    assert closing.call_count == 1 and gone(hass)


async def _skipped_elsewhere(hass: HomeAssistant, zha: Zha, repo_dir: Path, device: LiveDevice, step: str) -> None:
    other = await continue_fix(hass, await start_fix(hass, issue_of(hass, FINE)), {"next_step_id": "skip"})
    assert other["reason"] == "skipped"


async def _checked_with_the_button(hass: HomeAssistant, zha: Zha, repo_dir: Path, device: LiveDevice, step: str) -> None:
    result = await run_check(hass, hass.config_entries.async_entries(const.DOMAIN)[0], device.device_id)
    result = await continue_check(hass, result, {"matches": ["1-temperature"], "wrong": []})
    await report_link(hass, result)


async def _verified_now(hass: HomeAssistant, zha: Zha, repo_dir: Path, device: LiveDevice, step: str) -> None:
    prepare(repo_dir, keyed(tuya_a={"status": "verified"}), threshold=3)
    REPOS[repo_dir].publish()
    await manager_of(hass).async_check()
    await hass.async_block_till_done(wait_background_tasks=True)


async def _deleted(hass: HomeAssistant, zha: Zha, repo_dir: Path, device: LiveDevice, step: str) -> None:
    await _notice_deleted(hass, zha, repo_dir, device)


async def _updated(hass: HomeAssistant, zha: Zha, repo_dir: Path, device: LiveDevice, step: str) -> None:
    await _update_file(hass, zha, repo_dir, device)


async def _removed(hass: HomeAssistant, zha: Zha, repo_dir: Path, device: LiveDevice, step: str) -> None:
    await _remove_file(hass, zha, repo_dir, device)


# What closes or changes the notice between its last page and the click, the reason the click then ends with, and the
# reports kept by then (another check kept one more).
CLICK_STALE = {"skipped_elsewhere": (_skipped_elsewhere, "gone", 1),
               "checked_with_the_button": (_checked_with_the_button, "gone", 2), "verified_now": (_verified_now, "gone", 1),
               "notice_deleted": (_deleted, "gone", 1), "file_updated": (_updated, "file_changed", 1), "file_removed": (_removed, "file_changed", 1)}


@pytest.mark.parametrize("step", ["report", "thumbs_up"])
@pytest.mark.parametrize("event", list(CLICK_STALE))
async def test_a_click_after_the_notice_closed_or_changed_opens_nothing(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, event: str, step: str):
    """The notice was closed in another window ("Do not check", the button's check), its file is verified now,
    updated or removed: the click ends with the reason, opens no second form, closes nothing."""
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock)
    if step == "thumbs_up":
        search.reply = answer(hit(42, title(FIRMWARE_A), thumbs=1))
    answers(device)
    flow = await to_last(hass, step)
    change, reason, reports = CLICK_STALE[event]
    await change(hass, zha, repo_dir, device, step)
    before = manager_of(hass).checks.summary()
    result = await continue_fix(hass, flow, {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == reason
    assert manager_of(hass).checks.summary() == before and len(manager_of(hass).checks.reports) == reports
    assert issues_of(hass, FINE) == []


async def test_a_click_after_the_device_left_still_closes_and_opens(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """The link needs no device: the notice stands, the button closes it."""
    (device,), _search = await notice(hass, zha, repo_dir, aioclient_mock)
    answers(device)
    flow = await to_last(hass, "report")
    await _device_leaves(hass, zha, repo_dir, device)
    assert still_open(hass)
    await click(hass, flow, "report")
    assert gone(hass)


async def test_a_click_while_the_entry_is_unloaded_leaves_the_notice(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    flow = await last(hass, zha, repo_dir, aioclient_mock, "report")
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    await hass.config_entries.async_unload(entry.entry_id)
    result = await continue_fix(hass, flow, {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "not_loaded"
    await restart_with(hass, entry, repo_dir)
    assert still_open(hass) and len(manager_of(hass).checks.reports) == 1


@pytest.mark.parametrize("how", ["restart", "reload"])
async def test_a_restart_between_the_last_page_and_the_click(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, how: str):
    """A restart of Home Assistant ends every flow: the button of a last page still open in the browser reaches no flow
    (Home Assistant answers 404). The notice stays, the report and the check done stay stored, the next opening starts on
    the first page. A reload of the entry alone keeps the flow: its button closes the notice of the reloaded entry."""
    flow = await last(hass, zha, repo_dir, aioclient_mock, "report")
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    if how == "restart":
        hass.config_entries.subentries.async_abort(flow["flow_id"])
    await restart_with(hass, entry, repo_dir)
    assert still_open(hass) and len(manager_of(hass).checks.reports) == 1 and self_check(hass)["device_check_done"]
    if how == "reload":
        await click(hass, flow, "report")
        assert gone(hass)
        return
    with pytest.raises(UnknownFlow):
        await continue_fix(hass, flow, {})
    first = await start_fix(hass, issue_of(hass, FINE))
    assert first["type"] is FlowResultType.MENU and first["step_id"] == FIRST


# What a search answer may carry

HOSTILE = [
    "evil.example", "tracker.example", "click me", "evil-login", "<script", "javascript:", "@maintainer", "Verify:", "**",
]


async def test_nothing_of_the_answer_but_number_and_count_reaches_a_dialog(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """A matching issue whose title carries markdown, links and an image: only its number (in a link built here) and
    its count reach the dialog. Body, user and URLs of the answer neither."""
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock)
    fixed = confirm.fixed_part("_TZE200_aaaa", "TS0601", FIRMWARE_A, KEY_A)
    evil = f"Verify: [click me](https://evil.example/x) ![p](https://tracker.example/p.png) **@maintainer** <script>javascript:x</script> {fixed}"
    assert confirm.matches(evil, fixed)
    search.reply = answer(hit(42, evil, thumbs=1, html_url="https://evil.example/issues/42", user={"login": "evil-login"},
                              body="[click me](https://evil.example) <script>"))
    answers(device)
    shown = []
    flow = await start_fix(hass, issue_of(hass, FINE))
    shown.append(flow)
    flow = await ask(hass, flow, {"next_step_id": "check"})
    shown.append(flow)
    flow = await continue_fix(hass, flow, {"next_step_id": "all_match"})
    shown.append(flow)
    assert await click(hass, flow, "thumbs_up") == ISSUE_LINK.format(number=42)
    for result in shown:
        for value in (result.get("description_placeholders") or {}).values():
            assert not [part for part in HOSTILE if part in value], (result["step_id"] if "step_id" in result else result["reason"], value)


@pytest.mark.parametrize("number", ["42", 0, -1, 2**31, 4.2, True, None], ids=["text", "zero", "negative", "too_big", "float", "bool", "none"])
async def test_an_issue_number_of_another_shape_gives_the_form(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, number):
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock)
    search.reply = answer(hit(1, title(FIRMWARE_A), thumbs=1) | {"number": number})
    answers(device)
    flow = await values_page(hass)
    flow = await continue_fix(hass, flow, {"next_step_id": "all_match"})
    assert last_page(flow) == "report"


# Flows that went stale


async def _update_file(hass: HomeAssistant, zha: Zha, repo_dir: Path, device: LiveDevice) -> None:
    prepare(repo_dir, version=2)
    REPOS[repo_dir].publish()
    await manager_of(hass).async_check()
    await apply_now(hass)
    zha.reload({IEEE_A: TUYA_A})
    await settle_now(hass)


async def _remove_file(hass: HomeAssistant, zha: Zha, repo_dir: Path, device: LiveDevice) -> None:
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    hass.config_entries.async_update_entry(entry, options={const.CONF_EXCLUDED: [device.device_id]})
    await settle_now(hass)
    await apply_now(hass)
    zha.reload({})
    await settle_now(hass)


async def _device_leaves(hass: HomeAssistant, zha: Zha, repo_dir: Path, device: LiveDevice) -> None:
    zha.remove_device(device.device_id)
    await settle_now(hass)


async def _notice_deleted(hass: HomeAssistant, zha: Zha, repo_dir: Path, device: LiveDevice) -> None:
    ir.async_delete_issue(hass, const.DOMAIN, issue_of(hass, FINE).issue_id)


STALE = {"file_updated": (_update_file, "file_changed"), "file_removed": (_remove_file, "file_changed"), "device_left": (_device_leaves, "no_device"),
         "notice_deleted": (_notice_deleted, "gone")}


@pytest.mark.parametrize("event", list(STALE))
@pytest.mark.parametrize("moment", ["first_page", "values"])
async def test_a_stale_flow_ends_with_a_reason(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, event: str, moment: str):
    (device,), _search = await notice(hass, zha, repo_dir, aioclient_mock)
    answers(device)
    change, reason = STALE[event]
    if moment == "first_page":
        flow = await start_fix(hass, issue_of(hass, FINE))
        await change(hass, zha, repo_dir, device)
        flow = await check_now(hass, flow)
    else:
        flow = await values_page(hass)
        await change(hass, zha, repo_dir, device)
        flow = await continue_fix(hass, flow, {"next_step_id": "all_match"})
    expected = "device_gone" if (moment, event) == ("values", "device_left") else reason
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == expected
    assert manager_of(hass).checks.reports == []


@pytest.mark.parametrize("event", list(STALE))
async def test_do_not_check_on_a_stale_flow_ends_with_a_reason(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, event: str):
    """A notice whose devices all left can still be put away: nothing else takes it away while its file stays."""
    (device,), _search = await notice(hass, zha, repo_dir, aioclient_mock)
    change, reason = STALE[event]
    flow = await start_fix(hass, issue_of(hass, FINE))
    await change(hass, zha, repo_dir, device)
    flow = await continue_fix(hass, flow, {"next_step_id": "skip"})
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == ("skipped" if event == "device_left" else reason)
    assert gone(hass)


async def test_a_notice_whose_devices_all_left_still_opens(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock)
    search.reply = answer(hit(5, title(FIRMWARE_A), thumbs=50))
    await _device_leaves(hass, zha, repo_dir, device)
    flow = await start_fix(hass, issue_of(hass, FINE))
    # No device to call confirmed: the page as always.
    assert flow["type"] is FlowResultType.MENU and flow["step_id"] == FIRST
    flow = await check_now(hass, flow)
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "no_device"


async def test_a_file_updated_while_the_device_is_asked_is_not_reported(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    (device,), _search = await notice(hass, zha, repo_dir, aioclient_mock)
    tuya = device.clusters[0xEF00]
    flow = await start_fix(hass, issue_of(hass, FINE))
    with patch.object(checks, "CHECK_WAIT", 0.3), patch.object(checks, "CHECK_QUIET", 0.05), patch.object(checks, "CHECK_STEP", 0.05):
        tuya.answer = lambda: fine_values(device)
        flow = await check_now(hass, flow)
        assert flow["type"] is FlowResultType.SHOW_PROGRESS
        await _update_file(hass, zha, repo_dir, device)
        await hass.async_block_till_done(wait_background_tasks=True)
    flow = await continue_fix(hass, flow)
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "file_changed"


async def test_an_entry_unloaded_while_the_device_is_asked_ends_the_check(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, caplog):
    """The entry unloaded during the progress dialog: the query's end leads on to the values step (Home Assistant takes
    nothing else after progress), which ends with "not_loaded". The dialog does not keep its spinner."""
    (device,), _search = await notice(hass, zha, repo_dir, aioclient_mock)
    tuya = device.clusters[0xEF00]
    flow = await start_fix(hass, issue_of(hass, FINE))
    with patch.object(checks, "CHECK_WAIT", 0.3), patch.object(checks, "CHECK_QUIET", 0.05), patch.object(checks, "CHECK_STEP", 0.05):
        tuya.answer = lambda: fine_values(device)
        flow = await check_now(hass, flow)
        assert flow["type"] is FlowResultType.SHOW_PROGRESS
        await hass.config_entries.async_unload(hass.config_entries.async_entries(const.DOMAIN)[0].entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert "Show progress can only" not in caplog.text
    # The progress ended (its done step names the values step): the dialog's fetch runs it.
    assert hass.config_entries.subentries.async_get(flow["flow_id"])["step_id"] == "values"
    flow = await continue_fix(hass, flow)
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "not_loaded"


async def test_the_flow_of_an_unloaded_entry_ends(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    await notice(hass, zha, repo_dir, aioclient_mock)
    issue = issue_of(hass, FINE)
    await hass.config_entries.async_unload(hass.config_entries.async_entries(const.DOMAIN)[0].entry_id)
    assert ir.async_get(hass).async_get_issue(const.DOMAIN, issue.issue_id) is not None
    flow = await start_fix(hass, issue)
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "not_loaded"


# The steps of the device check that need the step before: each ends when reached without it.
CHECK_STEPS = ("ask", "values", "values_no_answer", "values_no_answer_mains", "mismatch", "results", "all_match", *LAST_PAGES, "open", "open_test", "done")


@pytest.mark.parametrize("step", [*CHECK_STEPS, "notice", "user"])
async def test_the_first_page_offers_no_step_of_the_device_check(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, step: str):
    """Home Assistant refuses a choice the menu does not offer. The fix flow has no step of the device check."""
    await notice(hass, zha, repo_dir, aioclient_mock)
    flow = await start_fix(hass, issue_of(hass, FINE))
    with pytest.raises(InvalidData):
        await continue_fix(hass, flow, {"next_step_id": step})
    assert not hasattr(repairs.SelfCheckFlow, f"async_step_{step}")
    assert still_open(hass) and device_checks(hass) == []


def handler_of(hass: HomeAssistant, flow: dict) -> object:
    return hass.config_entries.subentries._progress[flow["flow_id"]]


@pytest.mark.parametrize("source", ["notice", "user"])
@pytest.mark.parametrize("step", CHECK_STEPS)
async def test_a_step_out_of_order_changes_nothing(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, step: str, source: str):
    """Home Assistant runs only the step the flow is on. Reached anyway (on the device picker), a step without the step
    before ends with a reason of the device check and asks nothing, keeps no report and closes nothing. The device check
    stays on its picker."""
    (kitchen, hallway), _search = await notice(hass, zha, repo_dir, aioclient_mock, second=True)
    answers(kitchen)
    if source == "notice":
        flow = await check_now(hass, await start_fix(hass, issue_of(hass, FINE)))
    else:
        flow = await open_check(hass, hass.config_entries.async_entries(const.DOMAIN)[0])
    assert flow["type"] is FlowResultType.FORM and flow["step_id"] == "check", flow
    sent = len(kitchen.clusters[0xEF00].sent)
    # The view's signal too: before the button it ends nothing.
    for user_input in (None, {}, {"next_step_id": "all_match"}, {"matches": ["1-temperature"], "wrong": []}, links.OPENED):
        result = await getattr(handler_of(hass, flow), f"async_step_{step}")(user_input)
        assert result["type"] is FlowResultType.ABORT and result["reason"] in CHECK["abort"], result
    assert len(kitchen.clusters[0xEF00].sent) == sent and manager_of(hass).checks.reports == [] and still_open(hass)
    again = await continue_check(hass, flow)
    assert again["type"] is FlowResultType.FORM and again["step_id"] == "check"


# Two flows for one notice


async def test_a_notice_handed_over_again_ends_its_check_before(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """The frontend deletes no check a notice handed over when its dialog closes. The notice's next handover (after the
    X, or in a second window) ends it: one check per notice, no second form for it. Other notices keep theirs."""
    (device,), _search = await notice(hass, zha, repo_dir, aioclient_mock)
    answers(device)
    first = await continue_fix(hass, await values_page(hass), {"next_step_id": "all_match"})
    assert last_page(first) == "report" and len(manager_of(hass).checks.reports) == 1
    handed = hass.data[config_flow.HANDED]
    assert handed == {issue_of(hass, FINE).issue_id: first["flow_id"]}
    handed["another_notice"] = "another-flow"
    second = await values_page(hass)
    with pytest.raises(UnknownFlow):
        await continue_fix(hass, first, {})
    assert [found["flow_id"] for found in device_checks(hass)] == [second["flow_id"]]
    assert handed == {issue_of(hass, FINE).issue_id: second["flow_id"], "another_notice": "another-flow"}
    assert still_open(hass)
    second = await continue_fix(hass, second, {"next_step_id": "all_match"})
    await click(hass, second, "report")
    assert gone(hass) and len(manager_of(hass).checks.reports) == 2
    # The check ends (here by hand, in the browser by the tab): its entry goes.
    hass.config_entries.subentries.async_abort(second["flow_id"])
    assert handed == {"another_notice": "another-flow"}


async def test_do_not_check_in_one_flow_ends_the_other(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    await notice(hass, zha, repo_dir, aioclient_mock)
    issue = issue_of(hass, FINE)
    first, second = await start_fix(hass, issue), await start_fix(hass, issue)
    first = await continue_fix(hass, first, {"next_step_id": "skip"})
    assert first["reason"] == "skipped"
    second = await continue_fix(hass, second, {"next_step_id": "skip"})
    assert second["type"] is FlowResultType.ABORT and second["reason"] == "gone"
    assert gone(hass)


# The device check from the integration page's button


def entry_of(hass: HomeAssistant):
    return hass.config_entries.async_entries(const.DOMAIN)[0]


async def test_the_button_offers_every_device_and_never_ends_with_a_thumbs_up(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """The button asks which device also for one, of every device that runs a file of ours. Its form link carries the
    same title as the notice's check. Only a notice's first page searches: "All values match" gives the form, also when an
    open issue of the variant exists. The check never adds a subentry."""
    prepare(repo_dir)
    device = radar(hass, zha)
    set_firmware(hass, device.device_id, FIRMWARE_A)
    search = Search(aioclient_mock, answer(hit(42, title(FIRMWARE_A), thumbs=1)))
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    answers(device)
    entry = entry_of(hass)
    picker = await open_check(hass, entry)
    assert picker["type"] is FlowResultType.FORM and picker["step_id"] == "check"
    assert [option["value"] for option in picker["data_schema"].schema["device"].config["options"]] == [device.device_id]
    assert hass.config_entries.subentries.async_get(picker["flow_id"])["context"]["source"] == "user"
    x(hass, picker)
    before = search.calls
    result = await run_check(hass, entry, device.device_id, to_list=False)
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "values" and result["menu_options"] == ["all_match", "mismatch"]
    result = await continue_check(hass, result, {"next_step_id": "all_match"})
    assert last_page(result) == "report" and search.calls == before
    assert link_fields(await report_link(hass, result))["title"] == title(FIRMWARE_A)
    assert entry.subentries == {} and device_checks(hass) == [hass.config_entries.subentries.async_get(result["flow_id"])]


async def test_a_button_check_of_a_zcl_file_reaches_its_values(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    prepare(repo_dir)
    device = acme(hass, zha)
    Search(aioclient_mock)
    await installed(hass, zha, repo_dir, {IEEE_C: ACME_C})
    result = await run_check(hass, entry_of(hass), device.device_id, to_list=False)
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "values"


@pytest.mark.parametrize("step", ["report", "report_wrong"])
async def test_the_button_check_ends_with_the_same_last_page_and_its_button_closes_the_notice(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock,
                                                                                              step: str):
    """The button's check of a device whose file has a notice: its last page is the notice's (a page without fields, one
    button, the report kept when it shows). The dialog's X there leaves the notice. The button closes it and opens the
    form as an external step. Clicked again or asked for again: the same step."""
    (device,), _search = await notice(hass, zha, repo_dir, aioclient_mock)
    answers(device)
    entry = entry_of(hass)
    answered = {"matches": ["1-humidity"], "wrong": ["1-temperature"]} if step == "report_wrong" else {"matches": ["1-temperature"], "wrong": []}
    for clicked in (False, True):
        result = await run_check(hass, entry, device.device_id)
        result = await continue_check(hass, result, answered)
        assert result["type"] is FlowResultType.FORM and result["step_id"] == step and result["data_schema"] is None
        assert set(result["description_placeholders"]) == ({"wrong", "matches", "unchecked"} if step == "report_wrong" else {"matches", "unchecked"})
        assert still_open(hass) and self_check(hass)["device_check_done"]
        again = await continue_check(hass, result)
        assert again["type"] is FlowResultType.FORM and again["step_id"] == step and still_open(hass)
        if not clicked:
            x(hass, result)
            assert still_open(hass)
    url = await report_link(hass, result, step)
    assert link_fields(url)["template"] == (text.PROBLEM_FORM if step == "report_wrong" else text.VERIFY_FORM)
    assert gone(hass) and len(manager_of(hass).checks.reports) == 2
    for user_input in ({}, None, {"anything": 1}):
        opened = await continue_check(hass, result, user_input)
        assert opened["type"] is FlowResultType.EXTERNAL_STEP and opened["step_id"] == "open" and opened["url"] == url
    assert not await comes_back(hass, repo_dir)


@pytest.mark.parametrize("step", ["report", "report_wrong"])
async def test_the_button_check_list_sent_again_is_no_click(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, step: str):
    """The list sent again (a doubled or retried POST) reaches the last page: the page again, the notice stays."""
    (device,), _search = await notice(hass, zha, repo_dir, aioclient_mock)
    answers(device)
    answered = AGAIN["results"] if step == "report_wrong" else {"matches": ["1-temperature"], "wrong": []}
    result = await run_check(hass, entry_of(hass), device.device_id)
    result = await continue_check(hass, result, answered)
    for _ in range(2):
        shown = await continue_check(hass, result, answered)
        assert last_page(shown) == step, shown
    assert still_open(hass) and len(manager_of(hass).checks.reports) == 1
    await report_link(hass, shown, step)
    assert gone(hass)


async def test_the_button_check_opens_the_form_when_the_notice_went_meanwhile(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """The button's check belongs to no notice: its button opens the form also after the notice closed elsewhere, and
    closes nothing more. While the entry is unloaded it ends with "not loaded"."""
    (device,), _search = await notice(hass, zha, repo_dir, aioclient_mock)
    answers(device)
    entry = entry_of(hass)
    result = await run_check(hass, entry, device.device_id)
    result = await continue_check(hass, result, {"matches": ["1-temperature"], "wrong": []})
    await _skipped_elsewhere(hass, zha, repo_dir, device, "report")
    assert link_fields(await report_link(hass, result))["template"] == text.VERIFY_FORM
    result = await run_check(hass, entry, device.device_id)
    result = await continue_check(hass, result, {"matches": ["1-temperature"], "wrong": []})
    await hass.config_entries.async_unload(entry.entry_id)
    result = await continue_check(hass, result, {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "not_loaded"


async def test_the_button_with_nothing_to_check_ends(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """No device runs a file of ours: "nothing to check" (the notice's own reason names its devices). An unloaded entry or
    ZHA not loaded end as in a notice's check."""
    prepare(repo_dir)
    Search(aioclient_mock)
    entry = await setup_entry(hass, repo_dir)
    assert (await open_check(hass, entry))["reason"] == "nothing_to_check"
    zha.start_reload()
    assert (await open_check(hass, entry))["reason"] == "zha_not_loaded"
    await hass.config_entries.async_unload(entry.entry_id)
    assert (await open_check(hass, entry))["reason"] == "not_loaded"
    assert device_checks(hass) == []


# The handover from the notice


async def test_the_frontend_gets_the_device_check_as_the_next_flow(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, hass_client):
    """Through Home Assistant's HTTP API as the frontend calls it: "Check the device now" answers an abort with next_flow
    (config_subentries_flow and the flow id) and the entry. The frontend then opens the sub-config dialog, which fetches
    the flow (a GET) and takes its type from the handler. The external step at the end names its step: the dialog shows
    its text."""
    assert await async_setup_component(hass, "config", {})
    (device,), _search = await notice(hass, zha, repo_dir, aioclient_mock)
    answers(device)
    issue_id = issue_of(hass, FINE).issue_id
    entry = entry_of(hass)
    client = await hass_client()
    with patch.object(checks, "CHECK_WAIT", 0.3), patch.object(checks, "CHECK_QUIET", 0.05), patch.object(checks, "CHECK_STEP", 0.05):
        first = await (await client.post("/api/repairs/issues/fix", json={"handler": const.DOMAIN, "issue_id": issue_id})).json()
        assert first["type"] == "menu" and first["step_id"] == FIRST
        response = await client.post(f"/api/repairs/issues/fix/{first['flow_id']}", json={"next_step_id": "check"})
        handed = await response.json()
        assert response.status == 200 and handed["type"] == "abort" and handed["reason"] == "check_started", handed
        assert handed["next_flow"][0] == "config_subentries_flow" and handed["result"]["entry_id"] == entry.entry_id and handed["result"]["domain"] == const.DOMAIN
        assert still_open(hass) and issue_of(hass, FINE).issue_id == issue_id
        path = f"/api/config/config_entries/subentries/flow/{handed['next_flow'][1]}"
        fetched = await (await client.get(path)).json()
        # Still asking (or done already: then its values).
        assert fetched["handler"] == [entry.entry_id, "device_check"] and fetched["step_id"] in ("ask", "values"), fetched
        await hass.async_block_till_done(wait_background_tasks=True)
    values = await (await client.get(path)).json()
    assert values["type"] == "menu" and values["step_id"] == "values"
    report = await (await client.post(path, json={"next_step_id": "all_match"})).json()
    assert report["type"] == "form" and report["step_id"] == "report"
    assert still_open(hass)
    opened = await (await client.post(path, json={})).json()
    assert opened["type"] == "external" and opened["step_id"] == "open" and opened["url"].startswith(f"https://github.com/{const.REPOSITORY}/issues/new?")
    assert gone(hass)
    assert await (await client.get(path)).json() == opened
    # The dialog's X.
    assert (await client.delete(path)).status == 200
    assert (await client.get(path)).status == 404


async def test_closing_the_device_check_while_it_asks_keeps_the_notice(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """The one device of a notice is asked as soon as the device check starts. The dialog's X while it asks leaves the
    check (the frontend deletes none a notice handed over): no report, the notice stays, the next opening starts on its
    first page. Its "Check the device now" ends the check before and stops its query."""
    (device,), _search = await notice(hass, zha, repo_dir, aioclient_mock)
    flow = await check_now(hass, await start_fix(hass, issue_of(hass, FINE)))
    assert flow["type"] is FlowResultType.SHOW_PROGRESS
    assert manager_of(hass).checks._checking == {device.device_id: 1}
    task = handler_of(hass, flow)._task
    x(hass, flow)
    assert still_open(hass) and manager_of(hass).checks.reports == []
    first = await start_fix(hass, issue_of(hass, FINE))
    assert first["type"] is FlowResultType.MENU and first["step_id"] == FIRST
    again = await check_now(hass, first)
    await asyncio.sleep(0.01)
    assert task.cancelled() and manager_of(hass).checks._checking == {device.device_id: 1}
    assert [found["flow_id"] for found in device_checks(hass)] == [again["flow_id"]]
    hass.config_entries.subentries.async_abort(again["flow_id"])
    await hass.async_block_till_done(wait_background_tasks=True)
    assert manager_of(hass).checks._checking == {} and manager_of(hass).checks.reports == []
    assert still_open(hass) and device_checks(hass) == []


async def test_asking_for_the_progress_page_again_sends_no_second_query(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """The frontend fetches the flow (a GET runs its step again) when the dialog opens and when it subscribes to the
    progress. A step asked again waits for the same query. So does a doubled "Check the device now" on the old page:
    it reaches no flow (the fix flow ended)."""
    (device,), _search = await notice(hass, zha, repo_dir, aioclient_mock)
    answers(device)
    tuya = device.clusters[0xEF00]
    sent = len(tuya.sent)
    first = await start_fix(hass, issue_of(hass, FINE))
    with patch.object(checks, "CHECK_WAIT", 0.3), patch.object(checks, "CHECK_QUIET", 0.05), patch.object(checks, "CHECK_STEP", 0.05):
        flow = await check_now(hass, first)
        assert flow["type"] is FlowResultType.SHOW_PROGRESS
        for user_input in (None, None, {"device": device.device_id}, {"anything": 1}):
            again = await continue_check(hass, flow, user_input)
            assert again["type"] is FlowResultType.SHOW_PROGRESS and again["step_id"] == "ask"
        with pytest.raises(UnknownFlow):
            await continue_fix(hass, first, {"next_step_id": "check"})
        await hass.async_block_till_done(wait_background_tasks=True)
    assert len(tuya.sent) == sent + 1 and len(device_checks(hass)) == 1
    values = await continue_check(hass, flow)
    assert values["type"] is FlowResultType.MENU and values["step_id"] == "values"
    for _ in range(2):
        again = await continue_check(hass, flow)
        assert again["step_id"] == "values" and again["description_placeholders"] == values["description_placeholders"]
    assert len(tuya.sent) == sent + 1


async def test_the_handover_ends_with_the_reason_of_a_check_that_cannot_start(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """ZHA is reloading: the device check cannot start, the fix flow ends with its reason (a text of the notice's flow),
    no device check stays, the notice stays."""
    await notice(hass, zha, repo_dir, aioclient_mock)
    first = await start_fix(hass, issue_of(hass, FINE))
    zha.start_reload()
    result = await check_now(hass, first)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "zha_not_loaded"
    assert still_open(hass) and device_checks(hass) == []


async def test_a_notice_made_again_under_the_same_id_is_not_the_last_pages(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """The notice was made again under the same id while the last page showed (another self-check of the same file, its id
    pruned from the notified list): the button ends with "gone", opens nothing and closes nothing. The new notice stays:
    the check never saw that self-check."""
    (device,), _search = await notice(hass, zha, repo_dir, aioclient_mock)
    answers(device)
    page = await continue_fix(hass, await values_page(hass), {"next_step_id": "all_match"})
    assert last_page(page) == "report"
    data = issue_of(hass, FINE).data
    observation = manager_of(hass).checks.self_check(data.get("file"), data.get("revision"), data.get("device"))
    observation["started"] = f"{observation['started']} again"
    result = await continue_fix(hass, page, {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "gone"
    assert still_open(hass)


# What the device check of a notice is handed, and what no one else can hand it


async def notice_check(hass: HomeAssistant, data: object) -> dict:
    """A device check started with the source "notice" and this data (only the fix flow does that)."""
    result = await hass.config_entries.subentries.async_init((entry_of(hass).entry_id, "device_check"), context={"source": "notice"}, data=data)
    result["kind"] = FINE
    return check_device_check_texts(result)


def _handed(hass: HomeAssistant) -> dict:
    issue = issue_of(hass, FINE)
    return {"issue_id": issue.issue_id, "data": dict(issue.data), "answer": None}


HANDED = {
    "none": (lambda hass: None, "gone"),
    "list": (lambda hass: [], "gone"),
    "no_data": (lambda hass: {"issue_id": issue_of(hass, FINE).issue_id}, "gone"),
    "data_not_a_dict": (lambda hass: _handed(hass) | {"data": "z2m_tuya_a.py"}, "gone"),
    "id_not_a_string": (lambda hass: _handed(hass) | {"issue_id": 7}, "gone"),
    "other_id": (lambda hass: _handed(hass) | {"issue_id": "self_check_z2m_acme_c_00000000"}, "gone"),
    "other_file": (lambda hass: _handed(hass) | {"data": _handed(hass)["data"] | {"file": "z2m_acme_c.py"}}, "file_changed"),
    "file_not_a_string": (lambda hass: _handed(hass) | {"data": _handed(hass)["data"] | {"file": ["z2m_tuya_a.py"]}}, "file_changed"),
    "other_revision": (lambda hass: _handed(hass) | {"data": _handed(hass)["data"] | {"revision": "0" * 8}}, "file_changed"),
    "devices_not_a_list": (lambda hass: _handed(hass) | {"data": _handed(hass)["data"] | {"devices": "all"}}, "no_device"),
    "other_device": (lambda hass: _handed(hass) | {"data": _handed(hass)["data"] | {"devices": ["not-a-device"]}}, "no_device"),
}


@pytest.mark.parametrize("handed", list(HANDED))
async def test_a_device_check_handed_other_data_ends_and_asks_nothing(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, handed: str):
    (device,), _search = await notice(hass, zha, repo_dir, aioclient_mock)
    answers(device)
    sent = len(device.clusters[0xEF00].sent)
    data, reason = HANDED[handed]
    result = await notice_check(hass, data(hass))
    assert result["type"] is FlowResultType.ABORT and result["reason"] == reason
    assert len(device.clusters[0xEF00].sent) == sent and device_checks(hass) == [] and still_open(hass)
    # The notice's own data: it starts and asks the device.
    with patch.object(checks, "CHECK_WAIT", 0.3), patch.object(checks, "CHECK_QUIET", 0.05), patch.object(checks, "CHECK_STEP", 0.05):
        result = await notice_check(hass, _handed(hass))
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_device_check_handed_the_id_of_another_notice_ends(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """The id names a notice that stands, but not the one of this self-check: "gone", nothing asked."""
    (device,), _search = await notice(hass, zha, repo_dir, aioclient_mock)
    sent, handed = len(device.clusters[0xEF00].sent), _handed(hass)
    ir.async_create_issue(hass, const.DOMAIN, "self_check_z2m_tuya_a_other", data={"kind": FINE, "file": "z2m_tuya_a.py"}, is_fixable=True, is_persistent=True,
                          severity=ir.IssueSeverity.WARNING, translation_key=FINE, translation_placeholders={"quirk": "Tuya A"})
    result = await notice_check(hass, handed | {"issue_id": "self_check_z2m_tuya_a_other"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "gone"
    assert len(device.clusters[0xEF00].sent) == sent and device_checks(hass) == []
    ir.async_delete_issue(hass, const.DOMAIN, "self_check_z2m_tuya_a_other")


async def test_a_device_check_for_a_closed_notice_ends(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    await notice(hass, zha, repo_dir, aioclient_mock)
    handed = _handed(hass)
    await _skipped_elsewhere(hass, zha, repo_dir, None, "report")
    result = await notice_check(hass, handed)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "gone" and device_checks(hass) == []


async def test_the_frontend_starts_only_the_buttons_device_check(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, hass_client):
    """Home Assistant's HTTP API starts a subentry flow with the source "user" whatever the request says: no request makes
    a device check of a notice. Nor one that reconfigures a subentry (the type has no such step), nor another type."""
    assert await async_setup_component(hass, "config", {})
    await notice(hass, zha, repo_dir, aioclient_mock)
    entry = entry_of(hass)
    client = await hass_client()
    handed = _handed(hass)
    started = await (await client.post("/api/config/config_entries/subentries/flow",
                                       json={"handler": [entry.entry_id, "device_check"], "source": "notice", "context": {"source": "notice"}, **handed})).json()
    assert started["type"] == "form" and started["step_id"] == "check", started
    assert hass.config_entries.subentries.async_get(started["flow_id"])["context"]["source"] == "user"
    response = await client.post("/api/config/config_entries/subentries/flow", json={"handler": [entry.entry_id, "device_check"], "subentry_id": "x"})
    assert response.status == 400
    response = await client.post("/api/config/config_entries/subentries/flow", json={"handler": [entry.entry_id, "other"]})
    assert response.status in (400, 404)
    with pytest.raises(UnknownStep):
        await hass.config_entries.subentries.async_init((entry.entry_id, "device_check"), context={"source": "reconfigure", "subentry_id": "x"})
    assert still_open(hass) and [flow["flow_id"] for flow in device_checks(hass)] == [started["flow_id"]]


async def test_an_unknown_device_check_answers_unknown_flow(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    await notice(hass, zha, repo_dir, aioclient_mock)
    for flow_id in ("not-a-flow", "", "0" * 32):
        with pytest.raises(UnknownFlow):
            await hass.config_entries.subentries.async_configure(flow_id, {})
        with pytest.raises(UnknownFlow):
            hass.config_entries.subentries.async_abort(flow_id)
    assert still_open(hass)


@pytest.mark.parametrize("step", LAST_PAGES)
async def test_any_other_input_on_the_last_page_is_no_click(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, step: str):
    """A page without fields takes any input (Home Assistant checks none): only the button's empty input is the click."""
    flow = await last(hass, zha, repo_dir, aioclient_mock, step)
    for user_input in (None, {"anything": 1}, {"next_step_id": "check"}, {"next_step_id": "mismatch"}, {"device": "x"}, {"matches": ["1-temperature"], "wrong": []},
                       {"": ""}):
        shown = await continue_fix(hass, flow, user_input)
        assert last_page(shown) == step and shown["description_placeholders"] == flow["description_placeholders"], (user_input, shown)
    assert still_open(hass) and len(manager_of(hass).checks.reports) == 1
    await click(hass, flow, step)
    assert gone(hass)


async def test_the_page_after_the_button_never_ends(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """Home Assistant lets an external step lead only to another one (it raises otherwise). After the click nothing ends
    the step: the entry unloaded, the notice gone, any input. Each call answers the same step."""
    flow = await last(hass, zha, repo_dir, aioclient_mock, "report")
    url = await click(hass, flow, "report")
    await hass.config_entries.async_unload(entry_of(hass).entry_id)
    for user_input in (None, {}, {"next_step_id": "all_match"}, {"anything": 1}):
        opened = await continue_fix(hass, flow, user_input)
        assert opened["type"] is FlowResultType.EXTERNAL_STEP and opened["step_id"] == "open" and opened["url"] == url


# The texts


# The steps of the device check whose page Home Assistant shows: the picker, the progress dialog (its title, the text is
# the progress action's), the values, the list, the last pages and the page after their button (an external step).
SHOWN = {"check", "ask", "values", "values_no_answer", "values_no_answer_mains", "results", *LAST_PAGES, "open", "open_test"}
# The end pages once the tab opened (test_links.py): abort reasons of the step "done".
END_PAGES = {"opened", "opened_test"}
# The reasons the notice's fix flow ends with: its own and those of a device check that cannot start, which it passes on.
NOTICE_ABORTS = {"not_loaded", "zha_not_loaded", "no_device", "device_gone", "check_failed", "no_entities", "confirmed", "skipped", "gone", "file_changed",
                 "check_started"}


def test_the_texts_of_the_notices_and_the_device_check():
    folder = integration.FOLDER / "translations"
    import json

    steps = {name.removeprefix("async_step_") for name in dir(config_flow.DeviceCheckFlow) if name.startswith("async_step_")}
    # The ways in and the choices lead to a page of another step, "done" to the end page (an abort).
    assert steps - SHOWN == {"user", "notice", "mismatch", "all_match", "done"}
    for path in sorted(folder.glob("*.json")):
        strings = json.loads(path.read_text())
        flows = [strings["issues"][kind]["fix_flow"] for kind in checks.KINDS]
        for flow in flows:
            # The fix flow is the first page: no page of the device check.
            assert set(flow) == {"step", "abort"} and set(flow["step"]) == {FIRST}, path.name
            assert set(flow["step"][FIRST]["menu_options"]) == {"check", "skip"} and set(flow["abort"]) == NOTICE_ABORTS, path.name
        assert flows[0]["abort"] == flows[1]["abort"] == flows[2]["abort"], path.name
        # The first pages differ.
        assert len({json.dumps(flow["step"][FIRST], sort_keys=True) for flow in flows}) == 3, path.name
        check = strings["config_subentries"]["device_check"]
        assert set(check) == {"initiate_flow", "entry_type", "step", "progress", "error", "abort"}, path.name
        assert set(check["initiate_flow"]) == {"user"} and check["initiate_flow"]["user"] and check["entry_type"], path.name
        assert set(check["step"]) == SHOWN, path.name
        assert set(check["step"]["ask"]) == {"title"} and set(check["progress"]) == {"asking"}, path.name
        # Each last page: a title, its text and the text of its one button. The page after it: a title and its text.
        assert all(set(check["step"][step]) == {"title", "description", "submit"} for step in LAST_PAGES), path.name
        assert all(set(check["step"][step]) == {"title", "description"} for step in ("open", "open_test")), path.name
        assert set(check["abort"]) == {"not_loaded", "zha_not_loaded", "no_device", "nothing_to_check", "device_gone", "check_failed", "no_entities", "gone",
                                       "file_changed", *END_PAGES}, path.name
        # A device check of a notice that cannot start ends the fix flow with its reason: each one is a reason of the fix
        # flow too ("nothing to check" is the button's, the end pages come only after the button).
        assert set(check["abort"]) - {"nothing_to_check", *END_PAGES} <= set(flows[0]["abort"]), path.name
        # The page after the button stays only when the browser blocked the tab: it points to HA's Open website button. The
        # end page (the tab opened) thanks and has no link.
        assert all("**" in check["step"][step]["description"] and "**" not in check["abort"][OPENED] for step, OPENED in config_flow.OPENED.items()), path.name
        # The notice's last pages moved, the options keep only the settings.
        assert set(strings["options"]["step"]) == {"settings"}, path.name


def test_the_page_after_the_button_says_what_opened():
    # This page stays only when no tab reached the one-time address: it names HA's own button on it, then the direct link
    # (a browser that cannot reach this Home Assistant's address in a new tab).
    assert CHECK["step"]["open"] == {"title": "Form on GitHub", "description": "No new tab? **Open website** opens the form. If that fails, [open the form on GitHub]({link})."}
    assert CHECK["step"]["open_test"] == {"title": "Device test on GitHub",
                                          "description": "No new tab? **Open website** opens the device test. If that fails, [open it on GitHub]({link})."}
    assert "thumbs-up" in CHECK["abort"]["opened_test"] and "Open website" not in CHECK["abort"]["opened"]
    assert CHECK["initiate_flow"]["user"] == "Check a device"
