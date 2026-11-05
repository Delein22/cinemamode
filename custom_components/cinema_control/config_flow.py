"""One-time UI setup; rooms and favorites are configured in the panel."""

import asyncio

import probatio as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import DOMAIN
from .main_ha import BridgeError, bridge_settings, main_ha_clients


class CinemaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """A local controller; an optional existing TV can be linked in its options."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Kinosteuerung", data={})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return CinemaOptionsFlow()


class CinemaOptionsFlow(config_entries.OptionsFlow):
    """Configure selected main-instance devices without disclosing stored tokens."""

    async def async_step_init(self, user_input=None):
        old = dict(self.config_entry.options)
        values = dict(old)
        errors = {}
        if user_input is not None:
            values.update(user_input)
            try:
                settings = bridge_settings(user_input, old)
                if settings:
                    clients = main_ha_clients(async_get_clientsession(self.hass), settings)
                    await asyncio.gather(*(client.validate() for client in clients))
                return self.async_create_entry(title="", data=settings)
            except BridgeError as err:
                errors["base"] = err.code
        # No old or submitted token is ever placed back in the displayed schema.
        schema = vol.Schema({
            vol.Optional("use_main_ha", default=bool(values.get("use_main_ha", False))): bool,
            vol.Optional("main_ha_url", default=values.get("main_ha_url", "")): str,
            vol.Optional("main_ha_token", default=""): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Optional("main_ha_tv", default=values.get("main_ha_tv", "")): str,
            vol.Optional("main_ha_remote", default=values.get("main_ha_remote", "")): str,
            vol.Optional("main_ha_netflix", default=bool(values.get("main_ha_netflix", False))): bool,
            vol.Optional("main_ha_additional", default=values.get("main_ha_additional", "")): TextSelector(
                TextSelectorConfig(multiline=True)),
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
