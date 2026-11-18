"""The fix flow of a self-check notice: the guided device check.

The first page says what the notice found and offers "Check the device now" or "Do not check". Before it shows, one
search (short timeout) tells whether every device of the notice is past the threshold now: then the notice goes. The
device check is the options flow's: the device picker (limited to the notice's devices, skipped for one), the query
with its progress step, the values. Then "All values match" (a thumbs-up on the open issue of the variant, else the
filled-in form) or "Something does not match" (today's list, then the problem report). Each last page has one button that
opens its link (no Close). No test reaches the network.
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
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData, UnknownFlow
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMockResponse
from yarl import URL

from . import integration
from .conftest import REPOS, STRINGS, Zha, continue_fix, issue_of, issues_of, manager_of, start_fix
from .integration import confirm, const, diagnostics, pictures
from .test_checks import ACME_C, IEEE_A, IEEE_C, NAME, TUYA_A, LiveDevice, acme, checks, conclude, form_ids, installed, link_fields, radar, run_check, text
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

FINE, OFF, UNPROVEN = checks.FINE, checks.OFF, checks.UNPROVEN
ISSUE_LINK = f"https://github.com/{const.REPOSITORY}/issues/{{number}}"
# Steps and aborts every kind shares (the first page differs).
FIRST = "confirm"
# The pages a device check ends with: one button each, which opens a link.
LAST_PAGES = ("report", "report_wrong", "thumbs_up")


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


async def ask(hass: HomeAssistant, flow: dict, user_input: dict) -> dict:
    """Go on into the device check: its progress step to the end, the page after it."""
    with patch.object(checks, "CHECK_WAIT", 0.3), patch.object(checks, "CHECK_QUIET", 0.05), patch.object(checks, "CHECK_STEP", 0.05):
        flow = await continue_fix(hass, flow, user_input)
        if flow["type"] is not FlowResultType.SHOW_PROGRESS:
            return flow
        assert flow["step_id"] == "ask" and flow["progress_action"] == "asking"
        assert STRINGS["issues"][flow["kind"]]["fix_flow"]["progress"]["asking"]
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
    assert STRINGS["issues"][flow["kind"]]["fix_flow"]["step"][step]["submit"]
    opened = await continue_fix(hass, flow, {})
    assert opened["type"] is FlowResultType.EXTERNAL_STEP and opened["step_id"] == "open", opened
    return opened["url"]


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
    assert gone(hass)
    assert link_fields(await click(hass, flow, "report"))["template"] == text.VERIFY_FORM


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
    # Finished like a report: the notice goes for this revision, the report is in the diagnostics.
    assert gone(hass) and len(manager_of(hass).checks.reports) == 1
    assert await click(hass, flow, "thumbs_up") == ISSUE_LINK.format(number=42)
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
    flow = await continue_fix(hass, flow, {"next_step_id": "check"})
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


async def test_the_list_without_a_wrong_value_never_ends_with_a_thumbs_up(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """After "Something does not match" only "All values match" leads to the thumbs-up. Every value marked matching and
    none wrong: back to the values page to choose it. Some marked matching, none wrong: the report with the values not
    checked, never a thumbs-up."""
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock)
    search.reply = answer(hit(42, title(FIRMWARE_A), thumbs=1))
    answers(device)
    flow = await values_page(hass)
    flow = await continue_fix(hass, flow, {"next_step_id": "mismatch"})
    rows = [option["value"] for option in flow["data_schema"].schema["matches"].config["options"]]
    assert len(rows) == 3
    flow = await continue_fix(hass, flow, {"matches": rows, "wrong": []})
    assert flow["type"] is FlowResultType.MENU and flow["step_id"] == "values" and flow["menu_options"] == ["all_match", "mismatch"], flow
    assert manager_of(hass).checks.reports == [] and not gone(hass)
    flow = await continue_fix(hass, flow, {"next_step_id": "mismatch"})
    flow = await continue_fix(hass, flow, {"matches": ["1-temperature"], "wrong": []})
    assert flow["description_placeholders"] == {"matches": "1", "unchecked": "2"}
    assert link_fields(await click(hass, flow, "report"))["template"] == text.VERIFY_FORM
    assert "| yes | sensor: Temperature |" in manager_of(hass).checks.reports[-1]["report"]
    assert "| not checked | sensor: Humidity |" in manager_of(hass).checks.reports[-1]["report"]
    assert gone(hass)


async def test_all_values_match_after_going_back_gives_the_thumbs_up(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock)
    search.reply = answer(hit(42, title(FIRMWARE_A), thumbs=1))
    answers(device)
    flow = await values_page(hass)
    flow = await continue_fix(hass, flow, {"next_step_id": "mismatch"})
    rows = [option["value"] for option in flow["data_schema"].schema["matches"].config["options"]]
    flow = await continue_fix(hass, flow, {"matches": rows, "wrong": []})
    flow = await continue_fix(hass, flow, {"next_step_id": "all_match"})
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
    assert gone(hass, OFF)


# The last page


async def last(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, step: str) -> dict:
    """The flow of a looks-fine notice up to this last page."""
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock)
    if step == "thumbs_up":
        search.reply = answer(hit(42, title(FIRMWARE_A), thumbs=1))
    answers(device)
    flow = await values_page(hass)
    if step == "report_wrong":
        flow = await continue_fix(hass, flow, {"next_step_id": "mismatch"})
        return await continue_fix(hass, flow, {"matches": ["1-humidity"], "wrong": ["1-temperature"]})
    return await continue_fix(hass, flow, {"next_step_id": "all_match"})


@pytest.mark.parametrize("step", LAST_PAGES)
@pytest.mark.parametrize("clicked", [True, False], ids=["clicked", "closed"])
async def test_the_last_page_has_one_button_that_opens_its_link(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, step: str, clicked: bool):
    """A page without fields shows one button with its own text (an abort page always shows Close). The check is over
    when the page shows: the notice is gone, the report kept. The button opens the link (the form or the device test of
    the variant) as an external step: Home Assistant opens it in a new tab and offers it as a button too. The dialog
    stays on that step until its X closes it, which deletes the flow. Closing it on either page changes nothing more."""
    flow = await last(hass, zha, repo_dir, aioclient_mock, step)
    assert last_page(flow) == step
    checks_ = manager_of(hass).checks
    assert gone(hass) and len(checks_.reports) == 1
    before = checks_.summary()
    # Asked for the page again (a GET of the flow): the same page, nothing opened.
    again = await continue_fix(hass, flow, None)
    assert last_page(again) == step and again["description_placeholders"] == flow["description_placeholders"]
    if clicked:
        url = await click(hass, again, step)
        if step == "thumbs_up":
            assert url == ISSUE_LINK.format(number=42)
        else:
            assert url.startswith(f"https://github.com/{const.REPOSITORY}/issues/new?")
            assert link_fields(url)["template"] == (text.PROBLEM_FORM if step == "report_wrong" else text.VERIFY_FORM)
        # Asked for again, or sent anything: the same external step (no event for the frontend).
        for user_input in (None, {"anything": 1}):
            opened = await continue_fix(hass, flow, user_input)
            assert opened["type"] is FlowResultType.EXTERNAL_STEP and opened["step_id"] == "open" and opened["url"] == url
    # The dialog's X: Home Assistant deletes a flow that did not end.
    hass.data["repairs"]["flow_manager"].async_abort(flow["flow_id"])
    with pytest.raises(UnknownFlow):
        await continue_fix(hass, flow, {})
    assert checks_.summary() == before and len(checks_.reports) == 1
    assert gone(hass) and self_check(hass)["device_check_done"]
    assert not await comes_back(hass, repo_dir)


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
        flow = await continue_fix(hass, flow, {"next_step_id": "check"})
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
    flow = await continue_fix(hass, flow, {"next_step_id": "check"})
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "no_device"


async def test_a_file_updated_while_the_device_is_asked_is_not_reported(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    (device,), _search = await notice(hass, zha, repo_dir, aioclient_mock)
    tuya = device.clusters[0xEF00]
    flow = await start_fix(hass, issue_of(hass, FINE))
    with patch.object(checks, "CHECK_WAIT", 0.3), patch.object(checks, "CHECK_QUIET", 0.05), patch.object(checks, "CHECK_STEP", 0.05):
        tuya.answer = lambda: fine_values(device)
        flow = await continue_fix(hass, flow, {"next_step_id": "check"})
        assert flow["type"] is FlowResultType.SHOW_PROGRESS
        await _update_file(hass, zha, repo_dir, device)
        await hass.async_block_till_done(wait_background_tasks=True)
    flow = await continue_fix(hass, flow)
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "file_changed"


async def test_the_flow_of_an_unloaded_entry_ends(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    await notice(hass, zha, repo_dir, aioclient_mock)
    issue = issue_of(hass, FINE)
    await hass.config_entries.async_unload(hass.config_entries.async_entries(const.DOMAIN)[0].entry_id)
    assert ir.async_get(hass).async_get_issue(const.DOMAIN, issue.issue_id) is not None
    flow = await start_fix(hass, issue)
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "not_loaded"


@pytest.mark.parametrize("step", ["all_match", "mismatch", "results", "ask", "values", "report", "report_wrong", "thumbs_up", "open"])
async def test_a_step_out_of_order_changes_nothing(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, step: str):
    """Home Assistant refuses a choice the menu does not offer. Reached anyway, a step without a device check ends."""
    await notice(hass, zha, repo_dir, aioclient_mock)
    flow = await start_fix(hass, issue_of(hass, FINE))
    with pytest.raises(InvalidData):
        await continue_fix(hass, flow, {"next_step_id": step})
    handler = repairs.SelfCheckFlow(issue_of(hass, FINE).issue_id, dict(issue_of(hass, FINE).data))
    handler.hass = hass
    result = await getattr(handler, f"async_step_{step}")()
    assert result["type"] is FlowResultType.ABORT and result["reason"] in STRINGS["issues"][FINE]["fix_flow"]["abort"]
    assert manager_of(hass).checks.reports == [] and issue_of(hass, FINE)


# Two flows for one notice


async def test_two_flows_for_one_notice(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    (device,), _search = await notice(hass, zha, repo_dir, aioclient_mock)
    answers(device)
    first, second = await values_page(hass), await values_page(hass)
    assert first["flow_id"] != second["flow_id"]
    first = await continue_fix(hass, first, {"next_step_id": "all_match"})
    assert last_page(first) == "report"
    second = await continue_fix(hass, second, {"next_step_id": "all_match"})
    assert second["type"] is FlowResultType.ABORT and second["reason"] == "gone"
    assert len(manager_of(hass).checks.reports) == 1


async def test_do_not_check_in_one_flow_ends_the_other(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    await notice(hass, zha, repo_dir, aioclient_mock)
    issue = issue_of(hass, FINE)
    first, second = await start_fix(hass, issue), await start_fix(hass, issue)
    first = await continue_fix(hass, first, {"next_step_id": "skip"})
    assert first["reason"] == "skipped"
    second = await continue_fix(hass, second, {"next_step_id": "skip"})
    assert second["type"] is FlowResultType.ABORT and second["reason"] == "gone"
    assert gone(hass)


# The options flow's device check


async def test_the_options_check_keeps_its_picker_and_names_the_variant_in_the_form_title(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """The options flow asks which device also for one. Its form link carries the same title as the fix flow's."""
    prepare(repo_dir)
    device = radar(hass, zha)
    set_firmware(hass, device.device_id, FIRMWARE_A)
    Search(aioclient_mock)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    answers(device)
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    result = await run_check(hass, entry, device.device_id)
    assert result["step_id"] == "results"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"matches": ["1-temperature"], "wrong": []})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "report"
    assert link_fields(result["description_placeholders"]["link"])["title"] == title(FIRMWARE_A)


