"""The one-time address behind the button of a device check's last page (links.py).

The button opens an address of this Home Assistant, built from the base the browser sends with the flow request
(HA-Frontend-Base). The new tab asks for it without a login: the view ends the device check's external step once and
sends the tab on to the GitHub link stored with the address (a page that goes there at once). The dialog, told by Home Assistant's
data_entry_flow_progressed event, asks for the flow and gets the end page (an abort with our text and Close). Without a
usable base the button opens GitHub directly, as before. Through Home Assistant's HTTP API as the frontend calls it.
"""

from __future__ import annotations

import asyncio
import html
import importlib
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import EVENT_DATA_ENTRY_FLOW_PROGRESSED, UnknownFlow
from homeassistant.helpers.http import current_request
from homeassistant.setup import async_setup_component
from multidict import CIMultiDict

from . import integration
from .test_checks import link_fields
from .test_confirm_search import FIRMWARE_A, answer, hit, title
from .test_self_check_flow import (
    CHECK,
    FINE,
    ISSUE_LINK,
    answers,
    checks,
    device_checks,
    gone,
    issue_of,
    notice,
    still_open,
)

links = importlib.import_module(f"{integration.PACKAGE}.links")
const = integration.const
BASE = "http://homeassistant.local:8123"
HEADERS = {"HA-Frontend-Base": BASE}
FLOWS = "/api/config/config_entries/subentries/flow"
GITHUB = f"https://github.com/{const.REPOSITORY}/issues/new?"


@pytest.fixture
def fast_check():
    with patch.object(checks, "CHECK_WAIT", 0.3), patch.object(checks, "CHECK_QUIET", 0.05), patch.object(checks, "CHECK_STEP", 0.05):
        yield


class Browser:
    """The frontend's calls: the dialog's (with the admin's token and the base header) and the new tab's (no login)."""

    def __init__(self, hass: HomeAssistant, client, tab, headers: dict | None = None) -> None:
        self.hass, self.client, self.tab = hass, client, tab
        self.headers = HEADERS if headers is None else headers

    async def start(self) -> dict:
        entry = self.hass.config_entries.async_entries(const.DOMAIN)[0]
        return await (await self.client.post(FLOWS, json={"handler": [entry.entry_id, "device_check"]}, headers=self.headers)).json()

    async def post(self, flow: dict, data: dict) -> dict:
        return await (await self.client.post(f"{FLOWS}/{flow['flow_id']}", json=data, headers=self.headers)).json()

    async def get(self, flow: dict) -> tuple[int, dict]:
        response = await self.client.get(f"{FLOWS}/{flow['flow_id']}", headers=self.headers)
        return response.status, await response.json()

    async def open(self, url: str, path_only: bool = True):
        """The new tab: GET of the address without a login, no redirect followed. Its page in .page."""
        response = await self.tab.get(url.removeprefix(BASE) if path_only else url, allow_redirects=False)
        response.page = await response.text()
        return response


async def last_page(hass: HomeAssistant, browser: Browser, device, *, wrong: bool = False) -> dict:
    """The button's device check of the notice's device through the frontend's calls up to its last page."""
    flow = await browser.start()
    assert flow["step_id"] == "check", flow
    await browser.post(flow, {"device": device.device_id})
    await hass.async_block_till_done(wait_background_tasks=True)
    _status, flow = await browser.get(flow)
    assert flow["step_id"] == "values", flow
    if wrong:
        await browser.post(flow, {"next_step_id": "mismatch"})
        flow = await browser.post(flow, {"matches": ["1-humidity"], "wrong": ["1-temperature"]})
    else:
        flow = await browser.post(flow, {"next_step_id": "all_match"})
    assert flow["type"] == "form" and flow["data_schema"] == [], flow
    return flow


@pytest.fixture
async def setup(hass: HomeAssistant, zha, repo_dir, aioclient_mock, hass_client, hass_client_no_auth, fast_check):
    """A notice for the kitchen radar, the config API (the frontend's flow calls), the dialog's client and the tab's."""
    assert await async_setup_component(hass, "config", {})
    (device,), search = await notice(hass, zha, repo_dir, aioclient_mock)
    answers(device)
    return SimpleNamespace(device=device, search=search, browser=Browser(hass, await hass_client(), await hass_client_no_auth()))


