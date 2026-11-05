"""A bounded Samsung bridge using the existing main HA REST API.

Only configured devices and fixed TV actions are accessible. Tokens
stay in the config entry and are never returned to the cinema panel.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
import logging
import re
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, SAMSUNG_PLEX_APP_ID

_LOGGER = logging.getLogger(__name__)
TV_SERVICES = frozenset({"turn_on", "turn_off", "volume_up", "volume_down", "volume_set",
                         "volume_mute", "select_source", "media_play", "media_pause", "media_stop",
                         "play_media"})
KEYS = frozenset({"KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT", "KEY_ENTER", "KEY_RETURN", "KEY_HOME"})
_ATTRS = frozenset({"friendly_name", "device_class", "supported_features", "source", "source_list",
                   "volume_level", "is_volume_muted", "media_title", "app_id"})
_YOUTUBE_STATUS_INTERVAL = 60


class BridgeError(HomeAssistantError):
    """A safe error code, containing neither tokens nor server response bodies."""

    def __init__(self, code: str = "cannot_connect") -> None:
        self.code = code
        super().__init__({
            "invalid_auth": "Zugriff auf die Hauptinstanz verweigert. Bitte Zugangstoken prüfen.",
            "invalid_device": "Bitte einen Samsung-TV und dessen zugehörige Fernbedienung auswählen.",
            "invalid_input": "Adresse oder Gerätekennung ist ungültig.",
            "not_allowed": "Diese Aktion ist für die TV-Verbindung nicht freigegeben.",
            "youtube_not_accepted": "Die Hauptinstanz hat den YouTube-Start nicht bestätigt.",
        }.get(code, "Die Hauptinstanz ist nicht erreichbar oder liefert keine gültige Antwort."))


def bridge_settings(values: dict, previous: dict | None = None) -> dict:
    """Validate the exact credential recipient and bounded entity IDs."""
    if not values.get("use_main_ha"):
        return {}
    previous = previous or {}
    url = str(values.get("main_ha_url", "")).strip().rstrip("/")
    token = str(values.get("main_ha_token", "")).strip() or previous.get("main_ha_token", "")
    tv = str(values.get("main_ha_tv", "")).strip()
    remote = str(values.get("main_ha_remote", "")).strip()
    try:
        parsed = urlsplit(url)
        valid_port = parsed.port is None or 1 <= parsed.port <= 65535
    except ValueError as err:
        raise BridgeError("invalid_input") from err
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or not valid_port
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in url) or "\\" in url
            or any(part in (".", "..") for part in parsed.path.split("/")) or "%" in parsed.path
            or not isinstance(token, str) or not token or len(token) > 8192
            or any(c.isspace() or ord(c) < 32 for c in token)
            or not re.fullmatch(r"media_player\.[a-z0-9_]{1,180}", tv)
            or (remote and not re.fullmatch(r"remote\.[a-z0-9_]{1,180}", remote))):
        raise BridgeError("invalid_input")
    # A blank password may retain a credential only for the same recipient.
    if (not str(values.get("main_ha_token", "")).strip()
            and previous.get("main_ha_url") != url):
        raise BridgeError("invalid_auth")
    additional = str(values.get("main_ha_additional", "")).strip()
    settings = {"use_main_ha": True, "main_ha_url": url, "main_ha_token": token,
                "main_ha_tv": tv, "main_ha_remote": remote}
    if values.get("main_ha_netflix") is True:
        settings["main_ha_netflix"] = True
    if additional:
        if len(additional) > 4096:
            raise BridgeError("invalid_input")
        devices = parse_additional_devices(additional, tv, remote)
        settings["main_ha_additional"] = "\n".join(
            f"{player}, {control}" if control else player for player, control in devices
        )
    return settings


def parse_additional_devices(value: str, tv: str, remote: str) -> list[tuple[str, str]]:
    """Each line identifies one player and optionally its own remote."""
    seen = {tv, remote} - {""}
    devices = []
    for line in value.splitlines():
        if not line.strip():
            continue
        player, _, control = (part.strip() for part in line.partition(","))
        if (len(devices) >= 9 or not re.fullmatch(r"media_player\.[a-z0-9_]{1,180}", player)
                or player in seen
                or (control and (control in seen or not re.fullmatch(r"remote\.[a-z0-9_]{1,180}", control)))):
            raise BridgeError("invalid_input")
        devices.append((player, control))
        seen.add(player)
        if control:
            seen.add(control)
    return devices


def main_ha_clients(session: ClientSession, settings: dict) -> list[MainHAClient]:
    """Reuse the same server and credential for each independently polled device."""
    validated = bridge_settings(settings)
    if not validated:
        return []
    tv, remote = validated["main_ha_tv"], validated["main_ha_remote"]
    additional = validated.pop("main_ha_additional", "")
    clients = [MainHAClient(session, validated)]
    validated.pop("main_ha_netflix", None)
    clients.extend(
        MainHAClient(session, {**validated, "main_ha_tv": player, "main_ha_remote": control})
        for player, control in parse_additional_devices(additional, tv, remote)
    )
    return clients


class MainHAClient:
    """No discovery, redirects, service retries, shell access or arbitrary targets."""

    def __init__(self, session: ClientSession, settings: dict) -> None:
        self.session = session
        self.settings = bridge_settings(settings)
        self.tv = self.settings["main_ha_tv"]
        self.remote = self.settings["main_ha_remote"]
        self.source_id = hashlib.sha256(
            (self.settings["main_ha_url"] + "|" + self.tv + "|" + self.remote).encode()
        ).hexdigest()[:24]
        self.youtube_available = False
        self._youtube_status_due = 0.0

    async def request(self, method: str, path: str, payload: dict | None = None, *, timeout: int = 6) -> Any:
        try:
            async with self.session.request(
                method, self.settings["main_ha_url"] + path,
                headers={"Authorization": "Bearer " + self.settings["main_ha_token"]},
                json=payload, allow_redirects=False, timeout=ClientTimeout(total=timeout),
            ) as response:
                if response.status in (401, 403):
                    raise BridgeError("invalid_auth")
                if response.status == 404:
                    raise BridgeError("invalid_device")
                if not 200 <= response.status < 300:
                    raise BridgeError()
                # Read to EOF under a strict size bound, including chunked replies.
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    body.extend(chunk)
                    if len(body) > 524288:
                        raise BridgeError()
                return json.loads(body)
        except (ClientError, TimeoutError, ValueError, UnicodeError) as err:
            raise BridgeError() from err

    async def fetch(self) -> dict[str, dict]:
        ids = [self.tv] + ([self.remote] if self.remote else [])
        values = await asyncio.gather(*(self.request("GET", "/api/states/" + entity) for entity in ids))
        result = {}
        for entity, value in zip(ids, values, strict=True):
            if (not isinstance(value, dict) or value.get("entity_id") != entity
                    or not isinstance(value.get("state"), str) or not isinstance(value.get("attributes"), dict)):
                raise BridgeError("invalid_device")
            attrs = {k: v for k, v in value["attributes"].items() if k in _ATTRS}
            allowed_states = {"on", "off", "unavailable", "unknown"}
            if entity == self.tv:
                allowed_states |= {"idle", "playing", "paused", "buffering", "standby"}
            if value["state"] not in allowed_states:
                raise BridgeError("invalid_device")
            for name in ("friendly_name", "source", "media_title", "app_id", "device_class"):
                if name in attrs and attrs[name] is not None and (
                        not isinstance(attrs[name], str) or len(attrs[name]) > 512):
                    raise BridgeError("invalid_device")
            if entity == self.tv:
                features = attrs.get("supported_features", 0)
                sources = attrs.get("source_list", [])
                if (type(features) is not int or not 0 <= features < 2**32
                        or not isinstance(sources, list) or len(sources) > 250
                        or any(not isinstance(x, str) or len(x) > 512 for x in sources)):
                    raise BridgeError("invalid_device")
            result[entity] = {"state": value["state"], "attributes": attrs}
        return result

    async def validate(self) -> dict[str, dict]:
        # IDs are restricted above, so these fixed expressions contain no user
        # template code. Only native Samsung membership and device equality leave
        # the remote template renderer, never arbitrary registry/state data.
        remote_check = (
            f"'{self.remote}' in integration_entities('samsungtv') and "
            f"device_id('{self.remote}') == device_id('{self.tv}')"
            if self.remote else "true"
        )
        template = (
            "{{ {'valid': '" + self.tv + "' in integration_entities('samsungtv') and "
            "device_id('" + self.tv + "') != none and (" + remote_check + ")} | to_json }}"
        )
        identity = await self.request("POST", "/api/template", {"template": template})
        if not isinstance(identity, dict) or identity.get("valid") is not True:
            raise BridgeError("invalid_device")
        return await self.fetch()

    async def launch_netflix(self, content_id: str) -> None:
        """Use only the configured main-instance action, with a numeric Netflix ID."""
        if not self.settings.get("main_ha_netflix") or not re.fullmatch(r"[0-9]{1,12}", content_id):
            raise BridgeError("not_allowed")
        result = await self.request(
            "POST", "/api/services/rest_command/cinema_netflix_wohnzimmer?return_response",
            {"content_id": content_id},
        )
        response = result.get("service_response") if isinstance(result, dict) else None
        status = response.get("status") if isinstance(response, dict) else None
        if type(status) is not int or not 200 <= status < 300:
            raise BridgeError()

    async def refresh_youtube_status(self) -> bool:
        """Update the optional YouTube capability and report whether it changed."""
        now = monotonic()
        if now < self._youtube_status_due:
            return False
        self._youtube_status_due = now + _YOUTUBE_STATUS_INTERVAL
        available = False
        try:
            async with asyncio.timeout(2):
                result = await self.request(
                    "POST", "/api/services/cinema_control/youtube_status?return_response",
                    {"entity_id": self.tv},
                )
                response = result.get("service_response") if isinstance(result, dict) else None
                available = isinstance(response, dict) and response.get("available") is True
        except (BridgeError, TimeoutError):
            # An older or unavailable main instance must not break the TV bridge.
            pass
        changed = self.youtube_available != available
        self.youtube_available = available
        return changed

    async def youtube_play(self, video_id: str) -> None:
        """Send a validated YouTube ID to the fixed main-instance service."""
        if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise BridgeError("not_allowed")
        try:
            result = await self.request(
                "POST", "/api/services/cinema_control/youtube_play?return_response",
                {"entity_id": self.tv, "video_id": video_id},
                timeout=20,
            )
        except BridgeError as err:
            if err.code == "invalid_device":
                raise BridgeError("youtube_not_accepted") from None
            raise
        response = result.get("service_response") if isinstance(result, dict) else None
        if not isinstance(response, dict) or response.get("accepted") is not True:
            raise BridgeError("youtube_not_accepted")

    async def call(self, domain: str, service: str, data: dict) -> None:
        """Allowlist every action and payload; the remote entity cannot be overridden."""
        payload = dict(data)
        if domain == "media_player" and service in TV_SERVICES:
            allowed = {"volume_set": {"volume_level"}, "volume_mute": {"is_volume_muted"},
                       "select_source": {"source"},
                       "play_media": {"media_content_type", "media_content_id"}}.get(service, set())
            if set(payload) != allowed:
                raise BridgeError("not_allowed")
            if service == "volume_set" and (type(payload["volume_level"]) not in (int, float)
                                              or not 0 <= payload["volume_level"] <= 1):
                raise BridgeError("not_allowed")
            if service == "volume_mute" and type(payload["is_volume_muted"]) is not bool:
                raise BridgeError("not_allowed")
            if service == "select_source" and (not isinstance(payload["source"], str)
                                                 or not 1 <= len(payload["source"]) <= 512):
                raise BridgeError("not_allowed")
            if service == "play_media" and payload != {
                "media_content_type": "app", "media_content_id": SAMSUNG_PLEX_APP_ID
            }:
                raise BridgeError("not_allowed")
            payload["entity_id"] = self.tv
        elif domain == "remote" and service == "send_command" and self.remote:
            if (set(payload) != {"command"} or not isinstance(payload["command"], str)
                    or payload["command"] not in KEYS):
                raise BridgeError("not_allowed")
            payload["entity_id"] = self.remote
        else:
            raise BridgeError("not_allowed")
        await self.request("POST", "/api/services/" + domain + "/" + service, payload)


class MainHACoordinator(DataUpdateCoordinator[dict[str, dict]]):
    def __init__(self, hass, entry, client: MainHAClient) -> None:
        super().__init__(hass, _LOGGER, name="Kinosteuerung Hauptinstanz", config_entry=entry,
                         update_interval=timedelta(seconds=5), always_update=False)
        self.client = client

    async def _async_update_data(self) -> dict[str, dict]:
        try:
            data = await self.client.fetch()
        except BridgeError as err:
            raise UpdateFailed(str(err)) from err
        if await self.client.refresh_youtube_status():
            manager = self.hass.data.get(DOMAIN, {}).get("manager")
            if manager is not None and not manager.closed:
                manager.notify()
        return data


class MainHAEntity(CoordinatorEntity[MainHACoordinator]):
    """A real local HA entity, governed by normal local entity permissions."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MainHACoordinator, entry, remote_id: str, suffix: str) -> None:
        super().__init__(coordinator)
        self.remote_id = remote_id
        self._attr_unique_id = f"{entry.entry_id}_{coordinator.client.source_id}_{suffix}"
        tv = coordinator.data.get(coordinator.client.tv, {})
        name = tv.get("attributes", {}).get("friendly_name", "Samsung-TV")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.client.source_id)}, name=f"Hauptinstanz · {name}",
            manufacturer="Home Assistant", model="Vorhandener Samsung-TV",
        )

    @property
    def record(self) -> dict:
        return (self.coordinator.data or {}).get(self.remote_id, {})

    @property
    def attributes(self) -> dict:
        return self.record.get("attributes", {})

    @property
    def available(self) -> bool:
        return super().available and self.record.get("state") not in (None, "unavailable", "unknown")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        data = self.hass.data[DOMAIN]
        ids = data.setdefault("bridged_entity_ids", set())
        ids.add(self.entity_id)
        self.async_on_remove(lambda: ids.discard(self.entity_id))
        client = self.coordinator.client
        if self.remote_id == client.tv:
            clients = data.setdefault("youtube_bridge_clients", {})
            clients[self.entity_id] = client
            self.async_on_remove(lambda: clients.pop(self.entity_id, None))

    async def send(self, domain: str, service: str, **data) -> None:
        if not self.available:
            raise BridgeError()
        await self.coordinator.client.call(domain, service, data)
        # The bounded five-second poll observes feedback independently. A slow
        # read must never turn an accepted command into a timeout/retry request.
