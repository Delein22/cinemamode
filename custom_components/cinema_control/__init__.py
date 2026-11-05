"""Home Assistant Kinosteuerung: existing devices, one local panel."""

import asyncio
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, PANEL_PATH, VERSION
from .images import async_register_image_upload
from .manager import CinemaManager
from .main_ha import BridgeError, MainHACoordinator, main_ha_clients
from .websocket import async_register_api
from .youtube_action import async_register_youtube_actions


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    data = hass.data.setdefault(DOMAIN, {})
    data.setdefault("manager", None)
    data.setdefault("static_registered", False)
    async_register_api(hass)
    async_register_youtube_actions(hass)
    await async_register_image_upload(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    current = CinemaManager(hass)
    await current.async_load()
    bridge = None
    try:
        if entry.options.get("use_main_ha"):
            clients = main_ha_clients(async_get_clientsession(hass), dict(entry.options))
            records = await asyncio.gather(*(client.validate() for client in clients))
            coordinators = []
            for client, initial in zip(clients, records, strict=True):
                coordinator = MainHACoordinator(hass, entry, client)
                coordinator.async_set_updated_data(initial)
                coordinators.append(coordinator)
            bridge, *additional_bridges = coordinators
            hass.data[DOMAIN]["bridge"] = bridge
            hass.data[DOMAIN]["additional_bridges"] = additional_bridges
            await hass.config_entries.async_forward_entry_setups(entry, ["media_player", "remote"])
        if not hass.data[DOMAIN]["static_registered"]:
            await hass.http.async_register_static_paths([
                StaticPathConfig("/cinema_control_static", str(Path(__file__).parent / "frontend"), False)
            ])
            hass.data[DOMAIN]["static_registered"] = True
        hass.data[DOMAIN]["manager"] = current
        await panel_custom.async_register_panel(
            hass, frontend_url_path=PANEL_PATH, webcomponent_name="cinema-control-panel",
            sidebar_title="Kinosteuerung", sidebar_icon="mdi:movie-open-outline",
            module_url=f"/cinema_control_static/cinema-panel.js?v={VERSION}",
            require_admin=False, config={"version": VERSION},
        )
    except Exception as err:
        if bridge:
            await hass.config_entries.async_unload_platforms(entry, ["media_player", "remote"])
            hass.data[DOMAIN]["bridge"] = None
            hass.data[DOMAIN].pop("additional_bridges", None)
        hass.data[DOMAIN]["manager"] = None
        await current.async_close()
        if isinstance(err, BridgeError):
            raise ConfigEntryNotReady(str(err)) from err
        raise
    async def shutdown(_event):
        await current.async_close()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown))
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if hass.data[DOMAIN].get("bridge"):
        if not await hass.config_entries.async_unload_platforms(entry, ["media_player", "remote"]):
            return False
        hass.data[DOMAIN]["bridge"] = None
        hass.data[DOMAIN].pop("additional_bridges", None)
    if current := hass.data[DOMAIN].get("manager"):
        await current.async_close()
        hass.data[DOMAIN]["manager"] = None
    frontend.async_remove_panel(hass, PANEL_PATH)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