def target_of(response) -> str:
    """Where the tab's page goes on to: its refresh and its link name the same address. Nothing in the response head."""
    assert response.status == 200 and response.content_type == "text/html" and response.headers["Cache-Control"] == "no-store"
    assert "Location" not in response.headers
    return _target(response.page)


def _target(page: str) -> str:
    (refresh,) = re.findall(r'<meta http-equiv="refresh" content="0;url=([^"]*)">', page)
    (link,) = re.findall(r'<a href="([^"]*)">', page)
    assert refresh == link
    return html.unescape(link)


def progressed(hass: HomeAssistant) -> list[dict]:
    """The data_entry_flow_progressed events the dialog listens to."""
    events: list[dict] = []
    hass.bus.async_listen(EVENT_DATA_ENTRY_FLOW_PROGRESSED, lambda event: events.append(dict(event.data)))
    return events


# The way through


@pytest.mark.parametrize("step", ["report", "report_wrong"])
async def test_the_tab_ends_the_external_step_and_lands_on_the_form(hass: HomeAssistant, setup, step: str):
    """The button: the notice goes, the external step names a one-time address of the browser's base. The tab asks for it
    without a login: a page on to the prefilled form, the step ends once (one progressed event for the dialog), the dialog's
    fetch gets the end page (an abort with our text, the flow is gone). The address works only once."""
    browser = setup.browser
    flow = await last_page(hass, browser, setup.device, wrong=step == "report_wrong")
    assert flow["step_id"] == step and still_open(hass)
    opened = await browser.post(flow, {})
    assert opened["type"] == "external" and opened["step_id"] == "open", opened
    assert opened["url"].startswith(f"{BASE}/api/xquirks/open/") and len(opened["url"].rsplit("/", 1)[1]) >= 40
    assert gone(hass)
    events = progressed(hass)
    response = await browser.open(opened["url"])
    target = target_of(response)
    # The page that stays without a tab also carries the link itself (a browser whose new tab cannot reach this address).
    assert opened["description_placeholders"] == {"link": target}
    assert target.startswith(GITHUB) and link_fields(target)["template"] == (integration.text.PROBLEM_FORM if step == "report_wrong" else integration.text.VERIFY_FORM)
    await hass.async_block_till_done()
    assert events == [{"handler": opened["handler"] if isinstance(opened["handler"], tuple) else tuple(opened["handler"]), "flow_id": opened["flow_id"], "refresh": True}]
    status, end = await browser.get(flow)
    assert status == 200 and end["type"] == "abort" and end["reason"] == "opened", end
    assert CHECK["abort"]["opened"] == "Thank you! The form is open in a new tab."
    assert (await browser.get(flow))[0] == 404 and device_checks(hass) == []
    again = await browser.open(opened["url"])
    assert again.status == 404 and "Location" not in again.headers and opened["url"].rsplit("/", 1)[1] not in again.page


async def test_the_thumbs_up_tab_lands_on_the_device_test(hass: HomeAssistant, setup):
    browser = setup.browser
    setup.search.reply = answer(hit(42, title(FIRMWARE_A), thumbs=1))
    first = await (await browser.client.post("/api/repairs/issues/fix", json={"handler": const.DOMAIN, "issue_id": issue_of(hass, FINE).issue_id})).json()
    handed = await (await browser.client.post(f"/api/repairs/issues/fix/{first['flow_id']}", json={"next_step_id": "check"})).json()
    assert handed["reason"] == "check_started", handed
    flow = {"flow_id": handed["next_flow"][1]}
    await hass.async_block_till_done(wait_background_tasks=True)
    _status, values = await browser.get(flow)
    page = await browser.post(values, {"next_step_id": "all_match"})
    assert page["step_id"] == "thumbs_up", page
    opened = await browser.post(page, {})
    assert opened["step_id"] == "open_test" and opened["url"].startswith(f"{BASE}/api/xquirks/open/")
    response = await browser.open(opened["url"])
    assert target_of(response) == ISSUE_LINK.format(number=42) == opened["description_placeholders"]["link"]
    status, end = await browser.get(flow)
    assert end["type"] == "abort" and end["reason"] == "opened_test" and "thumbs-up" in CHECK["abort"]["opened_test"]
    assert gone(hass)


