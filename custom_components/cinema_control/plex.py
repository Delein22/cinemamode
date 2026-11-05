"""Send Plex Companion commands through a configured local Plex Media Server."""

from __future__ import annotations

import asyncio
from ipaddress import IPv4Address, IPv4Network
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import uuid4
from xml.etree import ElementTree

from aiohttp import ClientError, ClientOSError, ClientSession, ClientTimeout, ServerDisconnectedError

from .models import CinemaError, entity_id, text

PROFILE_FILE = "cinema_plex.json"
LAN_NETWORKS = tuple(map(IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
_NUMERIC_ID = re.compile(r"[1-9][0-9]{0,17}")
_MACHINE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,512}")
_RESPONSE_LIMIT = 2 * 1024 * 1024


async def _send_once(request, handler):
    """Disable aiohttp's implicit connection retry for this state-changing GET."""
    try:
        return await handler(request)
    except (ClientOSError, ServerDisconnectedError):
        raise CinemaError("Plex hat den Titelstart nicht bestätigt.", "plex_unconfirmed") from None


class PlexLauncher:
    """A single local Plex server and its explicitly selected player."""

    def __init__(self, settings: dict) -> None:
        self.player = entity_id(settings["entity_id"])
        self.client_name = text(settings["client_name"], "Plex-Player")
        self.client_product = text(settings["client_product"], "Plex-Produkt")
        token = settings["token"]
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ValueError("Invalid Plex token")
        self._token = token

        raw_url = text(settings["server_url"], "Plex-Server", 256)
        url = urlsplit(raw_url)
        if (url.scheme != "http" or not url.hostname or url.path not in ("", "/")
                or url.username is not None or url.password is not None or url.query or url.fragment
                or any(char.isspace() for char in raw_url) or "\\" in raw_url):
            raise ValueError("Expected a local Plex HTTP origin")
        self._host = IPv4Address(url.hostname)
        if not any(self._host in network for network in LAN_NETWORKS):
            raise ValueError("Expected a private LAN address")
        self._port = url.port if url.port is not None else 32400
        if not 1 <= self._port <= 65535:
            raise ValueError("Invalid Plex port")
        self.server_url = f"http://{self._host}:{self._port}"

    @classmethod
    def load(cls, path: str) -> PlexLauncher | None:
        """Load the optional local configuration without exposing credentials."""
        try:
            with Path(path).open(encoding="utf-8") as file:
                raw = file.read(8193)
        except FileNotFoundError:
            return None
        if len(raw) > 8192:
            raise ValueError("Plex configuration exceeds size limit")
        settings = json.loads(raw)
        if not isinstance(settings, dict):
            raise ValueError("Invalid Plex configuration")
        return cls(settings)

    async def _request(
        self,
        session: ClientSession,
        headers: dict[str, str],
        path: str,
        *,
        method: str = "GET",
        params: dict | None = None,
        target: str | None = None,
    ) -> bytes:
        is_start = path == "/player/playback/playMedia"
        if target:
            headers = {**headers, "X-Plex-Target-Client-Identifier": target}
        try:
            async with session.request(
                method, self.server_url + path, params=params, headers=headers,
                allow_redirects=False, timeout=ClientTimeout(total=10 if is_start else 5),
                **({"middlewares": (_send_once,)} if is_start else {}),
            ) as response:
                if response.status in (401, 403):
                    raise CinemaError("Plex verweigert den Zugriff. Bitte den Plex-Token prüfen.", "plex_auth")
                if not 200 <= response.status < 300:
                    raise CinemaError(f"Plex hat die Anfrage abgelehnt (HTTP {response.status}).", "plex_rejected")
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    body.extend(chunk)
                    if len(body) > _RESPONSE_LIMIT:
                        raise CinemaError("Die Plex-Antwort ist zu groß.", "plex_response")
                return bytes(body)
        except (ClientError, TimeoutError, OSError, ValueError):
            if is_start:
                raise CinemaError("Plex hat den Titelstart nicht bestätigt.", "plex_unconfirmed") from None
            raise CinemaError("Der lokale Plex-Server antwortet nicht.", "plex_connection") from None

    async def _xml(
        self, session: ClientSession, headers: dict[str, str], path: str, **kwargs,
    ) -> ElementTree.Element:
        body = await self._request(session, headers, path, **kwargs)
        try:
            xml = body.decode("utf-8")
            if "\x00" in xml or re.search(r"<!DOCTYPE|<!ENTITY", xml, re.I):
                raise ValueError("Unexpected XML declaration")
            return ElementTree.fromstring(xml)
        except (ElementTree.ParseError, ValueError, LookupError):
            raise CinemaError("Plex hat eine ungültige Antwort gesendet.", "plex_response") from None

    async def _discover(self, session: ClientSession, headers: dict[str, str]) -> str:
        try:
            async with asyncio.timeout(12):
                while True:
                    clients = await self._xml(session, headers, "/clients")
                    matches = [
                        client for client in clients.findall("Server")
                        if client.get("name") == self.client_name and client.get("product") == self.client_product
                    ]
                    if len(matches) > 1:
                        raise CinemaError("Der Plex-Player ist nicht eindeutig.", "plex_player")
                    if matches:
                        player = matches[0]
                        target = player.get("machineIdentifier", "")
                        if not _MACHINE_ID.fullmatch(target) or "playback" not in (
                            item.strip() for item in player.get("protocolCapabilities", "").split(",")
                        ):
                            raise CinemaError("Der Plex-Player meldet keine Wiedergabesteuerung.", "plex_player")
                        return target
                    await asyncio.sleep(1)
        except TimeoutError:
            raise CinemaError("Der Plex-Player ist noch nicht erreichbar. Bitte Plex am TV öffnen.", "plex_player") from None

    async def launch(self, session: ClientSession, rating_key: str, *, restart: bool) -> bool:
        """Request playback and report whether PMS observes the exact film on that player."""
        if not isinstance(rating_key, str) or not _NUMERIC_ID.fullmatch(rating_key):
            raise CinemaError("Ungültige Plex-Filmkennung.")
        if session.trust_env or getattr(session, "_default_proxy", None) is not None:
            raise CinemaError("Für Plex ist eine direkte lokale Verbindung erforderlich.", "plex_connection")
        headers = {
            "X-Plex-Token": self._token,
            "X-Plex-Client-Identifier": str(uuid4()),
            "X-Plex-Device-Name": "Kinosteuerung",
            "X-Plex-Product": "Kinosteuerung",
            "Accept": "application/xml",
        }
        target = await self._discover(session, headers)
        await self._request(session, headers, "/player/timeline/poll", params={"wait": 0, "commandID": 1}, target=target)
        key = f"/library/metadata/{rating_key}"
        metadata = await self._xml(session, headers, key)
        video = next((item for item in metadata.findall("Video") if item.get("ratingKey") == rating_key), None)
        if video is None or video.get("type") != "movie":
            raise CinemaError("Der ausgewählte Plex-Film wurde auf diesem Server nicht gefunden.", "plex_media")
        offset = video.get("viewOffset", "0")
        if restart or not re.fullmatch(r"[0-9]{1,10}", offset):
            offset = "0"
        duration = video.get("duration", "")
        if re.fullmatch(r"[0-9]{1,10}", duration) and int(offset) >= int(duration):
            offset = "0"

        identity = await self._xml(session, headers, "/identity")
        server_id = identity.get("machineIdentifier", "")
        if not _MACHINE_ID.fullmatch(server_id):
            raise CinemaError("Plex meldet keine gültige Serverkennung.", "plex_response")
        queue = await self._xml(session, headers, "/playQueues", method="POST", params={
            "type": "video", "uri": f"server://{server_id}/com.plexapp.plugins.library{key}", "shuffle": 0,
        })
        queue_id = queue.get("playQueueID", "")
        if not _NUMERIC_ID.fullmatch(queue_id):
            raise CinemaError("Plex hat keine gültige Wiedergabeliste erstellt.", "plex_response")
        access = await self._xml(session, headers, "/security/token", params={"type": "delegation", "scope": "all"})
        delegated_token = access.get("token", "")
        if not _TOKEN.fullmatch(delegated_token):
            raise CinemaError("Plex hat keinen temporären Wiedergabezugang geliefert.", "plex_response")
        reply = await self._request(session, headers, "/player/playback/playMedia", target=target, params={
            "key": key,
            "containerKey": f"/playQueues/{queue_id}?window=100&own=1",
            "machineIdentifier": server_id,
            "address": str(self._host),
            "port": self._port,
            "protocol": "http",
            "providerIdentifier": "com.plexapp.plugins.library",
            "offset": offset,
            "type": "video",
            "commandID": 2,
            "token": delegated_token,
        })
        # Samsung may acknowledge with an unclosed XML Response element.
        code = re.search(rb"""<Response\b[^>]*\bcode=["']([0-9]{3})["']""", reply)
        if code and not 200 <= int(code[1]) < 300:
            raise CinemaError("Der Plex-Player hat den Titelstart abgelehnt.", "plex_rejected")

        try:
            async with asyncio.timeout(7):
                while True:
                    sessions = await self._xml(session, headers, "/status/sessions")
                    for item in sessions.findall("Video"):
                        player = item.find("Player")
                        if (item.get("ratingKey") == rating_key and player is not None
                                and player.get("machineIdentifier") == target and player.get("state") == "playing"):
                            return True
                    await asyncio.sleep(1)
        except (CinemaError, TimeoutError):
            return False
