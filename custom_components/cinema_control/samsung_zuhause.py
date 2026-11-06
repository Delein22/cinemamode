"""One explicit Netflix title request to the installed, opt-in Zuhause bridge."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from ipaddress import IPv4Address, IPv4Network
import json
import re
from urllib.parse import urlsplit
from uuid import uuid4

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType

from .device_access import TVAccess
from .models import CinemaError, content_url
from .samsung_youtube import _remote_url, samsung_youtube_pairing, samsung_youtube_trace_config

APP_ID = "BofenoHome.Zuhause"
_MIN_VERSION = (1, 4, 14)
_VERSION = re.compile(r"(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})")
_LAN = tuple(map(IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
_BODY_LIMIT = 32768


def _valid_access(access: TVAccess | None) -> bool:
    return (isinstance(access, TVAccess) and isinstance(access.host, IPv4Address)
            and any(access.host in network for network in _LAN))


async def async_probe_zuhause(access: TVAccess | None) -> dict:
    """Read only our fixed app's version; availability never confirms title playback."""
    check = {"id": "zuhause_bridge", "status": "unavailable", "reason": "invalid_device_access"}
    if not _valid_access(access):
        return check
    try:
        async with asyncio.timeout(4):
            async with ClientSession(timeout=ClientTimeout(total=3, sock_connect=2), trust_env=False,
                                     trace_configs=[samsung_youtube_trace_config()]) as session:
                async with session.get(f"http://{access.host}:8001/api/v2/applications/{APP_ID}",
                                       allow_redirects=False) as response:
                    check["http_status"] = response.status
                    if response.status != 200:
                        check["reason"] = "app_not_found" if response.status == 404 else "invalid_response"
                        return check
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(4096):
                        body.extend(chunk)
                        if len(body) > _BODY_LIMIT:
                            raise ValueError
                    data = json.loads(body)
                    if not isinstance(data, dict) or data.get("id") != APP_ID:
                        raise ValueError
                    version = data.get("version")
                    match = _VERSION.fullmatch(version) if isinstance(version, str) else None
                    if match is None:
                        raise ValueError
                    check["app_version"] = version
                    if tuple(map(int, match.groups())) < _MIN_VERSION:
                        check["reason"] = "version_unsupported"
                    else:
                        check.update(status="available", reason="complete")
                    return check
    except (ClientError, TimeoutError, OSError):
        check.update(status="unavailable", reason="connection_failed")
    except (ValueError, TypeError, RecursionError):
        check.update(status="unavailable", reason="invalid_response")
    return check


async def async_launch_zuhause_netflix(access: TVAccess | None, url: str,
                                      validate: Callable[[], None]) -> None:
    """Verify bridge presence, then send once through an existing Samsung pairing."""
    if not _valid_access(access):
        raise CinemaError("Für Zuhause fehlt eine gültige lokale Geräteverbindung.", "zuhause_not_available")
    pairing = (samsung_youtube_pairing({"port": access.pairing[0], "token": access.pairing[1]})
               if isinstance(access.pairing, tuple) and len(access.pairing) == 2 else None)
    if pairing is None:
        raise CinemaError("Bitte die vorhandene Samsung-Verbindung zuerst am Gerät zulassen.", "zuhause_pairing_required")
    try:
        title = urlsplit(content_url("netflix", url)).path.rstrip("/").rsplit("/", 1)[-1]
        if not re.fullmatch(r"[0-9]{1,12}", title):
            raise ValueError
    except (CinemaError, ValueError, TypeError):
        raise CinemaError("Für Zuhause wird ein gültiger Netflix-Titellink benötigt.", "zuhause_invalid_content") from None

    send_started = False
    try:
        async with asyncio.timeout(12):
            validate()
            check = await async_probe_zuhause(access)
            if check["status"] != "available":
                code = "zuhause_version_unsupported" if check["reason"] == "version_unsupported" else "zuhause_not_available"
                raise CinemaError("Zuhause ab Version 1.4.14 muss auf diesem Gerät installiert und erreichbar sein.", code)
            validate()
            async with ClientSession(timeout=ClientTimeout(sock_connect=3), trust_env=False,
                                     trace_configs=[samsung_youtube_trace_config()]) as session:
                async with session.ws_connect(_remote_url(access.host, pairing[0], access.client_name, pairing[1]),
                                              ssl=False, max_msg_size=128 * 1024) as ws:
                    for _ in range(32):
                        message = await ws.receive()
                        if message.type != WSMsgType.TEXT:
                            break
                        event = message.json()
                        if not isinstance(event, dict):
                            break
                        kind = event.get("event")
                        if kind in ("ms.channel.unauthorized", "ms.channel.timeOut"):
                            raise CinemaError("Die vorhandene Samsung-Verbindung benötigt eine erneute Gerätefreigabe.", "zuhause_pairing_required")
                        if kind == "ms.error":
                            break
                        if kind == "ms.channel.connect":
                            validate()
                            request = {"type": "zuhause.content.v1", "requestId": uuid4().hex,
                                       "provider": "netflix", "url": f"https://www.netflix.com/watch/{title}"}
                            send_started = True
                            await ws.send_json({"method": "ms.channel.emit", "params": {
                                "event": "ed.apps.launch", "to": "host", "data": {
                                    "appId": APP_ID, "action_type": "DEEP_LINK",
                                    "metaTag": json.dumps(request, separators=(",", ":"))}}})
                            return
    except CinemaError:
        raise
    except (ClientError, TimeoutError, OSError, ValueError, TypeError, RecursionError):
        if send_started:
            raise CinemaError("Der Ausgang des Zuhause-Aufrufs ist unklar. Bitte das Bild am TV prüfen.", "zuhause_start_unknown") from None
    raise CinemaError("Die vorhandene Samsung-Verbindung hat den Zuhause-Aufruf nicht abgeschlossen.", "zuhause_unavailable")