async def test_the_handed_over_check_ends_the_same_way(hass: HomeAssistant, setup):
    """The notice's "Check the device now" hands over (the dialog opened with continueFlowId fetches the flow): the same
    address, the same end page, the notice goes with the button."""
    browser = setup.browser
    first = await (await browser.client.post("/api/repairs/issues/fix", json={"handler": const.DOMAIN, "issue_id": issue_of(hass, FINE).issue_id})).json()
    handed = await (await browser.client.post(f"/api/repairs/issues/fix/{first['flow_id']}", json={"next_step_id": "check"})).json()
    flow = {"flow_id": handed["next_flow"][1]}
    await hass.async_block_till_done(wait_background_tasks=True)
    _status, values = await browser.get(flow)
    page = await browser.post(values, {"next_step_id": "all_match"})
    assert page["step_id"] == "report" and still_open(hass)
    opened = await browser.post(page, {})
    assert gone(hass)
    assert target_of(await browser.open(opened["url"])).startswith(GITHUB)
    assert (await browser.get(flow))[1]["reason"] == "opened"


# Without the tab, or a tab too late


async def test_until_the_tab_asks_the_external_step_stays(hass: HomeAssistant, setup):
    """Asked for again, clicked again or sent anything: the same external step with the same address. No request of the
    dialog ends it, the view's signal cannot come over HTTP."""
    browser = setup.browser
    flow = await last_page(hass, browser, setup.device)
    opened = await browser.post(flow, {})
    for data in ({}, {"anything": 1}, {"next_step_id": "done"}, {"opened": True}):
        again = await browser.post(flow, data)
        assert again["type"] == "external" and again["url"] == opened["url"], again
    status, again = await browser.get(flow)
    assert status == 200 and again["type"] == "external" and again["url"] == opened["url"]
    assert again["description_placeholders"] == opened["description_placeholders"] and again["description_placeholders"]["link"].startswith(GITHUB)
    assert target_of(await browser.open(opened["url"])).startswith(GITHUB)
    assert (await browser.get(flow))[1]["reason"] == "opened"


async def test_a_closed_dialog_drops_its_address(hass: HomeAssistant, setup):
    """The dialog's X before the tab asked (the flow is gone): its address works no more, other checks keep theirs."""
    browser = setup.browser
    other = await browser.post(await last_page(hass, browser, setup.device), {})
    flow = await last_page(hass, browser, setup.device)
    opened = await browser.post(flow, {})
    assert (await browser.client.delete(f"{FLOWS}/{flow['flow_id']}")).status == 200
    response = await browser.open(opened["url"])
    assert response.status == 404 and "Location" not in response.headers
    assert [found[0] for found in hass.data[links.DATA_LINKS].waiting.values()] == [other["flow_id"]]
    assert target_of(await browser.open(other["url"])).startswith(GITHUB)


async def test_the_flow_gone_between_take_and_end_still_sends_the_tab(hass: HomeAssistant, setup):
    """The flow removed while the view ends it (a restart, a race): the tab still gets its form, nothing fails."""
    browser = setup.browser
    flow = await last_page(hass, browser, setup.device)
    opened = await browser.post(flow, {})
    with patch.object(hass.config_entries.subentries, "async_configure", side_effect=UnknownFlow):
        response = await browser.open(opened["url"])
    assert target_of(response).startswith(GITHUB)


