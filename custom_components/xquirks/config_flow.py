"""Config flow: one entry, no settings needed. Options: the devices xquirks leaves alone and whether it offers high-risk
files that are not verified on a device.

The device check is a subentry flow of the entry ("device_check", DeviceCheckFlow). It never adds a subentry: it always
ends with an abort. It starts from the button on the integration page (source "user") or from the fix flow of a
self-check notice (source "notice", repairs.SelfCheckFlow)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

import probatio as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers.selector import (
    BooleanSelector,
    DeviceSelector,
    DeviceSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from homeassistant.util.hass_dict import HassKey

from . import confirm, links, text
from .checks import CheckResult, Checks
from .const import CONF_EXCLUDED, CONF_HIGH_RISK, DOMAIN, TITLE, ZHA_DOMAIN
from .manager import zha_folder

# The subentry type of the device check and the source of a check a notice hands over.
DEVICE_CHECK = "device_check"
SOURCE_NOTICE = "notice"
# The fields of the device check.
DEVICE = "device"
MATCHES = "matches"
WRONG = "wrong"
# The external steps after the button: the form, or the open device test of the variant (a thumbs-up). The end page
# after each once the tab opened (an abort reason).
OPEN_FORM = "open"
OPEN_TEST = "open_test"
OPENED = {OPEN_FORM: "opened", OPEN_TEST: "opened_test"}
# The device check a notice handed over last (hass.data): issue id -> flow id.
HANDED: HassKey[dict[str, str]] = HassKey("xquirks_handed_checks")


class QuirksConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # manifest.json has single_config_entry: Home Assistant aborts a second flow itself.
        if user_input is not None:
            return self.async_create_entry(title=TITLE, data={})
        return self.async_show_form(
            step_id="user",
            description_placeholders={"folder": zha_folder(self.hass) or "not configured"},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> QuirksOptionsFlow:
        return QuirksOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        # Home Assistant shows a button on the integration page and an item in its three-dot menu for it.
        return {DEVICE_CHECK: DeviceCheckFlow}


class DeviceCheckFlow(ConfigSubentryFlow):
    """The device check: which device ("check", asked without it for the one device of a notice), asking it with a
    progress dialog ("ask"), its values with "All values match" (a thumbs-up on the open issue of the device's variant,
    else the filled-in form) or "Something does not match" (the user marks what matches the device and what does not,
    then the report). Only a check from a notice has the answer of the notice's confirmation search: one from the button
    never ends with a thumbs-up.

    The last page (the report "report", the problem report "report_wrong" or the thumbs-up) keeps the report when it shows
    (Checks.finish_check). It is a page without fields whose one button opens its link: Home Assistant shows Close on an
    abort page and has no link button. The button answers with an external step: the frontend opens its address in a new
    tab and shows our text with the address as a button in case the browser blocked that, and the GitHub link itself in
    case a new tab cannot reach this Home Assistant. The address is a one-time one of this Home Assistant (links.py): when
    the tab asks for it, the view ends the external step ("done") and sends the tab to GitHub; the dialog then shows the
    end page (an abort with our text and Close). Nothing else ends that step: every other call gets it again, until the
    flow is deleted. The dialog's X deletes a check from the button. The frontend does not delete a check a notice handed
    over (only flows it started itself): the notice's next handover ends it (HANDED). Without the browser's base address
    the button opens GitHub directly and the page stays until X. Only the button closes the notices the check answers
    (_clicked, Checks.close_checked): the X on any page leaves them.

    A check from a notice offers only the notice's devices and ends on any step once the notice no longer stands as it was
    made (_stale): closed in another window (no second form for it), its file changed or its device left."""

    def __init__(self) -> None:
        super().__init__()
        self._device: str | None = None
        self._task: asyncio.Task[CheckResult | str] | None = None
        self._result: CheckResult | str | None = None
        # A check from a notice: the notice's issue id and data, and the answer of the first page's search (None: no
        # search, or it failed).
        self._notice: tuple[str, dict[str, Any]] | None = None
        self._answer: object = None
        # The last page as shown (step id and placeholders), the GitHub link of its button (None before it showed), the
        # self-checks the check answered (Checks.finish_check: their notices go with the button), the external step the
        # button opened and the address it opens (None before the click).
        self._last: tuple[str, dict[str, str]] | None = None
        self._link: str | None = None
        self._marked: frozenset[tuple[object, ...]] = frozenset()
        self._opened: str | None = None
        self._address: str | None = None

    @callback
    def async_remove(self) -> None:
        """The flow ended or was deleted: its one-time address works no more."""
        super().async_remove()
        links.async_drop(self.hass, self.flow_id)
        handed = self.hass.data.get(HANDED, {})
        if self._notice is not None and handed.get(self._notice[0]) == self.flow_id:
            del handed[self._notice[0]]

    # The two ways in

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """The button on the integration page: which device, of every device that runs an xquirks file."""
        return await self._start()

    async def async_step_notice(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Handed over by the fix flow of a self-check notice ("Check the device now"): the notice's issue id and data, and
        the answer of its first page's search. Only Home Assistant's own code starts a flow with this source (the
        frontend starts one with "user")."""
        found = user_input if isinstance(user_input, Mapping) else {}
        issue_id, data = found.get("issue_id"), found.get("data")
        if not isinstance(issue_id, str) or not isinstance(data, Mapping):
            return self.async_abort(reason="gone")
        handed = self.hass.data.setdefault(HANDED, {})
        if (before := handed.get(issue_id)) is not None:
            with suppress(UnknownFlow):
                self.hass.config_entries.subentries.async_abort(before)
        handed[issue_id] = self.flow_id
        self._notice, self._answer = (issue_id, dict(data)), found.get("answer")
        return await self._start()

    async def _start(self) -> SubentryFlowResult:
        """The first page: the device picker. The one device of a notice is asked without it."""
        if not isinstance(candidates := self._offered(), list):
            return candidates
        if self._notice is not None and len(candidates) == 1:
            self._device = candidates[0][0]
            return await self.async_step_ask()
        return self._picker(candidates)

    # The device check

    def _checks(self) -> Checks | None:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        return entry.runtime_data.checks if entry is not None and entry.state is ConfigEntryState.LOADED else None

    def _notice_devices(self, checks: Checks) -> list[str]:
        """The notice's devices the device check can ask now."""
        assert self._notice is not None
        return checks.notice_devices(self._notice[1].get("file"), self._notice[1].get("devices"))

    def _candidates(self, checks: Checks) -> list[tuple[str, str]]:
        """The devices to pick from: those that run a file of ours and are not left out, a notice's devices only."""
        candidates = checks.candidates()
        if self._notice is None:
            return candidates
        devices = set(self._notice_devices(checks))
        return [(device_id, label) for device_id, label in candidates if device_id in devices]

    def _stale(self, checks: Checks) -> SubentryFlowResult | None:
        """The abort of a step whose check went stale (None: it goes on). Only a check from a notice goes stale: the notice
        no longer stands as it was made, the device ran another revision (a change of the file waits for a ZHA reload:
        none) or it left."""
        if self._notice is None:
            return None
        if (found := checks.open_notice(*self._notice))[0] is None:
            return self.async_abort(reason=found[1])
        result = self._result
        if isinstance(result, CheckResult):
            if result.file != self._notice[1].get("file") or result.revision != self._notice[1].get("revision"):
                return self.async_abort(reason="file_changed")
            if result.device_id not in self._notice_devices(checks):
                return self.async_abort(reason="device_gone")
        return None

    def _offered(self) -> list[tuple[str, str]] | SubentryFlowResult:
        """The devices the picker offers, or the abort when there is nothing to check."""
        if (checks := self._checks()) is None:
            return self.async_abort(reason="not_loaded")
        if not self.hass.config_entries.async_loaded_entries(ZHA_DOMAIN):
            return self.async_abort(reason="zha_not_loaded")
        if (stale := self._stale(checks)) is not None:
            return stale
        if not (candidates := self._candidates(checks)):
            return self.async_abort(reason="no_device" if self._notice is not None else "nothing_to_check")
        return candidates

    def _picker(self, candidates: list[tuple[str, str]]) -> SubentryFlowResult:
        options = [SelectOptionDict(value=device_id, label=label) for device_id, label in candidates]
        schema = vol.Schema({vol.Required(DEVICE, default=candidates[0][0]): SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN))})
        return self.async_show_form(step_id="check", data_schema=schema)

    async def async_step_check(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Which device. Asked for the page again (no input), it shows the picker again, also for one device left."""
        if not isinstance(candidates := self._offered(), list):
            return candidates
        if user_input is not None and user_input.get(DEVICE) in dict(candidates):
            self._device = user_input[DEVICE]
            return await self.async_step_ask()
        return self._picker(candidates)

    async def async_step_ask(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Ask the device, wait for its values (a progress dialog). Asked for the page again, or the picker sent again, it
        waits for the same query: one query per device check."""
        first = self._task is None
        if first:
            if (checks := self._checks()) is None or self._device is None:
                return self.async_abort(reason="not_loaded")
            self._task = self.hass.async_create_task(checks.async_run_check(self._device), "xquirks device check")
        # Once the progress dialog shows, Home Assistant takes only progress or its end: the values step aborts when the
        # entry is gone meanwhile.
        if not self._task.done():
            return self.async_show_progress(step_id="ask", progress_action="asking", progress_task=self._task)
        self._result, self._task = self._task.result(), None
        if first:
            # Done before any progress dialog showed (it sent nothing): straight to the values, without the picker's
            # input.
            return await self.async_step_values()
        return self.async_show_progress_done(next_step_id="values")

    def _checked(self) -> tuple[Checks, CheckResult] | SubentryFlowResult:
        """The checks and the result of the device check, or the abort when there is none to show."""
        result = self._result
        if isinstance(result, str) or result is None:
            return self.async_abort(reason=result or "check_failed")
        if not result.rows:
            return self.async_abort(reason="no_entities")
        if (checks := self._checks()) is None:
            return self.async_abort(reason="not_loaded")
        if (stale := self._stale(checks)) is not None:
            return stale
        return checks, result

    @staticmethod
    def _shown(result: CheckResult) -> dict[str, str]:
        """What a page of the results shows: the device, the file and the table of its values."""
        return {"device": text.plain(result.name), "file": result.file, "revision": result.revision or "unknown", "entities": text.check_dialog(result)}

    async def async_step_values(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """The values of the device check with "All values match" and "Something does not match"."""
        if not isinstance(checked := self._checked(), tuple):
            return checked
        result = checked[1]
        step = "values" if result.answered is not False else "values_no_answer_mains" if result.mains else "values_no_answer"
        return self.async_show_menu(step_id=step, menu_options=["all_match", "mismatch"], description_placeholders=self._shown(result))

    # The values page when the device did not answer (Home Assistant wants a step for each page it shows). A mains device
    # does not sleep: it cannot be reached. Any other may sleep: wake it.
    async def async_step_values_no_answer(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self.async_step_values()

    async def async_step_values_no_answer_mains(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self.async_step_values()

    async def async_step_mismatch(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self.async_step_results()

    async def async_step_results(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """The list after "Something does not match": the user marks what matches the device and what does not. It ends
        with the report (_finish), never a thumbs-up. Every value marked matching and none wrong is "All values match"."""
        if not isinstance(checked := self._checked(), tuple):
            return checked
        checks, result = checked
        errors: dict[str, str] = {}
        if user_input is not None:
            matches, wrong = set(user_input.get(MATCHES, [])), set(user_input.get(WRONG, []))
            if not wrong and matches >= {row.suffix for row in result.rows}:
                return await self.async_step_all_match()
            if matches & wrong:
                errors["base"] = "both"
            elif not matches and not wrong:
                errors["base"] = "nothing"
            else:
                return self._finish(checks, result, matches, wrong)
        options = [SelectOptionDict(value=row.suffix, label=f"{row.name}: {row.value or 'no value'}") for row in result.rows]
        choice = SelectSelector(SelectSelectorConfig(options=options, multiple=True, mode=SelectSelectorMode.LIST))
        schema = vol.Schema({vol.Optional(MATCHES, default=list((user_input or {}).get(MATCHES, []))): choice,
                             vol.Optional(WRONG, default=list((user_input or {}).get(WRONG, []))): choice})
        return self.async_show_form(step_id="results", data_schema=schema, errors=errors, description_placeholders=self._shown(result))

    async def async_step_all_match(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """"All values match": a thumbs-up on the open issue of the device's variant when there is one, else the form."""
        if not isinstance(checked := self._checked(), tuple):
            return checked
        checks, result = checked
        matches = {row.suffix for row in result.rows}
        found = self._issue(checks, result)
        if found is None:
            return self._finish(checks, result, matches, set())
        _report, _url, answered = checks.finish_check(result, matches, set())
        number, count, threshold = found
        return self._show_last("thumbs_up", {"count": str(count), "threshold": str(threshold)}, text.issue_url(number), answered)

    def _issue(self, checks: Checks, result: CheckResult) -> tuple[int, int, int] | None:
        """The oldest open issue of the checked device's variant in the first page's answer (the fixed part the form's
        title ends with): its number, its count and the threshold. None without one or when the answer cannot tell."""
        threshold = confirm.valid_threshold(checks.manager.source.confirm_threshold)
        key = checks.check_key(result.file, result.revision)
        if threshold is None or key is None:
            return None
        found = confirm.confirmation(self._answer, confirm.fixed_part(result.manufacturer, result.model, result.firmware, key))
        if found.state != confirm.FOUND or found.number is None or found.count is None:
            return None
        return found.number, found.count, threshold

    def _finish(self, checks: Checks, result: CheckResult, matches: set[str], wrong: set[str]) -> SubentryFlowResult:
        """The report: the counts, the button opens the form it goes to."""
        _report, url, answered = checks.finish_check(result, matches, wrong)
        unchecked = str(len(result.rows) - len(matches) - len(wrong))
        if wrong:
            return self._show_last("report_wrong", {"wrong": str(len(wrong)), "matches": str(len(matches)), "unchecked": unchecked}, url, answered)
        return self._show_last("report", {"matches": str(len(matches)), "unchecked": unchecked}, url, answered)

    def _show_last(self, step_id: str, placeholders: dict[str, str], link: str, answered: frozenset[tuple[object, ...]]) -> SubentryFlowResult:
        self._last, self._link, self._marked = (step_id, placeholders), link, answered
        return self.async_show_form(step_id=step_id, data_schema=None, description_placeholders=placeholders)

    # The last page and its button

    def _open(self, user_input: dict[str, Any] | None) -> SubentryFlowResult:
        """The last page's button: _clicked decides. The button sends an empty input (the page has no fields). Any other
        input is no click: asked for the page again (none), or the step before sent again (a doubled or retried POST of
        its choice or its list lands here, a page without fields takes any input). Then the page, nothing changes."""
        if self._last is None or self._link is None or not isinstance(self._result, CheckResult):
            # Not reached through the last page (Home Assistant lets no flow skip to a step).
            return self.async_abort(reason="check_failed")
        step_id, placeholders = self._last
        if user_input != {}:
            return self.async_show_form(step_id=step_id, data_schema=None, description_placeholders=placeholders)
        return self._clicked(self._result, self._link, OPEN_TEST if step_id == "thumbs_up" else OPEN_FORM)

    def _clicked(self, result: CheckResult, link: str, opened: str) -> SubentryFlowResult:
        """The button: the notices the check answers go, the external step opens the link. Nothing here waits: a second
        click (a double submit) finds the flow on the external step already. A check from a notice clicks only while the
        notice stands as it was made and belongs to a self-check the page answered (the device need not be there, the link
        needs none). A check from the button belongs to no notice: its link opens whatever happened to them since the page
        showed."""
        if (checks := self._checks()) is None:
            return self.async_abort(reason="not_loaded")
        if self._notice is not None and (found := checks.open_notice(*self._notice, self._marked))[0] is None:
            return self.async_abort(reason=found[1])
        checks.close_checked(result, self._marked)
        self._opened, self._address = opened, links.async_link(self.hass, self.flow_id, link)
        return self.async_external_step(step_id=opened, url=self._address, description_placeholders={"link": link})

    async def async_step_report(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return self._open(user_input)

    async def async_step_report_wrong(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return self._open(user_input)

    async def async_step_thumbs_up(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return self._open(user_input)

    def _opened_again(self, user_input: object) -> SubentryFlowResult:
        """The external step: its end when the tab asked for the one-time address (links.OPENED, which only the view
        sends), else the same step whatever comes (Home Assistant lets an external step lead only to another one or to
        its end)."""
        if self._opened is None or self._address is None:
            # Not reached through the button.
            return self.async_abort(reason="check_failed")
        if user_input is links.OPENED:
            return self.async_external_step_done(next_step_id="done")
        return self.async_external_step(step_id=self._opened, url=self._address, description_placeholders={"link": self._link or ""})

    async def async_step_open(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return self._opened_again(user_input)

    async def async_step_open_test(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return self._opened_again(user_input)

    async def async_step_done(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """The end page once the tab opened: the frontend asks for the flow after the view ended the external step."""
        if self._opened is None:
            # Not reached through the external step.
            return self.async_abort(reason="check_failed")
        return self.async_abort(reason=OPENED[self._opened])


class QuirksOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_settings()

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            # Saving reloads nothing: the manager plans again (its update listener).
            return self.async_create_entry(data={CONF_EXCLUDED: sorted(user_input.get(CONF_EXCLUDED, [])), CONF_HIGH_RISK: bool(user_input.get(CONF_HIGH_RISK, False))})
        options = self.config_entry.options
        schema = vol.Schema({
            vol.Optional(CONF_EXCLUDED, default=list(options.get(CONF_EXCLUDED, []))): DeviceSelector(DeviceSelectorConfig(integration=ZHA_DOMAIN, multiple=True)),
            # Off until the user turns it on: high-risk files wait for a device test.
            vol.Optional(CONF_HIGH_RISK, default=bool(options.get(CONF_HIGH_RISK, False))): BooleanSelector(),
        })
        return self.async_show_form(step_id="settings", data_schema=schema)
