"""Fix flows: the change list (confirmed as a whole), the reminder to reload ZHA, the option that offers
held-back high-risk files and the first page of a self-check notice.

The change list's dialog shows every install, update and removal; "Apply all" carries them out
together (manager.async_apply) and reloads ZHA right after, so they take effect without another
step. The "changes wait for a ZHA reload" issue is left only when ZHA was not loaded; it reloads
ZHA when the user picks it. The notice of a held-back file turns on "Offer high-risk files"
(HeldBackFlow): the change list that follows asks before it installs anything. A self-check notice
hands over to the device check of the entry (SelfCheckFlow).

A first page shows the device picture of this run (manager.picture): its address carries a token of each Home Assistant
run, so the one a notice stored from an earlier run is dead.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.components.repairs.const import FlowType
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult, FlowResultType
from homeassistant.helpers import issue_registry as ir

from . import text
from . import plan as plans
from .checks import KINDS, OFF, Checks
from .config_flow import DEVICE_CHECK, SOURCE_NOTICE
from .const import CONF_HIGH_RISK, DOMAIN

if TYPE_CHECKING:
    from .manager import QuirkManager

# How long the first page of a self-check notice's flow waits for its confirmation search, in seconds (the wait for a
# search a self-check runs included).
SEARCH_WAIT = 5.0


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict | None) -> RepairsFlow:
    data = dict(data or {})
    kind = data.get("kind") if isinstance(data.get("kind"), str) else None
    flows = {"changes": ChangesFlow, "reload": ReloadFlow, "held_back": HeldBackFlow} | dict.fromkeys(KINDS, SelfCheckFlow)
    return flows.get(kind, UnknownFlow)(issue_id, data)


class _QuirkFlow(RepairsFlow):
    """The dialog is a menu: the change ("apply") or "not_now".

    Home Assistant shows no Ignore for fixable issues, so "not_now" ignores the issue itself. It
    then waits under "Show ignored repairs"; a list with another id comes back as a new issue.
    """

    # The abort when the issue went while the dialog was open.
    GONE = "nothing_to_do"

    def __init__(self, issue_id: str, data: dict) -> None:
        super().__init__()
        self.issue_id = issue_id
        self.issue_data = data

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        # Home Assistant calls init with {"issue_id": ...} as user_input: the dialog is "confirm".
        return await self.async_step_confirm()

    async def async_step_not_now(self, user_input: dict | None = None) -> FlowResult:
        if ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id) is None:
            # Another list came, or ZHA reloaded, while the dialog was open: nothing to put aside.
            return self.async_abort(reason=self.GONE)
        ir.async_ignore_issue(self.hass, DOMAIN, self.issue_id, True)
        return self.async_abort(reason="not_now")

    def _manager(self) -> QuirkManager | None:
        entries = self.hass.config_entries.async_loaded_entries(DOMAIN)
        return entries[0].runtime_data if entries else None


class ChangesFlow(_QuirkFlow):
    """Every change of the list, applied together or not at all."""

    GONE = "stale"

    def _gone(self, manager: QuirkManager) -> FlowResult | None:
        """The abort when the list the issue was made for is not the current one."""
        plan = manager.plan
        if plan.held:
            return self._held(plan.held)
        if not plan.changes or plan.key != self.issue_data.get("key"):
            return self.async_abort(reason="stale")
        return None

    def _held(self, reason: str) -> FlowResult:
        """Nothing was changed: a whole text per reason the list is held (plan.py), in the user's language."""
        return self.async_abort(reason=f"held_{reason}")

    async def async_step_confirm(self, user_input: dict | None = None) -> FlowResult:
        if (manager := self._manager()) is None:
            return self.async_abort(reason="not_loaded")
        # Decided on a fresh plan, as the apply is: a hold whose cause went away ends here.
        await manager.async_refresh()
        if (gone := self._gone(manager)) is not None:
            return gone
        placeholders = {"changes": await manager.async_change_text(), "folder": manager.folder or ""}
        return self.async_show_menu(step_id="confirm", menu_options=["apply", "not_now"], description_placeholders=placeholders)

    async def async_step_apply(self, user_input: dict | None = None) -> FlowResult:
        if (manager := self._manager()) is None:
            return self.async_abort(reason="not_loaded")
        # Home Assistant cancels this request when the browser goes away: the list is carried out to
        # its end all the same, only the reload of ZHA is then left to the reminder.
        applied = await asyncio.shield(self.hass.async_create_task(manager.async_apply(self.issue_data.get("key", ""))))
        if applied.held:
            return self._held(applied.held)
        if applied.stale:
            return self.async_abort(reason="stale")
        placeholders: dict[str, Any] = {"folder": manager.folder or ""}
        if applied.failed and not applied.left:
            return self.async_abort(reason="failed", description_placeholders={**placeholders, "failed": applied.failed})
        reloading = manager.async_reload_zha()
        if applied.failed:
            # Some steps could not be undone: ZHA loads what is there, the next list repairs the rest.
            return self.async_abort(reason="failed_partly", description_placeholders={**placeholders, "failed": applied.failed, "left": text.name_list(applied.left)})
        return self.async_abort(reason="done" if reloading else "done_zha_not_loaded", description_placeholders={**placeholders, "done": text.name_list(applied.done)})


