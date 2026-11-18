"""The confirmation search: a self-check whose day is over asks GitHub's issue search how many people confirmed its
file on each device's variant, then its notice leaves out the devices past the threshold. A verified file gets no
"fine" or "unproven" notice. "Looks off" always comes. An unknown answer, a missing key or threshold: the notice as
before. No test here reaches the network (aioclient_mock answers every request)."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import async_fire_time_changed
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMockResponse
from yarl import URL

from . import integration
from .conftest import GUARD_ID, MANIFEST, REPOS, Zha, continue_fix, issue_of, issues_of, load_state, manager_of, setup_entry, start_fix, write_repo
from .integration import confirm, const, content, manager
from .test_checks import ACME_C, IEEE_A, IEEE_C, NAME, TUYA_A, CustomCluster, LiveDevice, acme, checks, conclude, installed, radar, settle
from .test_content import restart_with

remote = integration.manager.remote

IEEE_B = "00:11:22:33:44:55:66:02"
HALL = "Radar in the hall"
KEY_A = "1a2b3c4d"
KEY_C = "00c0ffee"
FIRMWARE_A = "0x00000046"
FIRMWARE_B = "0x00000063"
QUERY = f"repo:{const.REPOSITORY} is:issue is:open label:verify in:title {KEY_A}"


def keyed(**changes: dict) -> dict:
    """MANIFEST with a key on the entries of z2m_tuya_a.py and z2m_acme_c.py (changes: fields per slug)."""
    found = copy.deepcopy(MANIFEST)
    found["tuya_a"]["key"] = KEY_A
    found["acme_c"]["key"] = KEY_C
    for slug, fields in changes.items():
        found[slug].update(fields)
    return found


def prepare(repo_dir: Path, manifest: dict | None = None, threshold: object = 2, version: int = 1) -> None:
    """The working tree of the next content tag: the manifest (keyed() by default) and content.json with the threshold
    (left out for ...)."""
    write_repo(repo_dir, keyed() if manifest is None else manifest, version=version)
    data = {"guard": GUARD_ID} if threshold is ... else {"guard": GUARD_ID, "confirm_threshold": threshold}
    (repo_dir / "content.json").write_text(json.dumps(data))


def title(firmware: str | None, manufacturer: str = "_TZE200_aaaa", model: str = "TS0601", key: str = KEY_A) -> str:
    return confirm.title("Tuya A", manufacturer, model, firmware, key)


def hit(number: int, issue_title: str, thumbs: object = 0, **fields) -> dict:
    """An item of a search answer as GitHub sends it (the parts the integration never reads included)."""
    return {
        "number": number, "title": issue_title, "state": "open", "body": "### Device\n\n[link](https://example.com) @someone",
        "html_url": f"https://github.com/{const.REPOSITORY}/issues/{number}", "user": {"login": "someone"},
        "reactions": {"url": "https://api.github.com/x", "total_count": 0, "+1": thumbs, "-1": 0}, **fields,
    }


def answer(*items: dict, **fields) -> dict:
    return {"total_count": len(items), "incomplete_results": False, "items": list(items), "search_type": "lexical", **fields}


def searches(aioclient_mock) -> list[URL]:
    """The search requests so far. None carries a token or anything but the agreed query."""
    found = []
    for _method, url, _data, headers in aioclient_mock.mock_calls:
        if str(url).startswith(const.SEARCH_URL):
            assert not any(name.lower() in ("authorization", "cookie") for name in (headers or {})), headers
            found.append(url)
    return found


def set_firmware(hass: HomeAssistant, device_id: str, firmware: str | None) -> None:
    dr.async_get(hass).async_update_device(device_id, sw_version=firmware)


def hall(hass: HomeAssistant, zha: Zha) -> LiveDevice:
    """A second device z2m_tuya_a.py is for."""
    tuya = CustomCluster(0xEF00, handled=(1, 2, 3))
    return LiveDevice(hass, zha, IEEE_B, "_TZE200_aaaa", "TS0601", [
        ("sensor", "temperature", tuya, "temperature", None, True),
        ("sensor", "humidity", tuya, "humidity", None, True),
        ("number", "max_temp", tuya, "max_temp", "config", True),
    ], name=HALL)


def fine_values(device: LiveDevice) -> None:
    """Values of the file that look right: the evidence of a "fine" notice."""
    device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    device.value("humidity", "48", device_class="humidity", unit_of_measurement="%")


def notice_devices(hass: HomeAssistant, kind: str) -> list[str]:
    """The device lines of the one notice of this kind."""
    (issue,) = issues_of(hass, kind)
    return [line for line in issue.translation_placeholders["devices"].splitlines() if line.startswith("- ")]


def self_check(hass: HomeAssistant, name: str = "z2m_tuya_a.py") -> dict | None:
    return manager_of(hass).summary()["self_check"].get(name)


async def due(hass: HomeAssistant) -> None:
    """The next count finds the self-checks due. It does not wait for the searches it starts."""
    with patch.object(checks, "WINDOW", timedelta(0)):
        async_fire_time_changed(hass, dt_util.utcnow() + checks.TICK)
        await hass.async_block_till_done()


class Gate:
    """A search answer held back until open() (only the first `held` requests), counting the requests in flight."""

    def __init__(self, reply, held: int = 1) -> None:
        self.reply, self.held = reply, held
        self.event = asyncio.Event()
        self.calls = self.in_flight = self.most = 0

    async def __call__(self, method, url, data):
        self.calls += 1
        self.in_flight += 1
        self.most = max(self.most, self.in_flight)
        try:
            if self.calls <= self.held:
                await self.event.wait()
            body = self.reply(url) if callable(self.reply) else self.reply
            return AiohttpClientMockResponse(method, URL(url), json=body)
        finally:
            self.in_flight -= 1

    async def open(self, hass: HomeAssistant) -> None:
        self.event.set()
        await hass.async_block_till_done(wait_background_tasks=True)


# The request


def bodiless(data: dict) -> dict:
    """An answer as async_search gives it back: each hit without its body."""
    return {**data, "items": [{field: value for field, value in item.items() if field != "body"} for item in data["items"]]}


async def test_the_search_asks_for_the_key_alone_without_a_token(hass: HomeAssistant, aioclient_mock):
    aioclient_mock.get(const.SEARCH_URL, json=answer(hit(7, title(FIRMWARE_A))))
    found = await remote.async_search(async_get_clientsession(hass), KEY_A)
    assert found == bodiless(answer(hit(7, title(FIRMWARE_A))))
    ((method, url, data, headers),) = aioclient_mock.mock_calls
    assert method.lower() == "get" and data is None
    assert f"{url.scheme}://{url.host}{url.path}" == const.SEARCH_URL
    assert dict(url.query) == {"q": QUERY, "sort": "created", "order": "asc", "per_page": str(const.SEARCH_PAGE)}
    assert headers == remote.HEADERS and not any(name.lower() == "authorization" for name in headers)
    assert const.SEARCH_PAGE == 10 and const.SEARCH_TIMEOUT <= 10


@pytest.mark.parametrize("key", ["1A2B3C4D", "1a2b3c4", "1a2b3c4d0", "1a2b3c4d ", "1a2b3c4d\n", " 1a2b3c4d", "1a2b3c4g", "1a2b3c4d in:body",
                                 "../../x", "", None, 0x1A2B3C4D, ["1a2b3c4d"], True, b"1a2b3c4d", "１a2b3c4d"])
async def test_a_key_of_another_shape_is_never_sent(hass: HomeAssistant, aioclient_mock, key):
    aioclient_mock.get(const.SEARCH_URL, json=answer())
    assert await remote.async_search(async_get_clientsession(hass), key) is None
    assert aioclient_mock.mock_calls == []


BIG = b'{"total_count": 0, "incomplete_results": false, "items": []}'


@pytest.mark.parametrize("response", [
    {"status": 403, "json": {"message": "API rate limit exceeded"}, "headers": {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1795006270"}},
    {"status": 403, "json": {"message": "secondary rate limit"}, "headers": {"Retry-After": "60"}},
    {"status": 429, "headers": {"Retry-After": "3600"}},
    {"status": 500},
    {"status": 404},
    {"status": 422, "json": {"message": "Validation Failed"}},
    {"status": 304},
    {"status": 301, "headers": {"Location": "https://example.com/"}},
    {"exc": TimeoutError()},
    {"exc": aiohttp.ClientConnectionError("offline")},
    {"exc": aiohttp.ClientPayloadError("cut off")},
    {"content": BIG + b" " * const.MAX_SEARCH, "headers": {"Content-Length": str(len(BIG) + const.MAX_SEARCH)}},
    {"content": BIG + b" " * const.MAX_SEARCH},
    {"text": "<html>Service unavailable</html>"},
    {"content": b""},
    {"content": b'\xff\xfe{"items": []}'},
    {"content": b"[" * 200_000 + b"]" * 200_000},
    {"content": b'{"items": [' + b"1," * 10 + b"]"},
], ids=["403_rate_limit", "403_secondary", "429", "500", "404", "422", "304", "301", "timeout", "offline", "payload", "too_large_by_length",
        "too_large_while_read", "html", "empty", "not_utf8", "nested_too_deep", "cut_json"])
async def test_a_failed_search_is_an_unknown_answer_and_not_repeated(hass: HomeAssistant, aioclient_mock, response):
    aioclient_mock.get(const.SEARCH_URL, **response)
    assert await remote.async_search(async_get_clientsession(hass), KEY_A) is None
    assert len(aioclient_mock.mock_calls) == 1


# The rest of a hit as GitHub sends it: links, the account, labels, reactions (measured hits are about 3 KB).
FIELDS = {"user": {"login": "x" * 39, **{f"{name}_url": "https://api.github.com/users/" + "x" * 39 for name in "abcdefghijklmnop"}},
          "labels": [{"id": 1, "name": "verify", "color": "ededed", "description": "x" * 100, "url": "https://api.github.com/x" * 3}] * 3,
          **{f"{name}_url": f"https://api.github.com/repos/{const.REPOSITORY}/issues/12345" for name in "abcdefghij"}}


def hostile_page(numbers, total: int, match: int | None = None) -> bytes:
    """A page as hostile issues make it: each hit has a body of 65,536 "<" (GitHub's JSON writes `\\u003c`, 6 bytes) and a
    title of "<" up to GitHub's 256 characters that holds the key. The hit numbered match has the variant's title."""
    filler = "<" * (256 - len(f" {KEY_A}"))
    items = [hit(n, title(FIRMWARE_A) if n == match else f"{filler} {KEY_A}", body="<" * 65_536, **FIELDS) for n in numbers]
    return json.dumps(answer(*items, total_count=total)).replace("<", "\\u003c").encode()


async def test_a_search_answer_up_to_the_limit_is_read(hass: HomeAssistant, aioclient_mock):
    """The limit fits a page of hostile hits: bodies and titles as long as GitHub allows, each character escaped."""
    body = BIG + b" " * (const.MAX_SEARCH - len(BIG))
    aioclient_mock.get(const.SEARCH_URL, content=body)
    assert await remote.async_search(async_get_clientsession(hass), KEY_A) == json.loads(BIG)
    page = hostile_page(range(1, const.SEARCH_PAGE + 1), const.SEARCH_PAGE, match=3)
    assert 10 * (65_536 + 256) * 6 < len(page) < const.MAX_SEARCH
    aioclient_mock.clear_requests()
    aioclient_mock.get(const.SEARCH_URL, content=page)
    found = await remote.async_search(async_get_clientsession(hass), KEY_A)
    assert confirm.confirmation(found, confirm.fixed_part("_TZE200_aaaa", "TS0601", FIRMWARE_A, KEY_A)) == (confirm.FOUND, 3, 1)


@pytest.mark.parametrize("parsed", [[], "text", None, 7, {"items": "x"}, {"message": "Bad credentials"}])
async def test_json_of_another_shape_comes_back_as_it_is(hass: HomeAssistant, aioclient_mock, parsed):
    """The shared rules read whatever came (confirm.confirmation): an answer of another shape is an unknown one there."""
    aioclient_mock.get(const.SEARCH_URL, text=json.dumps(parsed))
    found = await remote.async_search(async_get_clientsession(hass), KEY_A)
    assert found == parsed
    assert confirm.confirmation(found, confirm.fixed_part("_TZE200_aaaa", "TS0601", FIRMWARE_A, KEY_A)).state == confirm.UNKNOWN


# Page by page: the next page while a variant has no match yet


FIXED_A = confirm.fixed_part("_TZE200_aaaa", "TS0601", FIRMWARE_A, KEY_A)
FIXED_B = confirm.fixed_part("_TZE200_aaaa", "TS0601", FIRMWARE_B, KEY_A)


def others(numbers) -> list[dict]:
    """Open issues of other variants of the file (another manufacturer id, same key)."""
    return [hit(n, title(FIRMWARE_A, manufacturer=f"_TZE200_{n:04d}")) for n in numbers]


class Pages:
    """GitHub's search page by page: pages[n - 1] answers page n (a JSON body, a status, an exception or bytes)."""

    def __init__(self, aioclient_mock, *pages: object) -> None:
        self.pages = list(pages)
        self.asked: list[int] = []
        aioclient_mock.get(const.SEARCH_URL, side_effect=self)

    async def __call__(self, method, url, data):
        page = int(URL(url).query.get("page", "1"))
        self.asked.append(page)
        reply = self.pages[page - 1]
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, int):
            return AiohttpClientMockResponse(method, URL(url), status=reply)
        if isinstance(reply, bytes):
            return AiohttpClientMockResponse(method, URL(url), response=reply)
        return AiohttpClientMockResponse(method, URL(url), json=reply)


