"""Per-TV Netflix Preapp launches using only fresh, paired Samsung preview data."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from ipaddress import IPv4Address, IPv4Network
import json
import re
from urllib.parse import parse_qsl, quote_plus

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType

from .device_access import TVAccess
from .diagnostics import _app_provider
from .models import CinemaError
from .samsung_youtube import _remote_url, samsung_youtube_pairing, samsung_youtube_trace_config

_PREFIX = "action_play_url&"
_ENVELOPE_MARKER = "|NETFLIX_LAUNCH|"
_TITLE = re.compile(r"[0-9]{1,12}")
_ACTION_KEYS = frozenset({"action_data", "actionData", "action_url", "actionUrl", "action_play_url", "metaTag"})
_EDEN_EVENTS = frozenset({"ed.edenApp.get", "ed.edenApp.update"})
_LAN = tuple(map(IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
_FORMAT_MESSAGE = ("Dieser Fernseher liefert noch kein eindeutig unterstütztes Netflix-Startformat. "
                   "Bitte einen aktuellen Direktstart-Diagnosebericht erstellen.")


def _query(value: str) -> tuple[list[str], list[tuple[str, str]]]:
    if (not value or len(value) > 32768 or re.search(r"%(?![0-9a-fA-F]{2})", value)
            or any(ord(char) < 32 for char in value)):
        raise ValueError
    pairs = parse_qsl(value, keep_blank_values=True, strict_parsing=True, max_num_fields=64,
                      encoding="utf-8", errors="strict")
    parts = value.split("&")
    if (len(parts) != len(pairs) or len({key for key, _ in pairs}) != len(pairs)
            or any(not key or len(key) > 120 or any(ord(char) < 32 for char in key + item)
                   for key, item in pairs)):
        raise ValueError
    return parts, pairs


def _replace_query(parts: list[str], pairs: list[tuple[str, str]], changes: dict[str, str]) -> str:
    # Retain every unrelated field byte-for-byte, including the TV's private partner values.
    return "&".join(part.split("=", 1)[0] + "=" + quote_plus(changes[key]) if key in changes else part
                    for part, (key, _) in zip(parts, pairs, strict=True))


def _envelope_query(value: object) -> str | None:
    """Unwrap only the observed, bounded Smart Hub Netflix launch envelope."""
    if (not isinstance(value, str) or len(value) > 32768
            or value.count(_ENVELOPE_MARKER) != 1):
        return None
    name, query = value.split(_ENVELOPE_MARKER, 1)
    if (not 0 < len(name) <= 160 or "|" in name or not all(char.isprintable() for char in name)
            or not query):
        return None
    return query


def bind_netflix_action(action: object, content_id: str) -> str | None:
    """Replace only both title IDs in the known Preapp query; never invent device values."""
    if (not isinstance(action, str) or len(action) > 32768 or not isinstance(content_id, str)
            or not _TITLE.fullmatch(content_id)):
        return None
    envelope = _envelope_query(action)
    if _ENVELOPE_MARKER in action and envelope is None:
        return None
    value = envelope if envelope is not None else action
    value = value[len(_PREFIX):] if value.startswith(_PREFIX) else value
    try:
        parts, pairs = _query(value)
        outer = dict(pairs)
        inner_parts, inner_pairs = _query(outer["source_type_payload"])
        inner = dict(inner_pairs)
        if (outer.get("source_type") != "27" or not _TITLE.fullmatch(outer.get("m", ""))
                or inner.get("movieId") != outer["m"] or inner.get("action") != "mdp"
                or inner.get("category") != "Preapp" or inner.get("partnerId") != "PREAPP"
                or not inner.get("partnerVdid")):
            return None
        payload = _replace_query(inner_parts, inner_pairs, {"movieId": content_id})
        return _PREFIX + _replace_query(parts, pairs, {"m": content_id, "source_type_payload": payload})
    except (KeyError, ValueError, UnicodeError):
        return None


def netflix_preview_action(data: object, app_id: str, content_id: str) -> str | None:
    """Read recognized action fields belonging to Netflix, with bounded JSON traversal."""
    selected: str | None = None
    remaining = 16384

    def visit(value: object, scope: bool | None = None, depth: int = 0) -> None:
        nonlocal remaining, selected
        remaining -= 1
        if remaining < 0 or depth > 20:
            raise ValueError
        if isinstance(value, str) and scope is not False and _envelope_query(value) is not None:
            if selected is None:
                selected = bind_netflix_action(value, content_id)
        elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            if len(value) > 2 * 1024 * 1024:
                raise ValueError
            try:
                decoded = json.loads(value)
            except (ValueError, RecursionError):
                return
            visit(decoded, scope, depth + 1)
        elif isinstance(value, dict):
            identity = value.get("appId", value.get("app_id"))
            if identity is not None:
                scope = isinstance(identity, str) and identity in (app_id, "Netflix")
            elif scope is not False:
                name = value.get("appName", value.get("name"))
                if isinstance(name, str) and (provider := _app_provider(name)) is not None:
                    scope = provider == "netflix"
                if scope is not False and value.get("action_type") == "NETFLIX_LAUNCH":
                    scope = True
            for key, child in value.items():
                if (selected is None and scope is True and key in _ACTION_KEYS
                        and (bound := bind_netflix_action(child, content_id))):
                    selected = bound
                visit(child, scope, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, scope, depth + 1)

    try:
        visit(data)
    except (ValueError, RecursionError):
        return None
    # Several Netflix tiles normally carry different row/tracking values. The
    # first recognized tile supplies one complete current-device launch recipe.
    return selected


def _decoded(value: object) -> object:
    if isinstance(value, str):
        if len(value) > 2 * 1024 * 1024:
            raise ValueError
        return json.loads(value)
    return value


class SamsungNetflixLauncher:
    """A single selected TV; no HA REST action, stored preview or app-only fallback."""

    def __init__(self, access: TVAccess, *, validate: Callable[[], None]) -> None:
        self.access = access
        self.validate = validate

    async def _preview_body(self, session: ClientSession, content_id: str) -> str:
        access = self.access
        port, token = access.pairing
        app_ids: set[str] = set()
        preview = None
        apps_received = False
        async with session.ws_connect(
            _remote_url(access.host, port, access.client_name, token), ssl=False,
            max_msg_size=2 * 1024 * 1024,
        ) as ws:
            connected = False
            for _ in range(64):
                message = await ws.receive()
                if message.type != WSMsgType.TEXT:
                    break
                event = message.json()
                if not isinstance(event, dict):
                    continue
                kind = event.get("event")
                if kind in ("ms.error", "ms.channel.unauthorized", "ms.channel.timeOut"):
                    break
                if kind == "ms.channel.connect" and not connected:
                    for query in ("ed.installedApp.get", "ed.edenApp.get"):
                        await ws.send_json({"method": "ms.channel.emit", "params": {"event": query, "to": "host"}})
                    connected = True
                elif connected and kind == "ed.installedApp.get":
                    data = _decoded(event.get("data"))
                    apps = _decoded(data.get("data")) if isinstance(data, dict) else data
                    if not isinstance(apps, list) or len(apps) > 512:
                        raise ValueError
                    app_ids = {app["appId"] for app in apps if isinstance(app, dict)
                               and isinstance(app.get("appId"), str) and 0 < len(app["appId"]) <= 120
                               and isinstance(app.get("name"), str) and _app_provider(app["name"]) == "netflix"}
                    apps_received = True
                elif connected and kind in _EDEN_EVENTS:
                    preview = _decoded(event.get("data"))
                if apps_received and preview is not None:
                    if len(app_ids) == 1:
                        body = netflix_preview_action(preview, next(iter(app_ids)), content_id)
                        if body is not None:
                            return body
                    raise CinemaError(_FORMAT_MESSAGE, "netflix_start_format_unknown")
        raise CinemaError("Die vorhandene Samsung-Kopplung hat keine vollständige Netflix-Vorschau geliefert.",
                          "netflix_start_unavailable")

    async def launch_netflix(self, content_id: str) -> None:
        if not isinstance(content_id, str) or not _TITLE.fullmatch(content_id):
            raise CinemaError("Bitte einen gültigen Netflix-Inhaltslink verwenden.", "invalid_content")
        access = self.access
        pairing = access.pairing
        if (not isinstance(access.host, IPv4Address) or not any(access.host in network for network in _LAN)
                or not pairing or samsung_youtube_pairing({"port": pairing[0], "token": pairing[1]}) is None):
            raise CinemaError("Für diesen Fernseher fehlt eine vorhandene Samsung-Kopplung.",
                              "netflix_start_unavailable")
        self.validate()
        try:
            async with asyncio.timeout(12):
                async with ClientSession(timeout=ClientTimeout(total=12, sock_connect=3), trust_env=False,
                                         trace_configs=[samsung_youtube_trace_config()]) as session:
                    body = await self._preview_body(session, content_id)
                    self.validate()
                    async with session.post(
                        f"http://{access.host}:8080/ws/app/Netflix", data=body.encode("utf-8"),
                        headers={"Content-Type": "text/plain; charset=utf-8"}, allow_redirects=False,
                    ) as response:
                        if response.status not in (200, 201):
                            raise CinemaError("Der Fernseher hat den Netflix-Titelaufruf nicht angenommen.",
                                              "netflix_start_rejected")
        except CinemaError:
            raise
        except (ClientError, TimeoutError, OSError, ValueError, AttributeError, RecursionError):
            raise CinemaError("Die direkte Netflix-Verbindung zum gewählten Fernseher ist fehlgeschlagen.",
                              "netflix_start_unavailable") from None
