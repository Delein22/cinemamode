"""Launch YouTube using this Home Assistant instance's Samsung pairing."""

import probatio as vol

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .models import CinemaError
from .samsung_disney import SamsungDisneyLauncher
from .samsung_youtube import samsung_youtube_pairing


_ENTITY = vol.Match(r"media_player\.[a-z0-9_]{1,180}\Z")
_VIDEO = vol.Match(r"[A-Za-z0-9_-]{11}\Z")
_STATUS_SCHEMA = vol.Schema({vol.Required("entity_id"): _ENTITY})
_PLAY_SCHEMA = vol.Schema({vol.Required("entity_id"): _ENTITY, vol.Required("video_id"): _VIDEO})


async def _check_control(hass: HomeAssistant, call: ServiceCall, entity: str) -> None:
    if call.context.user_id is None:
        return
    user = await hass.auth.async_get_user(call.context.user_id)
    if user is None or not user.permissions.check_entity(entity, POLICY_CONTROL):
        raise Unauthorized(context=call.context, entity_id=entity, permission=POLICY_CONTROL)


@callback
def _launch_target(
    hass: HomeAssistant, entity: str,
) -> tuple[SamsungDisneyLauncher, tuple[int, str | None]] | None:
    manager = hass.data[DOMAIN].get("manager")
    if manager is None or manager.closed:
        return None
    launcher = manager.disney_launcher
    if launcher is None or launcher.player != entity:
        return None
    registered = er.async_get(hass).async_get(entity)
    if registered is None or not registered.config_entry_id:
        return None
    entry = hass.config_entries.async_get_entry(registered.config_entry_id)
    if (entry is None or entry.state is not ConfigEntryState.LOADED or entry.domain != "samsungtv"
            or entry.data.get("method") != "websocket" or entry.data.get("host") != str(launcher.host)):
        return None
    pairing = samsung_youtube_pairing(entry.data)
    return (launcher, pairing) if pairing is not None else None


@callback
def async_register_youtube_actions(hass: HomeAssistant) -> None:
    """Expose only the status and title start for the configured native Samsung."""

    async def status(call: ServiceCall) -> dict[str, bool]:
        entity = call.data["entity_id"]
        await _check_control(hass, call, entity)
        return {"available": _launch_target(hass, entity) is not None}

    async def play(call: ServiceCall) -> dict[str, bool]:
        entity = call.data["entity_id"]
        await _check_control(hass, call, entity)
        target = _launch_target(hass, entity)
        if target is None:
            raise HomeAssistantError("Der YouTube-Direktstart ist für diesen Samsung nicht eingerichtet.")
        launcher, pairing = target
        try:
            await launcher.launch_youtube(f"https://www.youtube.com/watch?v={call.data['video_id']}", pairing)
        except CinemaError as error:
            raise HomeAssistantError(str(error)) from None
        return {"accepted": True}

    hass.services.async_register(DOMAIN, "youtube_status", status, schema=_STATUS_SCHEMA,
                                 supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "youtube_play", play, schema=_PLAY_SCHEMA,
                                 supports_response=SupportsResponse.ONLY)