async def test_the_next_page_holds_the_match(hass: HomeAssistant, aioclient_mock):
    """Ten open issues of other variants fill the first page: the second, asked the same way, holds the oldest issue of
    the variant (#57). The answer holds both pages, without bodies."""
    pages = Pages(aioclient_mock, answer(*others(range(1, 11)), total_count=12), answer(hit(57, title(FIRMWARE_A), thumbs=4), hit(58, title(FIRMWARE_A)), total_count=12))
    found = await remote.async_search(async_get_clientsession(hass), KEY_A, {FIXED_A})
    assert pages.asked == [1, 2]
    assert confirm.confirmation(found, FIXED_A) == (confirm.FOUND, 57, 5)
    assert len(found["items"]) == 12 and not any("body" in item for item in found["items"])
    first, second = searches(aioclient_mock)
    assert dict(first.query) == {"q": QUERY, "sort": "created", "order": "asc", "per_page": str(const.SEARCH_PAGE)}
    assert dict(second.query) == {**dict(first.query), "page": "2"}


async def test_no_next_page_when_every_variant_has_its_match_or_nothing_is_left(hass: HomeAssistant, aioclient_mock):
    """A match for every variant asked, a page that is not full, or GitHub counts no more: one page."""
    full = others(range(1, 10)) + [hit(10, title(FIRMWARE_A))]
    for first, wanted in [(answer(*full, total_count=30), {FIXED_A}), (answer(*others(range(1, 10)), total_count=30), {FIXED_A}),
                          (answer(*others(range(1, 11)), total_count=10), {FIXED_A}), (answer(*others(range(1, 11)), total_count=30), set()),
                          (answer(*others(range(1, 11)), total_count=30, incomplete_results=True), {FIXED_A})]:
        aioclient_mock.clear_requests()
        pages = Pages(aioclient_mock, first, answer(hit(57, title(FIRMWARE_A)), total_count=30))
        await remote.async_search(async_get_clientsession(hass), KEY_A, wanted)
        assert pages.asked == [1], first


