"""Fix flows: the stub written again, OK in the notice of the changed devices."""

from __future__ import annotations

import probatio as vol
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import issue_registry as ir

from . import install
from .const import CHANGED, DOMAIN, STUB_ISSUE
from .devices import reload_zha


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict | None) -> RepairsFlow:
    if issue_id == STUB_ISSUE:
        return StubFlow()
    if issue_id == CHANGED:
        return NoticeFlow()
    return ConfirmRepairFlow()


class StubFlow(RepairsFlow):
    """Write the stub again, then reload ZHA."""

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict | None = None) -> FlowResult:
        if (folder := install.custom_quirks_dir(self.hass)) is None:
            return self.async_abort(reason="no_folder")
        if user_input is None:
            return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}), description_placeholders={"folder": str(folder)})
        try:
            await install.async_install(self.hass, folder)
        except OSError as err:
            return self.async_abort(reason="write_failed", description_placeholders={"error": str(err)})
        reload_zha(self.hass)
        return self.async_create_entry(data={})


class NoticeFlow(ConfirmRepairFlow):
    """The notice of the changed devices with its list: OK closes it for good. A list that changed while the dialog was
    open shows again first."""

    def __init__(self) -> None:
        super().__init__()
        self._shown: dict[str, str] | None = None

    async def async_step_confirm(self, user_input: dict | None = None) -> FlowResult:
        if (issue := ir.async_get(self.hass).async_get_issue(DOMAIN, CHANGED)) is None:
            # Closed meanwhile.
            return self.async_create_entry(data={})
        if user_input is None or issue.translation_placeholders != self._shown:
            self._shown = issue.translation_placeholders
            return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}), description_placeholders=self._shown)
        if not (loaded := self.hass.config_entries.async_loaded_entries(DOMAIN)):
            return self.async_abort(reason="not_loaded")
        # The end of the flow deletes the issue. No await before it: a notice raised in between would go too.
        loaded[0].runtime_data.async_close_notice()
        return self.async_create_entry(data={})
