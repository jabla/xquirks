"""Config flow: one entry, no settings needed. Options: the devices xquirks leaves alone and whether
it offers high-risk files that are not verified on a device, and the device check: one device that
runs an xquirks file is asked for its current values, the user marks what matches the device and gets
a report with a button that opens the prefilled issue (checks.py). The fix flow of a self-check notice
uses the same steps (DeviceCheck, repairs.py)."""

from __future__ import annotations

import asyncio
from typing import Any

import probatio as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    DeviceSelector,
    DeviceSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import text
from .checks import CheckResult, Checks
from .const import CONF_EXCLUDED, CONF_HIGH_RISK, DOMAIN, TITLE, ZHA_DOMAIN
from .manager import zha_folder

# The fields of the device check.
DEVICE = "device"
MATCHES = "matches"
WRONG = "wrong"


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


class DeviceCheck:
    """The steps of the device check, for the options flow and the fix flow of a self-check notice (repairs.SelfCheckFlow):
    which device ("check"), asking it with a progress dialog ("ask"), what it shows ("results": the user marks what
    matches the device and what does not), the report with its prefilled issue link.

    The last page (the report "report" or the problem report "report_wrong") keeps the report when it shows
    (Checks.finish_check). It is a page without fields whose one button opens its link: Home Assistant shows Close on an
    abort page and has no link button. The button answers with an external step (step "open"): the frontend opens the
    link in a new tab and offers it as a button in case the browser blocked that. The dialog stays there until its X
    deletes the flow. Only the button closes the notices the check answers (_clicked, Checks.close_checked): the X on the
    last page leaves them.

    A flow that uses them gives the checks (_checks). It may limit the devices (_candidates), pick the only one without
    asking (PICK_ONE), end a step that went stale (_stale), show a page of its own before the list (RESULTS), end
    otherwise (_finish) and decide the button on its own (_clicked)."""

    # The step after the device was asked.
    RESULTS = "results"
    # One device to choose from: asked without the picker.
    PICK_ONE = False
    # The list says when the device did not answer (a flow whose page before the list says so leaves it out).
    NO_ANSWER_ERROR = True

    _device: str | None = None
    _task: asyncio.Task[CheckResult | str] | None = None
    _result: CheckResult | str | None = None
    # The last page as shown (step id and placeholders), the link its button opens (None before it showed) and the
    # self-checks the check answered (Checks.finish_check: their notices go with the button).
    _last: tuple[str, dict[str, str]] | None = None
    _link: str | None = None
    _marked: frozenset[tuple[object, ...]] = frozenset()

    def _checks(self) -> Checks | None:
        raise NotImplementedError

    def _candidates(self, checks: Checks) -> list[tuple[str, str]]:
        return checks.candidates()

    def _stale(self, checks: Checks) -> ConfigFlowResult | None:
        """The abort of a step whose flow went stale (None: it goes on)."""
        return None

    async def async_step_check(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Which device: one that runs an xquirks file and is not left out."""
        if (checks := self._checks()) is None:
            return self.async_abort(reason="not_loaded")
        if not self.hass.config_entries.async_loaded_entries(ZHA_DOMAIN):
            return self.async_abort(reason="zha_not_loaded")
        if (stale := self._stale(checks)) is not None:
            return stale
        candidates = self._candidates(checks)
        if not candidates:
            return self.async_abort(reason="no_device")
        if user_input is not None and user_input.get(DEVICE) in dict(candidates):
            self._device = user_input[DEVICE]
            return await self.async_step_ask()
        if self.PICK_ONE and len(candidates) == 1:
            self._device = candidates[0][0]
            return await self.async_step_ask()
        options = [SelectOptionDict(value=device_id, label=label) for device_id, label in candidates]
        schema = vol.Schema({vol.Required(DEVICE, default=candidates[0][0]): SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN))})
        return self.async_show_form(step_id="check", data_schema=schema)

    async def async_step_ask(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask the device, wait for its values (a progress dialog)."""
        if (checks := self._checks()) is None or self._device is None:
            return self.async_abort(reason="not_loaded")
        first = self._task is None
        if first:
            self._task = self.hass.async_create_task(checks.async_run_check(self._device), "xquirks device check")
        if not self._task.done():
            return self.async_show_progress(step_id="ask", progress_action="asking", progress_task=self._task)
        self._result, self._task = self._task.result(), None
        if first:
            # Done before any progress dialog showed (it sent nothing): straight to the results, without
            # the device picker's input.
            return await getattr(self, f"async_step_{self.RESULTS}")()
        return self.async_show_progress_done(next_step_id=self.RESULTS)

    def _checked(self) -> tuple[Checks, CheckResult] | ConfigFlowResult:
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

    async def async_step_results(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """What the device shows: the user marks what matches the device and what does not."""
        if not isinstance(checked := self._checked(), tuple):
            return checked
        checks, result = checked
        errors: dict[str, str] = {}
        if user_input is not None:
            matches, wrong = set(user_input.get(MATCHES, [])), set(user_input.get(WRONG, []))
            if matches & wrong:
                errors["base"] = "both"
            elif not matches and not wrong:
                errors["base"] = "nothing"
            else:
                return self._finish(checks, result, matches, wrong)
        elif result.answered is False and self.NO_ANSWER_ERROR:
            # Shown above the form: the values below are the stored ones (None: nothing was asked). A
            # mains device does not sleep: it cannot be reached. Any other may sleep: wake it.
            errors["base"] = "no_answer_mains" if result.mains else "no_answer"
        options = [SelectOptionDict(value=row.suffix, label=f"{row.name}: {row.value or 'no value'}") for row in result.rows]
        choice = SelectSelector(SelectSelectorConfig(options=options, multiple=True, mode=SelectSelectorMode.LIST))
        schema = vol.Schema({vol.Optional(MATCHES, default=list((user_input or {}).get(MATCHES, []))): choice,
                             vol.Optional(WRONG, default=list((user_input or {}).get(WRONG, []))): choice})
        return self.async_show_form(step_id="results", data_schema=schema, errors=errors, description_placeholders=self._shown(result))

    @staticmethod
    def _shown(result: CheckResult) -> dict[str, str]:
        """What a page of the results shows: the device, the file and the table of its values."""
        return {"device": text.plain(result.name), "file": result.file, "revision": result.revision or "unknown", "entities": text.check_dialog(result)}

    def _finish(self, checks: Checks, result: CheckResult, matches: set[str], wrong: set[str]) -> ConfigFlowResult:
        """The report: the counts, the button opens the form it goes to."""
        _report, url, answered = checks.finish_check(result, matches, wrong)
        unchecked = str(len(result.rows) - len(matches) - len(wrong))
        if wrong:
            return self._show_last("report_wrong", {"wrong": str(len(wrong)), "matches": str(len(matches)), "unchecked": unchecked}, url, answered)
        return self._show_last("report", {"matches": str(len(matches)), "unchecked": unchecked}, url, answered)

    def _show_last(self, step_id: str, placeholders: dict[str, str], link: str, answered: frozenset[tuple[object, ...]]) -> ConfigFlowResult:
        self._last, self._link, self._marked = (step_id, placeholders), link, answered
        return self.async_show_form(step_id=step_id, data_schema=None, description_placeholders=placeholders)

    def _open(self, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        """The last page's button: _clicked decides. The button sends an empty input (the page has no fields). Any other
        input is no click: asked for the page again (none), or the step before sent again (a doubled or retried POST of
        its choice or its list lands here, a page without fields takes any input). Then the page, nothing changes."""
        if self._last is None or self._link is None or not isinstance(self._result, CheckResult):
            # Not reached through the last page (Home Assistant lets no flow skip to a step).
            return self.async_abort(reason="check_failed")
        if user_input != {}:
            step_id, placeholders = self._last
            return self.async_show_form(step_id=step_id, data_schema=None, description_placeholders=placeholders)
        return self._clicked(self._result, self._link)

    def _clicked(self, result: CheckResult, link: str) -> ConfigFlowResult:
        """The button: the notices the check answers go, the external step opens the link. Nothing here waits: a second
        click (a double submit) finds the flow on the external step already. The options check belongs to no notice: its
        link opens whatever happened to them since the page showed."""
        if (checks := self._checks()) is None:
            return self.async_abort(reason="not_loaded")
        checks.close_checked(result, self._marked)
        return self.async_external_step(step_id="open", url=link)

    async def async_step_report(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self._open(user_input)

    async def async_step_report_wrong(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self._open(user_input)

    async def async_step_open(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The link opened: the same external step whatever comes (it can only lead to another one or to its end)."""
        if self._link is None:
            return self.async_abort(reason="check_failed")
        return self.async_external_step(step_id="open", url=self._link)


class QuirksOptionsFlow(DeviceCheck, OptionsFlow):
    def __init__(self) -> None:
        self._device = None
        self._task = None
        self._result = None
        self._last = None
        self._link = None
        self._marked = frozenset()

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["settings", "check"])

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

    def _checks(self) -> Checks | None:
        entry = self.config_entry
        return entry.runtime_data.checks if entry.state is ConfigEntryState.LOADED else None