async def test_the_next_page_until_every_variant_has_its_match(hass: HomeAssistant, aioclient_mock):
    """Two variants asked: the first page holds A's issue, the second B's."""
    pages = Pages(aioclient_mock, answer(*others(range(1, 10)), hit(10, title(FIRMWARE_A)), total_count=25),
                  answer(*others(range(11, 20)), hit(20, title(FIRMWARE_B), thumbs=2), total_count=25), answer(*others(range(21, 26)), total_count=25))
    found = await remote.async_search(async_get_clientsession(hass), KEY_A, {FIXED_A, FIXED_B})
    assert pages.asked == [1, 2]
    assert confirm.confirmation(found, FIXED_A) == (confirm.FOUND, 10, 1) and confirm.confirmation(found, FIXED_B) == (confirm.FOUND, 20, 3)


async def test_the_pages_stop_at_the_cap(hass: HomeAssistant, aioclient_mock):
    """SEARCH_PAGES pages at most, each full of other variants: the variant's issue may come later, so "unknown"."""
    assert const.SEARCH_PAGES == 5 and const.SEARCH_PAGES * const.SEARCH_PAGE <= confirm.MAX_ITEMS
    pages = Pages(aioclient_mock, *(answer(*others(range(n * 10 + 1, n * 10 + 11)), total_count=80) for n in range(8)))
    found = await remote.async_search(async_get_clientsession(hass), KEY_A, {FIXED_A})
    assert pages.asked == [1, 2, 3, 4, 5]
    assert confirm.confirmation(found, FIXED_A) == (confirm.UNKNOWN, None, None)
    # Up to the last page: no issue of the variant when GitHub counts no more.
    aioclient_mock.clear_requests()
    pages = Pages(aioclient_mock, *(answer(*others(range(n * 10 + 1, n * 10 + 11)), total_count=50) for n in range(5)))
    found = await remote.async_search(async_get_clientsession(hass), KEY_A, {FIXED_A})
    assert pages.asked == [1, 2, 3, 4, 5] and confirm.confirmation(found, FIXED_A) == (confirm.NO_ISSUE, None, 0)


@pytest.mark.parametrize("second", [403, 429, 500, TimeoutError(), [], {"items": "x"}, b"<html>", "other_total"],
                         ids=["403", "429", "500", "timeout", "list", "items", "no_json", "other_total"])