async def test_an_expired_address_ends_nothing(hass: HomeAssistant, setup):
    """Past its TTL the address gets the short text: no page on to GitHub, the external step stays (the dialog's Open website
    then leads to the text too; X and a new check remain)."""
    browser = setup.browser
    flow = await last_page(hass, browser, setup.device)
    opened = await browser.post(flow, {})
    store = hass.data[links.DATA_LINKS]
    now = store.clock()
    store.clock = lambda: now + links.TTL + 1
    response = await browser.open(opened["url"])
    assert response.status == 404 and "Location" not in response.headers
    status, again = await browser.get(flow)
    assert again["type"] == "external"


async def test_two_tabs_at_once_end_the_step_once(hass: HomeAssistant, setup):
    """Two requests for the address at the same moment (the tab and Open website): one page on, one short text, the step
    ends exactly once."""
    browser = setup.browser
    flow = await last_page(hass, browser, setup.device)
    opened = await browser.post(flow, {})
    configure = hass.config_entries.subentries.async_configure
    with patch.object(hass.config_entries.subentries, "async_configure", side_effect=configure) as ending:
        first, second = await asyncio.gather(browser.open(opened["url"]), browser.open(opened["url"]))
    assert sorted([first.status, second.status]) == [200, 404]
    assert ending.call_count == 1 and ending.call_args.args[1] is links.OPENED
    assert (await browser.get(flow))[1]["reason"] == "opened"


# What the tab can send


@pytest.mark.parametrize("path", ["/api/xquirks/open/x", "/api/xquirks/open/" + "A" * 43, "/api/xquirks/open/..%2F..%2Fapi%2Fconfig",
                                  "/api/xquirks/open/%00", "/api/xquirks/open/" + "x" * 5000, "/api/xquirks/open/%E2%82%AC"])
async def test_an_unknown_address_gets_the_short_text(hass: HomeAssistant, setup, path: str):
    browser = setup.browser
    flow = await last_page(hass, browser, setup.device)
    opened = await browser.post(flow, {})
    response = await browser.tab.get(path, allow_redirects=False)
    assert response.status in (404, 414, 400) and "Location" not in response.headers
    # The real address still works.
    assert target_of(await browser.open(opened["url"])).startswith(GITHUB)


async def test_the_address_sends_only_to_its_own_link(hass: HomeAssistant, setup):
    """Query, fragment or headers of the tab's request change nothing: the target is the stored link."""
    browser = setup.browser
    flow = await last_page(hass, browser, setup.device)
    opened = await browser.post(flow, {})
    response = await browser.tab.get(opened["url"].removeprefix(BASE) + "?to=https://evil.example&next=//evil.example", allow_redirects=False,
                                     headers={"Referer": "https://evil.example", "Host": "evil.example", "HA-Frontend-Base": "https://evil.example"})
    response.page = await response.text()
    assert target_of(response).startswith(GITHUB) and "evil" not in response.page


async def test_a_long_form_travels_in_the_page_not_in_the_head(hass: HomeAssistant, setup):
    """The longest link a check makes (REPORT_LIMIT) leaves the response head small: a reverse proxy reads the head into
    a small buffer (nginx: 4 KB by default). The page escapes what it carries."""
    browser = setup.browser
    flow = await last_page(hass, browser, setup.device)
    await browser.post(flow, {})
    target = GITHUB + "template=verify.yml&title=a%22b&report=" + "x" * integration.text.REPORT_LIMIT + '&q="><script>&r=1'
    token = hass.data[links.DATA_LINKS].add("gone", target)
    response = await browser.open(f"/api/xquirks/open/{token}")
    assert target_of(response) == target and "<script>" not in response.page
    assert sum(len(name) + len(value) + 4 for name, value in response.raw_headers) < 1024


# The base address


