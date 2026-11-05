"""One Netflix DIAL title trial independent of Samsung's Home preview.

The JSON content parameter and text/plain envelope follow ConnectSDK's
DIALService.launchNetflix / sendCommand implementation. Acceptance is not
evidence of playback; the setup assistant requires the user's observation.
https://github.com/ConnectSDK/Connect-SDK-Android-Core/blob/master/src/com/connectsdk/service/DIALService.java
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from ipaddress import IPv4Address, IPv4Network
import json
import re

from aiohttp import ClientError, ClientSession, ClientTimeout

from .device_access import TVAccess
from .models import CinemaError
from .samsung_http_probe import dial_app_url, parse_dial_app_state, parse_dial_service
from .samsung_youtube import samsung_youtube_trace_config

_LAN = tuple(map(IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
_BODY_LIMIT = 32768


async def _get(session, url: str) -> tuple[int, bytes, str | None]:
    async with session.get(url, allow_redirects=False, timeout=ClientTimeout(total=3)) as response:
        body = bytearray()
        async for chunk in response.content.iter_chunked(4096):
            body.extend(chunk)
            if len(body) > _BODY_LIMIT:
                raise ValueError
        return response.status, bytes(body), response.headers.get("Application-URL")


async def async_launch_netflix_dial(access: TVAccess | None, content_id: str, validate: Callable[[], None]) -> None:
    """Fresh discovery, exact-app status, then one explicit content request."""
    if (access is None or not isinstance(access.host, IPv4Address)
            or not any(access.host in network for network in _LAN)
            or not isinstance(content_id, str) or not re.fullmatch(r"[0-9]{1,12}", content_id)):
        raise CinemaError("Für diesen DIAL-Test fehlt eine gültige Geräteadresse oder Netflix-Titelkennung.", "dial_not_supported")
    post_started = False
    try:
        async with asyncio.timeout(10):
            async with ClientSession(timeout=ClientTimeout(total=3, sock_connect=2), trust_env=False,
                                     trace_configs=[samsung_youtube_trace_config()]) as session:
                validate()
                status, _, advertised = await _get(session, f"http://{access.host}:7678/nservice/")
                service = parse_dial_service(access.host, advertised) if status == 200 else None
                url = dial_app_url(access.host, service, "netflix") if service else None
                if url is None:
                    raise CinemaError("Der Fernseher bietet hier keinen unterstützten Netflix-DIAL-Dienst an.", "dial_not_supported")
                validate()
                status, body, _ = await _get(session, url)
                if status != 200 or parse_dial_app_state(body, "netflix") is None:
                    raise CinemaError("Der Netflix-DIAL-Dienst hat keinen passenden App-Status geliefert.", "dial_not_supported")
                validate()
                post_started = True
                async with session.post(url, allow_redirects=False, timeout=ClientTimeout(total=3),
                                        headers={"Content-Type": 'text/plain; charset="utf-8"'},
                                        data=json.dumps({"v": content_id}, separators=(",", ":")).encode("utf-8")) as response:
                    if response.status not in (200, 201):
                        raise CinemaError("Der Fernseher hat den Netflix-DIAL-Titelaufruf abgelehnt. Bitte das Bild am TV prüfen.",
                                          "dial_start_rejected")
    except CinemaError:
        raise
    except (ClientError, TimeoutError, OSError, ValueError, TypeError):
        if post_started:
            raise CinemaError("Der Ausgang des Netflix-DIAL-Aufrufs ist unklar. Bitte das Bild am TV prüfen.", "dial_start_unknown") from None
        raise CinemaError("Der Netflix-DIAL-Dienst ist für diesen Test nicht erreichbar.", "dial_unavailable") from None