async def test_a_failed_next_page_is_an_unknown_answer_and_not_repeated(hass: HomeAssistant, aioclient_mock, second):
    """Any page that fails makes the whole answer unknown, without a retry. So does a page whose total_count differs
    (the hits changed between the requests: one may have moved to the page already read)."""
    if second == "other_total":
        second = answer(hit(57, title(FIRMWARE_A)), total_count=11)
    pages = Pages(aioclient_mock, answer(*others(range(1, 11)), total_count=12), second, answer())
    assert await remote.async_search(async_get_clientsession(hass), KEY_A, {FIXED_A}) is None
    assert pages.asked == [1, 2]


async def test_an_incomplete_next_page_marks_the_answer_incomplete(hass: HomeAssistant, aioclient_mock):
    Pages(aioclient_mock, answer(*others(range(1, 11)), total_count=12), answer(hit(57, title(FIRMWARE_A)), hit(58, title(FIRMWARE_B)), total_count=12,
                                                                                  incomplete_results=True))
    found = await remote.async_search(async_get_clientsession(hass), KEY_A, {FIXED_A})
    assert found["incomplete_results"] is True and confirm.confirmation(found, FIXED_A).state == confirm.UNKNOWN


async def test_hostile_pages_still_parse(hass: HomeAssistant, aioclient_mock):
    """Pages of hostile hits (bodies and titles as long as GitHub allows, each character escaped) are read one by one:
    the match on the third page is found, the bodies of the pages before are not kept."""
    pages = Pages(aioclient_mock, hostile_page(range(1, 11), 25), hostile_page(range(11, 21), 25), hostile_page(range(21, 26), 25, match=23))
    found = await remote.async_search(async_get_clientsession(hass), KEY_A, {FIXED_A})
    assert pages.asked == [1, 2, 3]
    assert confirm.confirmation(found, FIXED_A) == (confirm.FOUND, 23, 1)
    assert not any("body" in item for item in found["items"])


# The key and the threshold of a content state


@pytest.mark.parametrize(("key", "kept"), [
    (KEY_A, True), ("00000000", True), ("1A2B3C4D", False), ("1a2b3c4", False), ("1a2b3c4d\n", False), (" 1a2b3c4d", False),
    (0x1A2B3C4D, False), (None, False), ([KEY_A], False), ({"key": KEY_A}, False), (True, False), ("", False),
])
def test_a_manifest_key_of_another_shape_is_no_key(tmp_path: Path, key, kept: bool):
    """A file without a valid key has none: it is asked as before. The manifest is taken all the same."""
    folder = write_repo(tmp_path, keyed(tuya_a={"key": key}, acme_d={"key": KEY_C}, acme_b={"unknown_field": {"nested": [1]}}))
    state = load_state(folder)
    assert state.keys.get("z2m_tuya_a.py") == (key if kept else None)
    assert state.keys.get("z2m_acme_c.py") == KEY_C
    # A key on an entry without a file and a field this release does not know change nothing.
    assert set(state.keys) <= set(state.shas)


@pytest.mark.parametrize(("value", "kept"), [
    (10, 10), (1, 1), (1000, 1000), (0, None), (1001, None), (-1, None), ("10", None), (10.0, None), (True, None), (None, None), ([10], None),
    (2**80, None),
])
def test_a_threshold_of_another_shape_is_no_threshold(value, kept):
    assert content.parse_threshold(json.dumps({"guard": GUARD_ID, "confirm_threshold": value})) == kept
    assert content.parse_threshold(json.dumps({"guard": GUARD_ID})) is None
    assert content.parse_threshold("[10]") is None