@pytest.mark.parametrize(("headers", "base"), [
    ({"HA-Frontend-Base": BASE}, BASE),
    ({"Origin": "https://ha.example.org"}, None),
    ({"HA-Frontend-Base": "https://a.example:8443", "Origin": "https://b.example"}, "https://a.example:8443"),
    ({"HA-Frontend-Base": "javascript:alert(1)", "Origin": "https://b.example"}, None),
    ({"HA-Frontend-Base": "ftp://a.example"}, None),
    ({"HA-Frontend-Base": "https://a.example/lovelace"}, None),
    ({"HA-Frontend-Base": "https://user:pw@a.example"}, None),
    ({"HA-Frontend-Base": "https://a.example?x=1"}, None),
    ({"HA-Frontend-Base": "https://a.example#x"}, None),
    ({"HA-Frontend-Base": "//a.example"}, None),
    ({"Origin": "null"}, None),
    ({"HA-Frontend-Base": "", "Origin": ""}, None),
    ({"HA-Frontend-Base": "http://[::1]:8123"}, "http://[::1]:8123"),
    ({"HA-Frontend-Base": "https://a.example/"}, "https://a.example"),
    ({"HA-Frontend-Base": "http://a b"}, None),
    ({"HA-Frontend-Base": "http://a<b>.example"}, None),
    ({"HA-Frontend-Base": "https://a.example:99999"}, None),
    ({"HA-Frontend-Base": "http://home_assistant:8123"}, "http://home_assistant:8123"),
    ({"HA-Frontend-Base": "https://b\u00fccher.example"}, "https://xn--bcher-kva.example"),
    ({"HA-Frontend-Base": "http://192.0.2.10:8123"}, "http://192.0.2.10:8123"),
    ({}, None),
], ids=["base", "origin_alone", "base_wins", "bad_base_not_origin", "ftp", "path", "userinfo", "query", "fragment", "no_scheme", "origin_null", "empty",
        "ipv6", "slash", "space", "brackets", "bad_port", "underscore", "idn", "ipv4", "none"])
def test_the_base_comes_from_the_browser_headers(headers: dict, base: str | None):
    token = current_request.set(SimpleNamespace(headers=CIMultiDict(headers)))
    try:
        assert links.frontend_base() == base
    finally:
        current_request.reset(token)


def test_without_a_request_there_is_no_base():
    assert current_request.get() is None
    assert links.frontend_base() is None


@pytest.mark.parametrize("headers", [{}, {"HA-Frontend-Base": "ftp://a.example"}, {"Origin": BASE}], ids=["none", "bad_scheme", "origin_alone"])
async def test_without_a_base_the_button_opens_github_directly(hass: HomeAssistant, setup, headers: dict):
    """Today's behaviour: the external step names the GitHub link, no address is made, the step stays until X."""
    browser = Browser(hass, setup.browser.client, setup.browser.tab, headers)
    flow = await last_page(hass, browser, setup.device)
    opened = await browser.post(flow, {})
    assert opened["type"] == "external" and opened["url"].startswith(GITHUB)
    assert links.DATA_LINKS not in hass.data or not hass.data[links.DATA_LINKS].waiting
    assert gone(hass)
    assert (await browser.get(flow))[1]["type"] == "external"


# The store


def test_an_address_works_once_and_within_its_ttl():
    now = [100.0]
    store = links.Links(clock=lambda: now[0])
    token = store.add("flow", "https://github.com/x")
    assert store.take("other") is None
    assert store.take(token) == ("flow", "https://github.com/x") and store.take(token) is None
    late = store.add("flow", "https://github.com/y")
    now[0] += links.TTL
    assert store.take(late) is None
    tokens = [store.add(f"flow{n}", "https://github.com/z") for n in range(links.MAX_LINKS + 5)]
    assert len(store.waiting) == links.MAX_LINKS and store.take(tokens[0]) is None and store.take(tokens[-1]) == (f"flow{links.MAX_LINKS + 4}", "https://github.com/z")
    # Expired addresses go when the next one is made.
    now[0] += links.TTL + 1
    store.add("flow", "https://github.com/w")
    assert len(store.waiting) == 1


def test_dropping_a_flow_drops_only_its_addresses():
    store = links.Links()
    first, second, third = store.add("a", "https://github.com/1"), store.add("b", "https://github.com/2"), store.add("a", "https://github.com/3")
    store.drop("a")
    assert store.take(first) is None and store.take(third) is None and store.take(second) == ("b", "https://github.com/2")
    store.drop("unknown")


def test_an_address_works_for_an_hour_at_most():
    assert links.TTL == 3600.0
