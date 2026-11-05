"""The selected main-instance TV exposed as a native local media player."""

from homeassistant.components.media_player import MediaPlayerEntity, MediaPlayerEntityFeature as F

from .const import DOMAIN, SAMSUNG_PLEX_APP_ID
from .main_ha import BridgeError, MainHAEntity

_SUPPORTED = (F.TURN_ON | F.TURN_OFF | F.VOLUME_STEP | F.VOLUME_SET | F.VOLUME_MUTE
              | F.SELECT_SOURCE | F.PLAY | F.PAUSE | F.STOP | F.PLAY_MEDIA)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    data = hass.data[DOMAIN]
    coordinators = [data["bridge"], *data.get("additional_bridges", ())]
    async_add_entities([MainHATV(coordinator, entry) for coordinator in coordinators])


class MainHATV(MainHAEntity, MediaPlayerEntity):
    _attr_name = "Fernseher"
    _attr_device_class = "tv"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, coordinator.client.tv, "tv")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        client = self.coordinator.client
        if client.settings.get("main_ha_netflix"):
            launchers = self.hass.data[DOMAIN].setdefault("netflix_launchers", {})
            launchers[self.entity_id] = client
            self.async_on_remove(lambda: launchers.pop(self.entity_id, None))

    @property
    def state(self):
        return self.record.get("state")

    @property
    def supported_features(self):
        return F(self.attributes.get("supported_features", 0)) & _SUPPORTED

    @property
    def volume_level(self):
        value = self.attributes.get("volume_level")
        return value if type(value) in (int, float) and 0 <= value <= 1 else None

    @property
    def is_volume_muted(self):
        value = self.attributes.get("is_volume_muted")
        return value if type(value) is bool else None

    @property
    def source(self):
        return self.attributes.get("source")

    @property
    def source_list(self):
        return self.attributes.get("source_list", [])

    @property
    def media_title(self):
        return self.attributes.get("media_title")

    @property
    def app_id(self):
        return self.attributes.get("app_id")

    async def async_turn_on(self):
        await self.send("media_player", "turn_on")

    async def async_turn_off(self):
        await self.send("media_player", "turn_off")

    async def async_volume_up(self):
        await self.send("media_player", "volume_up")

    async def async_volume_down(self):
        await self.send("media_player", "volume_down")

    async def async_set_volume_level(self, volume):
        await self.send("media_player", "volume_set", volume_level=volume)

    async def async_mute_volume(self, mute):
        await self.send("media_player", "volume_mute", is_volume_muted=mute)

    async def async_select_source(self, source):
        if source not in self.source_list:
            raise BridgeError("not_allowed")
        await self.send("media_player", "select_source", source=source)

    async def async_play_media(self, media_type, media_id, **kwargs):
        if media_type != "app" or media_id != SAMSUNG_PLEX_APP_ID:
            raise BridgeError("not_allowed")
        await self.send("media_player", "play_media", media_content_type=media_type,
                        media_content_id=media_id)

    async def async_media_play(self):
        await self.send("media_player", "media_play")

    async def async_media_pause(self):
        await self.send("media_player", "media_pause")

    async def async_media_stop(self):
        await self.send("media_player", "media_stop")