async def test_the_threshold_comes_with_the_state_and_survives_a_restart(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    prepare(repo_dir, threshold=7)
    entry = await setup_entry(hass, repo_dir)
    state = manager_of(hass).source
    assert state.confirm_threshold == 7 and state.keys == {"z2m_tuya_a.py": KEY_A, "z2m_acme_c.py": KEY_C}
    # Without network after a restart: the stored state keeps both.
    aioclient_mock.clear_requests()
    aioclient_mock.get(const.TAGS_URL, exc=aiohttp.ClientConnectionError("offline"))
    await restart_with(hass, entry, repo_dir)
    state = manager_of(hass).source
    assert state.confirm_threshold == 7 and state.keys["z2m_tuya_a.py"] == KEY_A


def test_a_stored_threshold_of_another_shape_is_none():
    entry = {**MANIFEST["tuya_a"], "sha256": "0" * 64, "revision": "0123abcd", "key": KEY_A}
    stored = {"tag": "content-4", "number": 4, "commit": "c" * 40, "homeassistant": "2026.1.0", "withdrawn": [], "manifest": {"tuya_a": entry},
              "guard": GUARD_ID}
    assert manager._stored_state({**stored, "confirm_threshold": 5}, GUARD_ID).confirm_threshold == 5
    for value in (None, "5", 0, True, 5.5):
        assert manager._stored_state({**stored, "confirm_threshold": value}, GUARD_ID).confirm_threshold is None
    assert manager._stored_state(stored, GUARD_ID).confirm_threshold is None


# The notice decision


@pytest.mark.parametrize("threshold", [2, 3])
async def test_a_device_past_the_threshold_gets_no_fine_notice(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, threshold: int):
    """The oldest open issue of the variant counts its author and one thumbs-up: 2. That reaches a threshold of 2, not
    one of 3. A newer issue of the variant, issues for another firmware or key and a pull request do not add to it."""
    prepare(repo_dir, threshold=threshold)
    device = radar(hass, zha)
    set_firmware(hass, device.device_id, FIRMWARE_A)
    aioclient_mock.get(const.SEARCH_URL, json=answer(
        hit(3, title(FIRMWARE_A), thumbs=1), hit(9, title(FIRMWARE_A), thumbs=0), hit(4, title(FIRMWARE_B), thumbs=50),
        hit(2, title(FIRMWARE_A, key="deadbeef"), thumbs=50), hit(1, title(FIRMWARE_A), thumbs=50, pull_request={"url": "x"}),
    ))
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    fine_values(device)
    await conclude(hass)
    if threshold == 2:
        assert [i for kind in checks.KINDS for i in issues_of(hass, kind)] == []
    else:
        assert notice_devices(hass, "self_check_fine") == [f"- {NAME} (\\_TZE200\\_aaaa TS0601)"]
    assert self_check(hass)["result"] == "fine"
    (url,) = searches(aioclient_mock)
    assert url.query["q"] == QUERY


@pytest.mark.parametrize("thumbs", [0, "1", True, 1.0, -1])
async def test_a_device_below_the_threshold_gets_the_notice_as_before(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, thumbs):
    """One person (or a count that is no count): the notice asks as before."""
    prepare(repo_dir)
    device = radar(hass, zha)
    set_firmware(hass, device.device_id, FIRMWARE_A)
    aioclient_mock.get(const.SEARCH_URL, json=answer(hit(3, title(FIRMWARE_A), thumbs=thumbs)))
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    fine_values(device)
    await conclude(hass)
    assert notice_devices(hass, "self_check_fine") == [f"- {NAME} (\\_TZE200\\_aaaa TS0601)"]
    (issue,) = issues_of(hass, "self_check_fine")
    assert issue.is_fixable
    assert issue.data == {"kind": "self_check_fine", "file": "z2m_tuya_a.py", "revision": self_check(hass)["revision"], "devices": [device.device_id]}
    assert len(searches(aioclient_mock)) == 1


async def test_a_huge_count_is_past_any_threshold(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    prepare(repo_dir, threshold=1000)
    device = radar(hass, zha)
    # Beyond 64 bits: Home Assistant's JSON writer cannot send it, Python's reader takes it.
    aioclient_mock.get(const.SEARCH_URL, text=json.dumps(answer(hit(2**31 - 1, title(None), thumbs=10**40))))
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    fine_values(device)
    await conclude(hass)
    assert issues_of(hass, "self_check_fine") == []


async def test_an_unproven_notice_leaves_out_a_device_past_the_threshold_too(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    prepare(repo_dir)
    radar(hass, zha)
    aioclient_mock.get(const.SEARCH_URL, json=answer(hit(5, title(None), thumbs=1)))
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    await conclude(hass)
    assert [i for kind in checks.KINDS for i in issues_of(hass, kind)] == []
    assert self_check(hass)["result"] == "unproven"


@pytest.mark.parametrize("confirmed", ["kitchen", "hall"])
async def test_the_kind_comes_from_the_devices_the_notice_names(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, confirmed: str):
    """The kitchen radar (firmware A) sends right values, the hall radar (firmware B) sends nothing. A device left out as
    confirmed does not decide the kind: the hall alone is unproven, the kitchen alone is fine."""
    prepare(repo_dir)
    kitchen, other = radar(hass, zha), hall(hass, zha)
    set_firmware(hass, kitchen.device_id, FIRMWARE_A)
    set_firmware(hass, other.device_id, FIRMWARE_B)
    aioclient_mock.get(const.SEARCH_URL, json=answer(hit(7, title(FIRMWARE_A if confirmed == "kitchen" else FIRMWARE_B), thumbs=1)))
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A, IEEE_B: TUYA_A})
    fine_values(kitchen)
    await conclude(hass)
    (issue,) = [i for kind in checks.KINDS for i in issues_of(hass, kind)]
    named = other if confirmed == "kitchen" else kitchen
    assert issue.translation_key == ("self_check_unproven" if confirmed == "kitchen" else "self_check_fine")
    assert issue.data["devices"] == [named.device_id]
    assert manager_of(hass).checks.observations["z2m_tuya_a.py"]["notice"] == issue.translation_key


async def test_looks_off_comes_past_the_threshold(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    prepare(repo_dir)
    device = radar(hass, zha)
    aioclient_mock.get(const.SEARCH_URL, json=answer(hit(5, title(None), thumbs=40)))
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    device.value("temperature", "3276.7", device_class="temperature", unit_of_measurement="°C")
    await conclude(hass)
    assert notice_devices(hass, "self_check_off") == [f"- {NAME} (\\_TZE200\\_aaaa TS0601)"]


async def test_only_the_device_below_the_threshold_is_named(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    prepare(repo_dir)
    kitchen, hallway = radar(hass, zha), hall(hass, zha)
    set_firmware(hass, kitchen.device_id, FIRMWARE_A)
    set_firmware(hass, hallway.device_id, FIRMWARE_B)
    aioclient_mock.get(const.SEARCH_URL, json=answer(hit(3, title(FIRMWARE_A), thumbs=1), hit(4, title(FIRMWARE_B), thumbs=0)))
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A, IEEE_B: TUYA_A})
    fine_values(kitchen)
    fine_values(hallway)
    await conclude(hass)
    assert notice_devices(hass, "self_check_fine") == [f"- {HALL} (\\_TZE200\\_aaaa TS0601)"]
    (issue,) = issues_of(hass, "self_check_fine")
    assert issue.data["devices"] == [hallway.device_id]
    assert len(searches(aioclient_mock)) == 1


async def test_no_notice_when_every_device_is_past_the_threshold(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    prepare(repo_dir)
    kitchen, hallway = radar(hass, zha), hall(hass, zha)
    set_firmware(hass, kitchen.device_id, FIRMWARE_A)
    set_firmware(hass, hallway.device_id, FIRMWARE_B)
    aioclient_mock.get(const.SEARCH_URL, json=answer(hit(3, title(FIRMWARE_A), thumbs=1), hit(4, title(FIRMWARE_B), thumbs=8)))
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A, IEEE_B: TUYA_A})
    fine_values(kitchen)
    await conclude(hass)
    assert [i for kind in checks.KINDS for i in issues_of(hass, kind)] == []


