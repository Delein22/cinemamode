"""Open a known Samsung app without requiring a Home Assistant source list.

Every command follows a fresh, matching app-status response. This sends at most
one app-only POST and does not claim that a film or series has started.
REST status/run: samsung-tv-ws-api/samsungtvws/rest.py.
DIAL discovery/status/empty launch: ConnectSDK DIALService.java.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from aiohttp import ClientError, ClientSession, ClientTimeout

from .device_access import TVAccess
from .models import CinemaError
from .samsung_http_probe import (
    _private, dial_app_url, parse_dial_app_state, parse_dial_service,
    parse_rest_app_state, rest_app_ids, rest_app_url,
)
from .samsung_youtube import samsung_youtube_trace_config

_BODY_LIMIT = 32 * 1024
_PREFLIGHT_ERRORS = (ClientError, TimeoutError, OSError, ValueError, TypeError)


async def _get(session, url: str) -> tuple[int, bytes, str | None]:
    async with session.get(url, allow_redirects=False, timeout=ClientTimeout(total=2, sock_connect=1)) as response:
        body = bytearray()
        async for chunk in response.content.iter_chunked(4096):
            body.extend(chunk)
            if len(body) > _BODY_LIMIT:
                raise ValueError
        return response.status, bytes(body), response.headers.get("Application-URL")


async def async_open_samsung_app(access: TVAccess | None, provider: str, validate: Callable[[], None]) -> dict:
    """Use only fixed public IDs, exact same-TV URLs and one explicit app open."""
    if (not isinstance(access, TVAccess) or not _private(access.host)
            or not isinstance(provider, str) or not rest_app_ids(provider) or not callable(validate)):
        raise CinemaError("Für diese App fehlt eine unterstützte Samsung-Geräteverbindung.", "samsung_app_not_supported")
    post_started = False
    try:
        validate()
        async with asyncio.timeout(10):
            async with ClientSession(timeout=ClientTimeout(total=3, sock_connect=1), trust_env=False,
                                     trace_configs=[samsung_youtube_trace_config()]) as session:
                async def dial_target() -> tuple[str, str, str] | None:
                    if provider not in ("netflix", "youtube"):
                        return None
                    try:
                        validate()
                        status, _body, advertised = await _get(session, f"http://{access.host}:7678/nservice/")
                        service = parse_dial_service(access.host, advertised) if status == 200 else None
                        url = dial_app_url(access.host, service, provider) if service else None
                        if url is None:
                            return None
                        validate()
                        status, body, _ = await _get(session, url)
                        if status == 200 and parse_dial_app_state(body, provider) is not None:
                            return url, "dial", "Netflix" if provider == "netflix" else "YouTube"
                    except _PREFLIGHT_ERRORS:
                        pass
                    return None

                async def rest_target() -> tuple[str, str, str] | None:
                    for app_id in rest_app_ids(provider):
                        url = rest_app_url(access.host, provider, app_id)
                        try:
                            validate()
                            status, body, _ = await _get(session, url)
                            if status == 200 and parse_rest_app_state(body, provider, app_id) is not None:
                                return url, "rest", app_id
                        except _PREFLIGHT_ERRORS:
                            pass
                    return None

                if provider == "netflix":
                    target = await dial_target() or await rest_target()
                else:
                    target = await rest_target() or await dial_target()
                if target is None:
                    raise CinemaError("Der Fernseher hat für diese App keinen passenden Startdienst bestätigt.",
                                      "samsung_app_not_supported")
                url, route, public_id = target
                validate()
                # Any response loss after this point is uncertain. Never retry a
                # second app-opening command through another route automatically.
                post_started = True
                kwargs = {"allow_redirects": False, "timeout": ClientTimeout(total=3, sock_connect=1), "data": b""}
                if route == "dial":
                    kwargs["headers"] = {"Content-Type": 'text/plain; charset="utf-8"'}
                async with session.post(url, **kwargs) as response:
                    if response.status not in (200, 201):
                        raise CinemaError("Der Fernseher hat den App-Start abgelehnt. Bitte das Bild am TV prüfen.",
                                          "samsung_app_start_rejected")
                return {"accepted": True, "route": route, "app_id": public_id,
                        "status": "command_sent", "evidence_level": "command_sent"}
    except CinemaError:
        raise
    except _PREFLIGHT_ERRORS:
        if post_started:
            raise CinemaError("Der Ausgang des App-Starts ist unklar. Bitte das Bild am TV prüfen.",
                              "samsung_app_start_unknown") from None
        raise CinemaError("Der App-Startdienst des Fernsehers ist zurzeit nicht erreichbar.",
                          "samsung_app_unavailable") from None