class ReloadFlow(_QuirkFlow):
    async def async_step_confirm(self, user_input: dict | None = None) -> FlowResult:
        if (manager := self._manager()) is None:
            return self.async_abort(reason="not_loaded")
        if not (changes := manager.data["pending"]["changes"]):
            return self.async_abort(reason="nothing_to_do")
        placeholders = {"changes": text.name_list([f"`{c['file']}`: {c['change']}" for c in changes]), "folder": manager.folder or ""}
        return self.async_show_menu(step_id="confirm", menu_options=["apply", "not_now"], description_placeholders=placeholders)

    async def async_step_apply(self, user_input: dict | None = None) -> FlowResult:
        if (manager := self._manager()) is None:
            return self.async_abort(reason="not_loaded")
        if not manager.data["pending"]["changes"]:
            return self.async_abort(reason="nothing_to_do")
        manager.async_reload_zha()
        return self.async_create_entry(data={})


class HeldBackFlow(_QuirkFlow):
    """"Offer high-risk files" on the notice of a held-back file: the option of that name (CONF_HIGH_RISK) holds for
    every file, so the page names each file held back now and the button turns it on only while those are still the
    files it releases. Nothing is downloaded or installed here: the change list that follows asks first."""

    GONE = "stale"
    # The holds of a plan made in full: any other plan does not say which files it holds back (a change waits for ZHA,
    # no content state, no readable folder).
    FULL = (None, plans.UNCHECKED, plans.ZHA_UNKNOWN)

    def __init__(self, issue_id: str, data: dict) -> None:
        super().__init__(issue_id, data)
        # The files the page named (None before it showed).
        self._shown: tuple[str, ...] | None = None

    def _held_back(self, manager: QuirkManager) -> tuple[str, ...] | FlowResult:
        """The files held back now. The abort when the notice no longer stands: the option is on already. Or the
        notice's file is no longer held back (the notice went with it). Or the plan cannot tell (held)."""
        if manager.entry.options.get(CONF_HIGH_RISK):
            return self.async_abort(reason="already_on")
        if manager.plan.held not in self.FULL:
            return self.async_abort(reason="held")
        names = tuple(sorted(item.name for item in manager.plan.held_back))
        if self.issue_data.get("file") not in names or ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id) is None:
            return self.async_abort(reason="stale")
        return names

    async def async_step_confirm(self, user_input: dict | None = None) -> FlowResult:
        if (manager := self._manager()) is None:
            return self.async_abort(reason="not_loaded")
        # Decided on a fresh plan, as the button is.
        await manager.async_refresh()
        if not isinstance(names := self._held_back(manager), tuple):
            return names
        self._shown = names
        # What the notice says (a fixable notice shows no description), as it stands after the refresh.
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id)
        stored = (issue.translation_placeholders if issue is not None else None) or {}
        placeholders = {key: str(stored.get(key, "")) for key in ("devices", "file", "status", "reasons")}
        placeholders["picture"] = manager.picture(self.issue_data["file"])
        placeholders["files"] = text.name_list([f"`{name}` ({text.plain(manager.quirk_label(name))})" for name in names])
        return self.async_show_menu(step_id="confirm", menu_options=["apply", "not_now"], description_placeholders=placeholders)

    async def async_step_apply(self, user_input: dict | None = None) -> FlowResult:
        if (manager := self._manager()) is None:
            return self.async_abort(reason="not_loaded")
        await manager.async_refresh()
        if not isinstance(names := self._held_back(manager), tuple):
            return names
        if names != self._shown:
            # The button would release other files than the page named.
            return self.async_abort(reason="stale")
        entry = manager.entry
        # Saving the options plans again (the manager's update listener): the change list offers the files.
        self.hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_HIGH_RISK: True})
        return self.async_abort(reason="done")