@pytest.mark.parametrize(("manufacturer", "firmware"), [
    ("_TZE200_a|b]c", None), ("_TZE200_aaaa", "0x0000_0046 [beta] | 2"), ("_TZE200_aaaa", "   "), ("_TZE200_aaaa", "v1\n2 ü"),
    ("_TZE200_aaaa", "x" * 80),
])
async def test_the_variant_is_compared_as_the_shared_rules_write_it(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, manufacturer, firmware):
    """Separators, whitespace, characters outside ASCII and an overlong firmware are normalized as the form link writes
    them: that title matches. A title with the raw text does not."""
    prepare(repo_dir, keyed(tuya_a={"fingerprints": [[manufacturer, "TS0601"]]}))
    tuya = CustomCluster(0xEF00, handled=(1, 2, 3))
    device = LiveDevice(hass, zha, IEEE_A, manufacturer, "TS0601", [("sensor", "temperature", tuya, "temperature", None, True)])
    set_firmware(hass, device.device_id, firmware)
    # The raw title is older and counts 1: taken for the variant, it would keep the notice.
    raw = f"Verify: Tuya A [{manufacturer} | TS0601 | {firmware} | {KEY_A}]"
    aioclient_mock.get(const.SEARCH_URL, json=answer(hit(2, raw, thumbs=0), hit(3, title(firmware, manufacturer=manufacturer), thumbs=1)))
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    await conclude(hass)
    assert self_check(hass)["result"] == "fine"
    assert issues_of(hass, "self_check_fine") == []
    fixed = manager_of(hass).checks.fixed_part(device.device_id, KEY_A)
    assert fixed == confirm.fixed_part(manufacturer, "TS0601", firmware, KEY_A) and not confirm.matches(raw, fixed)


