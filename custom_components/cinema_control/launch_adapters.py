"""Fixed local launch adapters. Device responses supply data, never executable code.

Samsung event layout follows samsung-tv-ws-api's ChannelEmitCommand protocol.
https://github.com/xchwarze/samsung-tv-ws-api/blob/master/samsungtvws/remote.py
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import re
import shlex
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType
from homeassistant.components.media_player.const import MediaPlayerEntityFeature as F

from .const import PROVIDERS
from .diagnostics import _app_provider
from .models import CinemaError, content_url
from .samsung_disney import disney_title_id
from .samsung_youtube import _remote_url, samsung_youtube_trace_config, youtube_video_id

ADAPTER_IDS = frozenset({"existing", "samsung_ws", "android_url", "webos_app"})
_PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+")
_WEBOS_APPS = {"netflix": "netflix", "youtube": "youtube.leanback.v4", "disney": "com.disney.disneyplus-prod",
               "prime": "amazon", "plex": "cdp-30", "apple": "com.apple.appletv"}
_ACTION_KEYS = frozenset({"action_data", "actionData", "action_url", "actionUrl", "action_play_url", "metaTag"})
_CONTENT_KEYS = frozenset({"contentId", "content_id", "movieId", "videoId", "video_id", "id", "url", "uri"})
_YOUTUBE_TILE = re.compile(r"(?P<prefix>https://www\.youtube\.com/tv#play\?v=)?(?P<video>[A-Za-z0-9_-]{11})(?P<tail>&launch=launcher&deeplinkId=[0-9a-fA-F-]{36})")


def _existing_route(manager, room: dict, target: dict, provider: str) -> bool:
    if provider == "netflix":
        return manager._netflix_launcher(room, target) is not None
    if provider in ("disney", "prime"):
        return manager._disney_launcher(room, target) is not None
    if provider == "plex":
        return manager._plex_launcher(room, target) is not None
    if provider == "youtube":
        return manager._youtube_bridge(room, target) is not None or manager._youtube_launcher(room, target) is not None
    return False


def adapter_options(manager, room: dict, target: dict, app: str) -> list[dict]:
    provider = _app_provider(app)
    if provider not in PROVIDERS:
        return []
    platform = manager.platform(target["entity_id"])
    result = [{"id":"existing", "label":"Bisheriger Startweg", "can_test":_existing_route(manager, room, target, provider),
               "reason":"Nur verfügbar, wenn bereits ein passender Startweg eingerichtet ist."}]
    if platform == "samsungtv":
        access = manager.device_access.resolve(manager, room, target)
        result.append({"id":"samsung_ws", "label":"Samsung · aktuelle Vorschau", "can_test":bool(access and access.pairing),
                       "reason":"Benötigt eine erreichbare, gekoppelte Geräteverbindung und passende aktuelle Vorschau-Daten."})
    elif platform in ("androidtv", "androidtv_remote"):
        available = (manager.hass.services.has_service("androidtv", "adb_command") if platform == "androidtv"
                     else manager.has_feature(target["entity_id"], F.PLAY_MEDIA)
                     and manager.hass.services.has_service("media_player", "play_media"))
        result.append({"id":"android_url", "label":"Android / Google TV · Inhaltslink", "can_test":bool(available),
                       "reason":"Verwendet die vorhandene Home-Assistant-Anbindung."})
    elif platform == "webostv":
        result.append({"id":"webos_app", "label":"LG webOS · App-Startparameter", "can_test":manager.hass.services.has_service("webostv", "command"),
                       "reason":"App-Parameter werden je Streamingdienst am Gerät geprüft."})
    return result


def bind_preview_action(action: str, provider: str, url: str) -> str | None:
    """Substitute a title only in recognized device-provided formats, never guess one."""
    url = content_url(provider, url)
    if not isinstance(action, str) or len(action) > 32768:
        return None
    if provider == "youtube" and (match := _YOUTUBE_TILE.fullmatch(action)):
        return (match["prefix"] or "") + youtube_video_id(url) + match["tail"]
    prefix = "action_play_url&" if action.startswith("action_play_url&") else ""
    value = action[len(prefix):]
    if provider == "netflix" and re.fullmatch(r"https://(?:www\.)?netflix\.com/(?:watch|title)/[0-9]{1,12}(?:\?[^\s]{0,512})?", value):
        title = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
        return prefix + re.sub(r"(/(?:watch|title)/)[0-9]{1,12}", lambda match: match[1] + title, value, count=1)
    if provider == "youtube" and re.fullmatch(r"https://www\.youtube\.com/(?:watch\?v=[A-Za-z0-9_-]{11}|tv#play\?v=[A-Za-z0-9_-]{11})", value):
        return prefix + re.sub(r"(?<=[?&]v=)[A-Za-z0-9_-]{11}", youtube_video_id(url), value, count=1)
    if not action.lstrip().startswith(("{", "[")):
        return None
    try:
        payload = json.loads(action)
    except (ValueError, RecursionError):
        return None
    replacements = 0
    nodes = 0

    def replace(value, key="", depth=0):
        nonlocal replacements, nodes
        nodes += 1
        if depth > 12 or nodes > 1024:
            raise ValueError
        if isinstance(value, dict):
            return {item:replace(child, item, depth + 1) for item, child in value.items()}
        if isinstance(value, list):
            return [replace(child, key, depth + 1) for child in value]
        if isinstance(value, str) and key in _CONTENT_KEYS:
            candidate = None
            if provider == "netflix" and key != "id" and re.fullmatch(r"[0-9]{1,12}", value):
                candidate = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
            elif provider == "disney" and re.fullmatch(r"entity-[0-9a-fA-F-]{36}", value):
                candidate = disney_title_id(url)
            elif provider == "prime" and re.fullmatch(r"amzn1\.dv\.gti\.[0-9a-fA-F-]{40,50}", value):
                candidate = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
            elif provider == "youtube" and key != "id" and re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
                candidate = youtube_video_id(url)
            elif value.startswith("https://"):
                candidate = bind_preview_action(value, provider, url)
            if candidate is not None:
                replacements += 1
                return candidate
        # Preserve nested JSON's envelope; alter only recognized title-bearing fields.
        if isinstance(value, str) and key in ("payload", "values") and value.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(value)
            except (ValueError, RecursionError):
                return value
            return json.dumps(replace(decoded, key, depth + 1), separators=(",", ":"))
        return value

    try:
        updated = replace(payload)
    except (ValueError, RecursionError):
        return None
    return json.dumps(updated, separators=(",", ":")) if replacements else None


def preview_action(data: object, provider: str, app_id: str, url: str) -> str | None:
    """Read only action fields belonging to the installed app, with strict traversal limits."""
    candidates = set()
    nodes = 0

    def visit(value, matched=False, depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > 16384 or depth > 20:
            raise ValueError
        if isinstance(value, dict):
            identity = value.get("appId", value.get("app_id"))
            if isinstance(identity, str):
                matched = identity == app_id
            name = value.get("appName", value.get("name"))
            if isinstance(name, str) and _app_provider(name) is not None:
                matched = _app_provider(name) == provider
            for key, child in value.items():
                if matched and key in _ACTION_KEYS and isinstance(child, str):
                    transformed = bind_preview_action(child, provider, url)
                    if transformed is not None:
                        candidates.add(transformed)
                visit(child, matched, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, matched, depth + 1)
        elif isinstance(value, str) and len(value) <= 131072 and value.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(value)
            except (ValueError, RecursionError):
                return
            visit(decoded, matched, depth + 1)

    try:
        visit(data)
    except (ValueError, RecursionError):
        return None
    return next(iter(candidates)) if len(candidates) == 1 else None


async def _samsung_dispatch(manager, room, target, favorite):
    access = manager.device_access.resolve(manager, room, target)
    if access is None or access.pairing is None:
        raise CinemaError("Bitte zuerst die Geräteverbindung speichern und am Samsung zulassen.", "pairing_required")
    provider = favorite["provider"]
    app_ids, preview = set(), None
    try:
        async with asyncio.timeout(10):
            async with ClientSession(timeout=ClientTimeout(sock_connect=3), trust_env=False,
                                     trace_configs=[samsung_youtube_trace_config()]) as session:
                async with session.ws_connect(_remote_url(access.host, access.pairing[0], access.client_name, access.pairing[1]),
                                              ssl=False, max_msg_size=2 * 1024 * 1024) as ws:
                    connected = False
                    for _ in range(48):
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
                                await ws.send_json({"method":"ms.channel.emit", "params":{"event":query,"to":"host"}})
                            connected = True
                        elif connected and kind == "ed.installedApp.get":
                            data = event.get("data")
                            apps = data.get("data") if isinstance(data, dict) else None
                            if isinstance(apps, list) and len(apps) <= 512:
                                app_ids = {app["appId"] for app in apps if isinstance(app, dict)
                                           and isinstance(app.get("appId"), str) and len(app["appId"]) <= 120
                                           and _app_provider(str(app.get("name", ""))) == provider}
                        elif connected and kind in ("ed.edenApp.get", "ed.edenApp.update"):
                            preview = event.get("data")
                        if len(app_ids) == 1 and preview is not None:
                            app_id = next(iter(app_ids))
                            action = preview_action(preview, provider, app_id, favorite["url"])
                            if action is None:
                                raise CinemaError("Die Vorschau enthält noch kein eindeutig nutzbares Startformat für diese App. Bitte den Diagnosebericht herunterladen.", "start_format_unknown")
                            await ws.send_json({"method":"ms.channel.emit", "params":{"event":"ed.apps.launch","to":"host",
                                                "data":{"action_type":"DEEP_LINK","appId":app_id,"metaTag":action}}})
                            return
    except CinemaError:
        raise
    except (ClientError, TimeoutError, OSError, ValueError, RecursionError):
        pass
    raise CinemaError("Die vorhandene Samsung-Verbindung hat den Startversuch nicht abgeschlossen.", "transport_unavailable")


async def dispatch(manager, room: dict, target: dict, favorite: dict, adapter_id: str, context) -> dict:
    """One explicit title request. No looping, installation, power or arbitrary endpoints."""
    if adapter_id not in ADAPTER_IDS:
        raise CinemaError("Unbekannter Startweg.", "unsupported_adapter")
    provider = favorite.get("provider")
    if provider not in PROVIDERS:
        raise CinemaError("Für diese App fehlt ein unterstützter Inhaltslink.", "unsupported_adapter")
    favorite = deepcopy(favorite)
    favorite["url"] = content_url(provider, favorite.get("url"))
    option = next((item for item in adapter_options(manager, room, target, provider) if item["id"] == adapter_id), None)
    if not option or not option["can_test"]:
        raise CinemaError("Dieser Startweg ist für das gewählte Gerät derzeit nicht verfügbar.", "unsupported_adapter")
    manager._check_prepared(room, target)
    player = target["entity_id"]
    if adapter_id == "existing":
        job = manager.start(room["id"], target["id"], favorite["id"], provider, context, assistant_existing=True)
        if job.get("phase") == "failed":
            raise CinemaError("Der vorhandene Startweg ist fehlgeschlagen.", "start_failed")
    elif adapter_id == "samsung_ws":
        await _samsung_dispatch(manager, room, target, favorite)
    elif adapter_id == "android_url":
        if manager.platform(player) == "androidtv":
            package = target.get("apps", {}).get(provider, {}).get("app_id") or PROVIDERS[provider]["package"]
            if not isinstance(package, str) or not _PACKAGE.fullmatch(package):
                raise CinemaError("Bitte eine gültige Android-App-Kennung einrichten.", "invalid_app")
            await manager._service("androidtv", "adb_command", player, context,
                                   command="am start -a android.intent.action.VIEW -d " + shlex.quote(favorite["url"]) + " -p " + shlex.quote(package))
        else:
            await manager._service("media_player", "play_media", player, context,
                                   media_content_type="url", media_content_id=favorite["url"])
    elif adapter_id == "webos_app":
        await manager._service("webostv", "command", player, context, command="system.launcher/launch",
                               payload={"id":_WEBOS_APPS[provider], "params":{"contentTarget":favorite["url"]}})
    return {"accepted":True, "evidence_level":"command_sent", "adapter_id":adapter_id}