class SelfCheckFlow(RepairsFlow):
    """The first page of a self-check notice (checks.py: values look fine, did not come, look off): what the notice found,
    with "Check the device now" or "Do not check". Before it shows, one search (SEARCH_WAIT at most): a fine or unproven
    notice whose devices are all past the threshold now, or whose file is verified now, goes ("already confirmed"). "Looks
    off" always stays. "Do not check" closes the notice for its file revision. From the search answer only the issue
    number (in a link built on the device check's last page) and its count reach a page.

    "Check the device now" hands over to the device check, a subentry flow of the entry (config_flow.DeviceCheckFlow,
    source "notice"): its dialog shows a text on the page after the report's button, the repairs dialog would show an
    empty page. The device check gets the notice and the search answer. Its first step runs the checks a device check
    starts with (the entry and ZHA loaded, the notice as it was made, a device of the notice there) and asks the one
    device of a notice at once. A check that cannot start ends here with its reason. Otherwise this flow ends with an
    abort that names the device check as the next flow: the abort keeps the notice, the frontend opens the device check's
    dialog without an abort page. The device check closes the notice with the button of its last page.
    """

    def __init__(self, issue_id: str, data: dict) -> None:
        super().__init__()
        self.issue_id = issue_id
        self.issue_data = data
        # The first page's search answer (None: no search, or it failed), for the device check's last page.
        self._answer: object = None

    def _manager(self) -> QuirkManager | None:
        entries = self.hass.config_entries.async_loaded_entries(DOMAIN)
        return entries[0].runtime_data if entries else None

    def _notice(self, checks: Checks) -> tuple[dict[str, Any] | None, str]:
        """The self-check of the notice while the notice stands as it was made; otherwise None and the abort reason."""
        return checks.open_notice(self.issue_id, self.issue_data)

    def _devices(self, checks: Checks) -> list[str]:
        """The notice's devices the device check can ask now."""
        return checks.notice_devices(self.issue_data.get("file"), self.issue_data.get("devices"))

    def _close(self, checks: Checks, observation: dict[str, Any], reason: str) -> FlowResult:
        checks.close_notice(self.issue_data["file"], observation)
        return self.async_abort(reason=reason)

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        # Home Assistant calls init with {"issue_id": ...} as user_input: the first page.
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict | None = None) -> FlowResult:
        if (manager := self._manager()) is None:
            return self.async_abort(reason="not_loaded")
        observation, reason = self._notice(manager.checks)
        if observation is None:
            return self.async_abort(reason=reason)
        name, off = self.issue_data["file"], self.issue_data.get("kind") == OFF
        if not off and manager.checks.file_verified(name, observation):
            return self._close(manager.checks, observation, "confirmed")
        key, threshold = manager.checks.file_key(name, observation), manager.source.confirm_threshold
        if key is not None and threshold is not None:
            wanted = manager.checks.fixed_parts(self._devices(manager.checks), key)
            self._answer = await manager.checks.async_search_now(key, SEARCH_WAIT, wanted)
            # Decided on what stands after the search.
            if (manager := self._manager()) is None:
                return self.async_abort(reason="not_loaded")
            observation, reason = self._notice(manager.checks)
            if observation is None:
                return self.async_abort(reason=reason)
            devices = self._devices(manager.checks)
            if not off and devices and manager.checks.confirmed(name, observation, self._answer, devices) >= set(devices):
                return self._close(manager.checks, observation, "confirmed")
        # What the notice found, as it showed it (a fixable issue shows no description).
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id)
        stored = (issue.translation_placeholders if issue is not None else None) or {}
        shown = ("file", "revision", "devices", *(("link",) if off else ()))
        placeholders = {key: str(stored.get(key, "")) for key in shown}
        placeholders["picture"] = manager.picture(name)
        if off:
            # The open reports of the file before the form: built here, a notice of an earlier release has no such link.
            placeholders["reports"] = text.reports_url(name)
        return self.async_show_menu(step_id="confirm", menu_options=["check", "skip"], description_placeholders=placeholders)

    async def async_step_skip(self, user_input: dict | None = None) -> FlowResult:
        """"Do not check": the notice goes and does not come back for this revision of the file."""
        if (manager := self._manager()) is None:
            return self.async_abort(reason="not_loaded")
        observation, reason = self._notice(manager.checks)
        if observation is None:
            return self.async_abort(reason=reason)
        return self._close(manager.checks, observation, "skipped")

    async def async_step_check(self, user_input: dict | None = None) -> FlowResult:
        """"Check the device now": the device check of the entry takes over."""
        if (manager := self._manager()) is None:
            return self.async_abort(reason="not_loaded")
        started = await self.hass.config_entries.subentries.async_init(
            (manager.entry.entry_id, DEVICE_CHECK),
            context={"source": SOURCE_NOTICE},
            data={"issue_id": self.issue_id, "data": self.issue_data, "answer": self._answer},
        )
        if started["type"] is FlowResultType.ABORT:
            # The device check's reasons for a notice are this flow's too.
            return self.async_abort(reason=started["reason"])
        return self.async_abort(reason="check_started", next_flow=(FlowType.CONFIG_SUBENTRIES_FLOW, started["flow_id"]))


class UnknownFlow(RepairsFlow):
    def __init__(self, issue_id: str, data: dict) -> None:
        super().__init__()

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        return self.async_abort(reason="nothing_to_do")