async def test_an_options_check_of_a_zcl_file_is_unchanged(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    prepare(repo_dir)
    device = acme(hass, zha)
    Search(aioclient_mock)
    await installed(hass, zha, repo_dir, {IEEE_C: ACME_C})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    result = await run_check(hass, entry, device.device_id)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "results"


# The texts


def test_every_kind_shares_the_texts_after_the_first_page():
    folder = integration.FOLDER / "translations"
    import json

    for path in sorted(folder.glob("*.json")):
        issues = json.loads(path.read_text())["issues"]
        flows = [issues[kind]["fix_flow"] for kind in checks.KINDS]
        for flow in flows:
            assert set(flow["step"][FIRST]["menu_options"]) == {"check", "skip"}, path.name
            assert set(flow["step"]) == {FIRST, "check", "values", "values_no_answer", "values_no_answer_mains", "results", *LAST_PAGES}, path.name
            assert set(flow["abort"]) == {"not_loaded", "zha_not_loaded", "no_device", "device_gone", "check_failed", "no_entities", "confirmed", "skipped", "gone",
                                          "file_changed"}, path.name
            # Each last page: a title, its text and the text of its one button.
            assert all(set(flow["step"][step]) == {"title", "description", "submit"} for step in LAST_PAGES), path.name
        shared = [{**flow, "step": {step: texts for step, texts in flow["step"].items() if step != FIRST}} for flow in flows]
        assert shared[0] == shared[1] == shared[2], path.name
        # The first pages differ.
        assert len({json.dumps(flow["step"][FIRST], sort_keys=True) for flow in flows}) == 3, path.name
