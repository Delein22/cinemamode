"""Bounded Samsung navigation via the selected main-instance remote."""

import asyncio

from homeassistant.components.remote import RemoteEntity

from .const import DOMAIN
from .main_ha import BridgeError, KEYS, MainHAEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    data = hass.data[DOMAIN]
    coordinators = [data["bridge"], *data.get("additional_bridges", ())]
    remotes = [MainHARemote(coordinator, entry) for coordinator in coordinators if coordinator.client.remote]
    if remotes:
        async_add_entities(remotes)


class MainHARemote(MainHAEntity, RemoteEntity):
    _attr_name = "Fernbedienung"
    _attr_supported_features = 0

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, coordinator.client.remote, "remote")

    @property
    def is_on(self):
        return self.record.get("state") == "on"

    async def async_send_command(self, command, **kwargs):
        commands = [command] if isinstance(command, str) else list(command)
        delay = kwargs.get("delay_secs", 0.4)
        if (not 1 <= len(commands) <= 10 or any(not isinstance(c, str) or c not in KEYS for c in commands)
                or kwargs.get("num_repeats", 1) != 1 or type(delay) not in (int, float) or not 0 <= delay <= 1
                or kwargs.get("hold_secs", 0) != 0):
            raise BridgeError("not_allowed")
        for index, key in enumerate(commands):
            if index:
                await asyncio.sleep(delay)
            await self.send("remote", "send_command", command=key)