async def test_the_variant_is_the_one_zigpy_reports_not_a_friendly_name(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """A quirk's friendly name changes the manufacturer and model of the device registry only. The variant is what the
    device reports, as the device check and its form name it."""
    prepare(repo_dir)
    tuya = CustomCluster(0xEF00, handled=(1, 2, 3))
    zha.zigpy[IEEE_A] = ("_TZE200_aaaa", "TS0601")
    device = LiveDevice(hass, zha, IEEE_A, "Sone", "SB1191", [("sensor", "temperature", tuya, "temperature", None, True)])
    aioclient_mock.get(const.SEARCH_URL, json=answer(hit(2, title(None, manufacturer="Sone", model="SB1191"), thumbs=30), hit(3, title(None), thumbs=0)))
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    await conclude(hass)
    assert len(notice_devices(hass, "self_check_fine")) == 1


async def test_firmware_none_is_unknown(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    prepare(repo_dir)
    device = radar(hass, zha)
    aioclient_mock.get(const.SEARCH_URL, json=answer(hit(3, "Verify: Tuya A [_TZE200_aaaa | TS0601 | unknown | 1a2b3c4d]", thumbs=1)))
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    fine_values(device)
    await conclude(hass)
    assert issues_of(hass, "self_check_fine") == []


UNKNOWN_ANSWERS = [
    {"status": 403, "headers": {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1795006270"}},
    {"status": 429},
    {"status": 500},
    {"exc": TimeoutError()},
    {"content": BIG + b" " * const.MAX_SEARCH},
    {"text": "not json"},
    {"json": []},
    {"json": {"items": {"0": "x"}}},
    # No match, and the answer may hold more than it shows.
    {"json": answer(hit(3, title(None, key="deadbeef"), thumbs=9), incomplete_results=True)},
    {"json": answer(hit(3, title(None), thumbs=-1))},
    {"json": answer(hit(3, title(None), thumbs=None))},
    {"json": answer(hit(3, title(None), state="weird", thumbs=5))},
]


@pytest.mark.parametrize("response", UNKNOWN_ANSWERS, ids=["403", "429", "500", "timeout", "too_large", "not_json", "list", "items_no_list",
                                                           "incomplete", "negative_count", "no_count", "odd_state"])
async def test_an_unknown_answer_keeps_the_notice_as_before(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, response):
    prepare(repo_dir)
    device = radar(hass, zha)
    aioclient_mock.get(const.SEARCH_URL, **response)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    fine_values(device)
    await conclude(hass)
    assert notice_devices(hass, "self_check_fine") == [f"- {NAME} (\\_TZE200\\_aaaa TS0601)"]
    # No second try.
    assert len(searches(aioclient_mock)) == 1


@pytest.mark.parametrize(("key", "threshold"), [
    ("1A2B3C4D", 2), ("1a2b3c4", 2), (None, 2), (12345678, 2), (KEY_A, ...), (KEY_A, 0), (KEY_A, "2"), (KEY_A, 2.0), (KEY_A, True), (KEY_A, 1001),
    (KEY_A, None),
], ids=["upper", "short", "none", "int", "no_threshold", "zero", "text", "float", "bool", "too_high", "null"])
async def test_without_a_valid_key_or_threshold_nothing_is_searched(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, key, threshold):
    prepare(repo_dir, keyed(tuya_a={"key": key}), threshold=threshold)
    device = radar(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    fine_values(device)
    await conclude(hass)
    assert notice_devices(hass, "self_check_fine") == [f"- {NAME} (\\_TZE200\\_aaaa TS0601)"]
    assert searches(aioclient_mock) == []


@pytest.mark.parametrize("values", ["fine", "nothing", "off"])
async def test_a_verified_file_gets_only_a_looks_off_notice(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, values: str):
    """Verified: nobody needs to test it, so neither "fine" nor "unproven" comes, and nothing is searched. "Looks off" comes."""
    prepare(repo_dir, keyed(tuya_a={"status": "verified"}))
    device = radar(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    if values == "fine":
        fine_values(device)
    elif values == "off":
        device.value("temperature", "3276.7", device_class="temperature", unit_of_measurement="°C")
    await conclude(hass)
    found = {kind: len(issues_of(hass, kind)) for kind in checks.KINDS}
    assert found == {checks.FINE: 0, checks.OFF: 1 if values == "off" else 0, checks.UNPROVEN: 0}
    assert self_check(hass)["result"] == {"fine": "fine", "nothing": "unproven", "off": "off"}[values]
    assert searches(aioclient_mock) == []


# The search while things change


async def test_a_second_count_while_the_search_runs_starts_no_second_one(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    prepare(repo_dir)
    device = radar(hass, zha)
    gate = Gate(answer(hit(3, title(None), thumbs=1)))
    aioclient_mock.get(const.SEARCH_URL, side_effect=gate)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    fine_values(device)
    await due(hass)
    await due(hass)
    await due(hass)
    assert gate.calls == 1 and self_check(hass)["result"] == "watching"
    await gate.open(hass)
    assert self_check(hass)["result"] == "fine" and issues_of(hass, "self_check_fine") == []
    await due(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert gate.calls == 1


async def test_searches_run_one_at_a_time(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """Two self-checks due at once: one search after the other. The second may meet a rate limit: it is not repeated."""
    prepare(repo_dir)
    kitchen, ours = radar(hass, zha), acme(hass, zha)

    def reply(url: URL) -> dict:
        return answer(hit(3, title(None), thumbs=0)) if KEY_A in url.query["q"] else answer()

    gate = Gate(reply)
    aioclient_mock.get(const.SEARCH_URL, side_effect=gate)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A, IEEE_C: ACME_C})
    fine_values(kitchen)
    ours.value("mode", "fast", options=["slow", "fast"])
    await due(hass)
    assert gate.calls == 1
    await gate.open(hass)
    assert gate.calls == 2 and gate.most == 1
    assert sorted(url.query["q"].rsplit(" ", 1)[1] for url in searches(aioclient_mock)) == sorted([KEY_A, KEY_C])
    assert {(i.data["file"]) for kind in checks.KINDS for i in issues_of(hass, kind)} == {"z2m_tuya_a.py", "z2m_acme_c.py"}


async def test_a_rate_limit_on_the_second_search_keeps_its_notice(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    prepare(repo_dir)
    kitchen, ours = radar(hass, zha), acme(hass, zha)
    calls = []

    async def reply(method, url, data):
        calls.append(url)
        if len(calls) == 1:
            return AiohttpClientMockResponse(method, URL(url), json=answer(hit(3, title(None), thumbs=5), hit(4, title(None, "Acme", "C-1", key=KEY_C), thumbs=5)))
        return AiohttpClientMockResponse(method, URL(url), status=403, headers={"X-RateLimit-Remaining": "0"})

    aioclient_mock.get(const.SEARCH_URL, side_effect=reply)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A, IEEE_C: ACME_C})
    fine_values(kitchen)
    ours.value("mode", "fast", options=["slow", "fast"])
    await conclude(hass)
    assert len(calls) == 2
    # The first file's device is past the threshold, the second answer is unknown: its notice comes as before.
    named = {i.data["file"] for kind in (checks.FINE, checks.UNPROVEN) for i in issues_of(hass, kind)}
    assert len(named) == 1


@pytest.mark.parametrize("thumbs", [1, 0])
async def test_a_variant_past_ten_other_issues_is_found_on_the_next_page(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, thumbs: int):
    """Ten open issues of other variants of the file fill the first page: the kitchen radar's variant (#57) is on the
    second. Past the threshold: no notice. Below it: the notice."""
    prepare(repo_dir)
    kitchen = radar(hass, zha)
    set_firmware(hass, kitchen.device_id, FIRMWARE_A)
    pages = Pages(aioclient_mock, answer(*others(range(1, 11)), total_count=11), answer(hit(57, title(FIRMWARE_A), thumbs=thumbs), total_count=11))
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    fine_values(kitchen)
    await conclude(hass)
    assert pages.asked == [1, 2]
    assert len(issues_of(hass, "self_check_fine")) == (0 if thumbs else 1)


async def test_the_pages_ask_for_every_device_of_the_self_check(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """The kitchen radar's variant is on the first page, the hall radar's on the second: both past the threshold."""
    prepare(repo_dir)
    kitchen, other = radar(hass, zha), hall(hass, zha)
    set_firmware(hass, kitchen.device_id, FIRMWARE_A)
    set_firmware(hass, other.device_id, FIRMWARE_B)
    pages = Pages(aioclient_mock, answer(*others(range(1, 10)), hit(10, title(FIRMWARE_A), thumbs=1), total_count=12),
                  answer(hit(57, title(FIRMWARE_B), thumbs=1), *others(range(58, 60)), total_count=12))
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A, IEEE_B: TUYA_A})
    fine_values(kitchen)
    fine_values(other)
    await conclude(hass)
    assert pages.asked == [1, 2]
    assert [i for kind in checks.KINDS for i in issues_of(hass, kind)] == []


@pytest.mark.parametrize("event", ["updated", "removed", "left_out"])
async def test_a_file_that_changes_while_the_search_runs_is_not_concluded(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, event: str):
    prepare(repo_dir)
    device = radar(hass, zha)
    gate = Gate(answer(hit(3, title(None), thumbs=0)))
    aioclient_mock.get(const.SEARCH_URL, side_effect=gate)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    old = self_check(hass)["revision"]
    fine_values(device)
    await due(hass)
    assert gate.calls == 1
    if event == "updated":
        prepare(repo_dir, version=2)
        REPOS[repo_dir].publish()
        await manager_of(hass).async_check()
        await apply_now(hass)
        zha.reload()
    elif event == "removed":
        zha.remove_device(device.device_id)
        await manager_of(hass).async_refresh()
        await apply_now(hass)
        zha.reload()
    else:
        hass.config_entries.async_update_entry(entry, options={const.CONF_EXCLUDED: [device.device_id]})
    await settle_now(hass)
    await gate.open(hass)
    assert [i for kind in checks.KINDS for i in issues_of(hass, kind)] == []
    if event == "updated":
        assert self_check(hass)["result"] == "watching" and self_check(hass)["revision"] != old
    else:
        assert self_check(hass) is None


async def test_a_device_that_leaves_while_the_search_runs_is_not_named(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    prepare(repo_dir)
    kitchen, hallway = radar(hass, zha), hall(hass, zha)
    gate = Gate(answer())
    aioclient_mock.get(const.SEARCH_URL, side_effect=gate)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A, IEEE_B: TUYA_A})
    fine_values(kitchen)
    fine_values(hallway)
    await due(hass)
    zha.remove_device(hallway.device_id)
    await settle_now(hass)
    await gate.open(hass)
    assert notice_devices(hass, "self_check_fine") == [f"- {NAME} (\\_TZE200\\_aaaa TS0601)"]
    (issue,) = issues_of(hass, "self_check_fine")
    assert issue.data["devices"] == [kitchen.device_id]


async def test_zha_down_while_the_search_runs_waits_for_the_next_count(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    prepare(repo_dir)
    device = radar(hass, zha)
    gate = Gate(answer())
    aioclient_mock.get(const.SEARCH_URL, side_effect=gate)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    fine_values(device)
    await due(hass)
    zha.start_reload()
    await settle_now(hass)
    await gate.open(hass)
    assert self_check(hass)["result"] == "watching" and issues_of(hass, "self_check_fine") == []
    zha.finish_reload()
    await settle(hass)
    await conclude(hass)
    assert gate.calls == 2
    assert len(issues_of(hass, "self_check_fine")) == 1


async def test_a_restart_during_the_search_searches_again_at_the_next_count(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    prepare(repo_dir)
    device = radar(hass, zha)
    gate = Gate(answer(hit(3, title(None), thumbs=1)))
    aioclient_mock.get(const.SEARCH_URL, side_effect=gate)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    fine_values(device)
    await due(hass)
    assert gate.calls == 1
    # The unload cancels the search: nothing of it is kept.
    await restart_with(hass, entry, repo_dir)
    assert self_check(hass)["result"] == "watching"
    await conclude(hass)
    assert gate.calls == 2
    assert self_check(hass)["result"] == "fine" and issues_of(hass, "self_check_fine") == []


async def apply_now(hass: HomeAssistant) -> None:
    """Confirm the visible change list without waiting for the background tasks (a search waits for its answer)."""
    flow = await start_fix(hass, issue_of(hass, "changes"))
    flow = await continue_fix(hass, flow, {"next_step_id": "apply"})
    assert flow["reason"] in ("done", "done_zha_not_loaded"), flow
    await hass.async_block_till_done()


async def settle_now(hass: HomeAssistant) -> None:
    """The refresh after a device registry change ran (without waiting for the background tasks)."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=const.REFRESH_COOLDOWN + 1))
    await hass.async_block_till_done()


# Notices of files that are verified now


@pytest.mark.parametrize("change", ["same_revision", "same_key", "other_key", "off"])
async def test_a_notice_goes_when_its_file_is_verified_now(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, change: str):
    """The next content state calls the file verified: its fine notice goes at once (the file differs only in its
    docstring, the change list updates it), without a search. A file verified with another key keeps it, so does a
    "looks off" notice."""
    prepare(repo_dir)
    device = radar(hass, zha)
    aioclient_mock.get(const.SEARCH_URL, json=answer())
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    if change == "off":
        device.value("temperature", "3276.7", device_class="temperature", unit_of_measurement="°C")
    else:
        fine_values(device)
    await conclude(hass)
    kind = checks.OFF if change == "off" else checks.FINE
    (issue,) = issues_of(hass, kind)
    before = len(searches(aioclient_mock))
    key = "deadbeef" if change == "other_key" else KEY_A
    prepare(repo_dir, keyed(tuya_a={"status": "verified", "key": key}), version=1 if change == "same_revision" else 2)
    REPOS[repo_dir].publish()
    await manager_of(hass).async_check()
    await hass.async_block_till_done(wait_background_tasks=True)
    gone = change in ("same_revision", "same_key")
    assert (ir.async_get(hass).async_get_issue(const.DOMAIN, issue.issue_id) is None) is gone
    # A refresh does not bring it back.
    await manager_of(hass).async_refresh()
    assert (ir.async_get(hass).async_get_issue(const.DOMAIN, issue.issue_id) is None) is gone
    assert len(searches(aioclient_mock)) == before


async def test_a_notice_of_a_verified_file_goes_at_load(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """A notice an earlier release made for a verified file goes when the integration loads, without a search."""
    prepare(repo_dir, keyed(tuya_a={"status": "verified"}))
    device = radar(hass, zha)
    aioclient_mock.get(const.SEARCH_URL, json=answer())
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    fine_values(device)
    with patch.object(checks.Checks, "file_verified", return_value=False):
        await conclude(hass)
    (issue,) = issues_of(hass, "self_check_fine")
    before = len(searches(aioclient_mock))
    await restart_with(hass, entry, repo_dir)
    assert ir.async_get(hass).async_get_issue(const.DOMAIN, issue.issue_id) is None
    assert len(searches(aioclient_mock)) == before


# The key of the revision installed

NEW_KEY = "0badf00d"


async def same_revision(hass: HomeAssistant, repo_dir: Path, key: object, threshold: object = 2) -> str:
    """The next content state ships z2m_tuya_a.py at the same revision with this key (a new key rule changes keys, not
    revisions), the self-check kept KEY_A at its start. The revision."""
    revision = self_check(hass)["revision"]
    prepare(repo_dir, keyed(tuya_a={"key": key}), threshold=threshold, version=1)
    REPOS[repo_dir].publish()
    await manager_of(hass).async_check()
    await hass.async_block_till_done(wait_background_tasks=True)
    source = manager_of(hass).source
    assert source.revisions["z2m_tuya_a.py"] == revision and source.keys.get("z2m_tuya_a.py") == (key if confirm.valid_key(key) else None)
    assert manager_of(hass).checks.observations["z2m_tuya_a.py"]["key"] == KEY_A
    return revision


@pytest.mark.parametrize("key", [NEW_KEY, None], ids=["new_key", "no_key"])
async def test_the_state_key_of_the_revision_installed_wins_over_the_kept_one(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, key):
    """The state in use states another key for the revision installed: the self-check searches with it (the key the
    integration sees for the file today). A state without a key for it leaves the kept one."""
    prepare(repo_dir)
    device = radar(hass, zha)
    aioclient_mock.get(const.SEARCH_URL, json=answer())
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    revision = await same_revision(hass, repo_dir, key)
    fine_values(device)
    await conclude(hass)
    wanted = key or KEY_A
    (url,) = searches(aioclient_mock)
    assert url.query["q"] == QUERY.replace(KEY_A, wanted)
    observation = manager_of(hass).checks.observations["z2m_tuya_a.py"]
    assert manager_of(hass).checks.file_key("z2m_tuya_a.py", observation) == wanted
    assert manager_of(hass).checks.check_key("z2m_tuya_a.py", revision) == wanted
