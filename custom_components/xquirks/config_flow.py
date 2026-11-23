"""Config flow: one entry. Setting it up writes the stub and its options into ZHA's custom quirks folder. Options: the
devices xquirks leaves alone and the high-risk opt-in, both in the options file the loader reads, and a ZHA reload."""

from __future__ import annotations

from typing import Any

import probatio as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import BooleanSelector, SelectOptionDict, SelectSelector, SelectSelectorConfig, SelectSelectorMode

from . import install
from .const import DOMAIN, TITLE
from .devices import definition, label_of, name_of, reload_zha, zha_devices

EXCLUDED = "excluded"
HIGH_RISK = "high_risk"
RELOAD = "reload"


class XquirksConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # manifest.json has single_config_entry: Home Assistant aborts a second flow itself.
        if (folder := install.custom_quirks_dir(self.hass)) is None:
            return self.async_abort(reason="no_folder")
        return self.async_show_menu(step_id="user", menu_options=["reload", "later"], description_placeholders={"folder": str(folder)})

    async def async_step_reload(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_create(reload=True)

    async def async_step_later(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_create(reload=False)

    async def _async_create(self, *, reload: bool) -> ConfigFlowResult:
        if (folder := install.custom_quirks_dir(self.hass)) is None:
            return self.async_abort(reason="no_folder")
        try:
            await install.async_install(self.hass, folder)
        except OSError as err:
            return self.async_abort(reason="write_failed", description_placeholders={"error": str(err)})
        if reload:
            reload_zha(self.hass)
        return self.async_create_entry(title=TITLE, data={})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> XquirksOptionsFlow:
        return XquirksOptionsFlow()


class XquirksOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if (folder := install.custom_quirks_dir(self.hass)) is None:
            return self.async_abort(reason="no_folder")
        if user_input is not None:
            try:
                await install.async_set_options(self.hass, folder, excluded=user_input.get(EXCLUDED, []), high_risk=bool(user_input.get(HIGH_RISK)))
            except OSError as err:
                return self.async_abort(reason="write_failed", description_placeholders={"error": str(err)})
            if user_input.get(RELOAD):
                reload_zha(self.hass)
            # The options live in the loader's file.
            return self.async_create_entry(data={})
        try:
            excluded, high_risk = await install.async_read_options(self.hass, folder)
        except OSError as err:
            return self.async_abort(reason="options_broken", description_placeholders={"error": str(err)})
        devices = SelectSelector(SelectSelectorConfig(options=_choices(self.hass, excluded), multiple=True, mode=SelectSelectorMode.LIST))
        schema = vol.Schema({
            vol.Optional(EXCLUDED, default=excluded): devices,
            vol.Optional(HIGH_RISK, default=high_risk): BooleanSelector(),
            vol.Optional(RELOAD, default=True): BooleanSelector(),
        })
        return self.async_show_form(step_id="init", data_schema=schema)


@callback
def _choices(hass: HomeAssistant, excluded: list[str]) -> list[SelectOptionDict]:
    """The devices on an xquirks definition and those left alone already, by IEEE address."""
    names = {ieee: f"{name_of(hass, ieee)} ({definition(label)[0]})" for ieee, device in (zha_devices(hass) or {}).items() if (label := label_of(device))}
    for ieee in excluded:
        names.setdefault(ieee, name_of(hass, ieee))
    return [SelectOptionDict(value=ieee, label=label) for ieee, label in sorted(names.items(), key=lambda item: item[1].casefold())]
