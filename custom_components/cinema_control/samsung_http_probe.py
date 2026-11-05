"""Bounded, read-only Samsung app checks independent of Smart Hub previews.

Only public app identifiers and fixed result codes leave this module. A running
app is service evidence, never evidence that a requested title is playing.
"""

from __future__ import annotations

import asyncio
from ipaddress import IPv4Address, IPv4Network
import json
from urllib.parse import urlsplit
from xml.etree import ElementTree

from aiohttp import ClientError, ClientTimeout


_LAN = tuple(map(IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
_MAX_BODY = 32 * 1024
_DIAL_NAMES = {"netflix": "Netflix", "youtube": "YouTube"}
_DIAL_ROUTES = {"ws_app": "/ws/app/", "ws_apps": "/ws/apps/", "apps": "/apps/"}
# Public identifiers already observed by the integration's Samsung capture.
# Failure to find one is not proof that an app is absent on another model.
_REST_APPS = {
    "netflix": ("11101200001", frozenset({"netflix"})),
    "youtube": ("111299001912", frozenset({"youtube", "youtube on tv"})),
    "disney": ("3201901017640", frozenset({"disney+", "disney plus", "disneyplus"})),
    "prime": ("org.tizen.ignition", frozenset({"prime video", "amazon prime video", "amazon video"})),
    "plex": ("3201512006963", frozenset({"plex"})),
}


def _private(host: object) -> bool:
    return isinstance(host, IPv4Address) and any(host in network for network in _LAN)


def parse_dial_service(host: IPv4Address, advertised_url: object) -> dict | None:
    """Accept only the selected private TV and exact known application routes."""
    if (not _private(host) or not isinstance(advertised_url, str) or len(advertised_url) > 512
            or any(ord(char) < 33 or ord(char) > 126 for char in advertised_url)):
        return None
    try:
        parsed = urlsplit(advertised_url)
        route = next((name for name, path in _DIAL_ROUTES.items()
                      if parsed.path in (path, path.rstrip("/"))), None)
        if (parsed.scheme != "http" or parsed.hostname != str(host)
                or parsed.port not in (8001, 8002, 8080) or not route
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            return None
        return {"port": parsed.port, "route": route}
    except ValueError:
        return None


def dial_app_url(host: IPv4Address, service: object, provider: str) -> str | None:
    """Build a fixed same-TV URL; arbitrary paths and app names are never used."""
    if (not _private(host) or not isinstance(service, dict)
            or not isinstance(provider, str) or provider not in _DIAL_NAMES):
        return None
    port, route = service.get("port"), service.get("route")
    if (type(port) is not int or port not in (8001, 8002, 8080)
            or not isinstance(route, str) or route not in _DIAL_ROUTES):
        return None
    return f"http://{host}:{port}{_DIAL_ROUTES[route]}{_DIAL_NAMES[provider]}"


def parse_dial_app_state(body: object, provider: str) -> str | None:
    """Require a DIAL service response with the exact selected public app name."""
    if (not isinstance(provider, str) or provider not in _DIAL_NAMES
            or not isinstance(body, bytes) or len(body) > _MAX_BODY
            or b"<!" in body or b"\x00" in body):
        return None
    try:
        root = ElementTree.fromstring(body)
    except (ElementTree.ParseError, ValueError):
        return None
    namespace = "{urn:dial-multiscreen-org:schemas:dial}"
    if root.tag not in ("service", namespace + "service"):
        return None
    name = root.find(namespace + "name") if root.tag.startswith("{") else root.find("name")
    state = root.find(namespace + "state") if root.tag.startswith("{") else root.find("state")
    if (name is None or state is None or not isinstance(name.text, str)
            or name.text.strip().casefold() != _DIAL_NAMES[provider].casefold()
            or not isinstance(state.text, str)):
        return None
    value = state.text.strip().casefold()
    return value if value in ("running", "stopped") else None


def _parse_rest_state(body: bytes, provider: str) -> str | None:
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    app_id, names = _REST_APPS[provider]
    identifier = value.get("id", value.get("appId"))
    name = value.get("name")
    if identifier is not None and identifier != app_id:
        return None
    if identifier != app_id and (not isinstance(name, str) or name.strip().casefold() not in names):
        return None
    running = value.get("running")
    return ("running" if running else "stopped") if type(running) is bool else None


async def _read(session, url: str) -> tuple[int, bytes]:
    async with session.get(url, allow_redirects=False, ssl=False,
                           timeout=ClientTimeout(total=2, sock_connect=1)) as response:
        body = bytearray()
        async for chunk in response.content.iter_chunked(4096):
            body.extend(chunk)
            if len(body) > _MAX_BODY:
                raise ValueError
        return response.status, bytes(body)


async def async_probe_samsung_apps(session, host: IPv4Address, service: dict | None) -> list[dict]:
    """At most seven GETs, two concurrent, within six seconds total.

    The caller owns a session with trust_env=False. Redirects stay disabled here;
    credentials are neither used nor forwarded by these public status requests.
    """
    if not _private(host):
        return []
    requests = []
    for provider in _DIAL_NAMES:
        url = dial_app_url(host, service, provider)
        if url is not None:
            requests.append((provider, "dial", service["port"], url))
    for provider, (identifier, _names) in _REST_APPS.items():
        requests.append((provider, "rest", 8001, f"http://{host}:8001/api/v2/applications/{identifier}"))
    records = [{"id": "samsung_app_status", "app": app, "protocol": protocol, "port": port,
                "status": "unavailable", "state": "unknown", "reason": "budget_exhausted", "attempted": False}
               for app, protocol, port, _url in requests]
    gate = asyncio.Semaphore(2)

    async def probe(index: int) -> None:
        provider, protocol, _port, url = requests[index]
        record = records[index]
        try:
            async with gate:
                record["attempted"] = True
                status, body = await _read(session, url)
            record["http_status"] = status
            if status != 200:
                record.update(status="not_supported" if status in (404, 405, 501) else "unavailable",
                              reason="endpoint_not_supported" if status in (404, 405, 501) else
                              "http_denied" if status in (401, 403) else
                              "redirect_rejected" if 300 <= status < 400 else "http_error")
                return
            state = parse_dial_app_state(body, provider) if protocol == "dial" else _parse_rest_state(body, provider)
            if state is None:
                record["reason"] = "invalid_response"
            else:
                record.update(status="available", state=state, reason="complete")
        except TimeoutError:
            record["reason"] = "response_timeout"
        except (ValueError, TypeError, AttributeError, RecursionError):
            record["reason"] = "invalid_response"
        except (ClientError, OSError):
            record["reason"] = "connection_failed"

    try:
        async with asyncio.timeout(6):
            await asyncio.gather(*(probe(index) for index in range(len(requests))))
    except TimeoutError:
        pass
    return records
