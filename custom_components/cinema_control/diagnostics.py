"""Bounded, passive beta reports. No launch commands or raw device logs leave here."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from ipaddress import IPv4Address, IPv4Network
import json
import re
from urllib.parse import parse_qsl, urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN, PROVIDERS, VERSION
from .models import CinemaError
from .device_access import AccessSource, TVAccess
from .samsung_capture import SamsungCapture, async_capture_samsung
from .samsung_http_probe import async_probe_samsung_apps, parse_dial_service
from .samsung_zuhause import async_probe_zuhause
from .samsung_youtube import (
    SAMSUNG_DEFAULT_CLIENT_NAME, samsung_youtube_pairing, samsung_youtube_trace_config,
)

OBSERVATIONS = frozenset({"not_tested", "app_only", "profile_selection", "title_page", "playing", "failed"})
EVIDENCE_LEVELS = ("not_checked", "access_blocked", "service_detected", "start_format_detected",
                   "command_accepted", "playback_confirmed")
_PLATFORMS = frozenset({"samsungtv", "webostv", "androidtv", "androidtv_remote", "roku", "cast", "apple_tv"})
_PROFILES = _PLATFORMS | {"generic"}
_CAPABILITIES = frozenset({"source", "play", "pause", "stop", "seek_start", "restart_stream",
                           "netflix_title", "disney_title", "prime_title", "plex_title", "youtube_title",
                           "youtube_via_main", "youtube_pairable"})
_LAN = tuple(map(IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
_MAX_BODY = 128 * 1024
_METADATA = re.compile(r"[\w .+()/-]{1,120}", re.ASCII)
_SENSITIVE = re.compile(
    r"(?:https?://|\b(?:bearer|token|password|secret|authorization)\b|"
    r"(?:\d{1,3}\.){3}\d{1,3}|(?:[a-f0-9]{2}[:-]){5}[a-f0-9]{2}|"
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|"
    r"\b(?:media_player|remote)\.)", re.I,
)
_FIELDS = frozenset({"action", "action_data", "actionData", "action_url", "actionUrl", "appId", "app_id",
                     "contentId", "content_id", "deeplink", "deepLink", "id", "launch", "launchMode",
                     "metaData", "metaTag", "payload", "uri", "url", "values", "action_play_url",
                     "module", "func", "page", "placement", "placementDetail", "distributionPartner",
                     "checkUpdate", "amazonType", "videoId"})
_QUERY_KEYS = frozenset({"v", "launch", "deeplinkId", "contentId", "action", "source", "source_type",
                         "t", "appId", "mediaType", "type"})
_YOUTUBE_PREVIEW = re.compile(
    r"(?:https://www\.youtube\.com/tv#play\?v=)?[A-Za-z0-9_-]{11}"
    r"&launch=launcher&deeplinkId=[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}"
    r"-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
)
_APP_ALIASES = {
    "netflix": "netflix", "disney": "disney", "disney+": "disney", "disneyplus": "disney",
    "primevideo": "prime", "amazonprimevideo": "prime", "amazonvideo": "prime",
    "youtube": "youtube", "plex": "plex", "appletv": "apple", "appletv+": "apple",
}
_PUBLIC_APP_IDS = {"11101200001": "netflix", "3201901017640": "disney", "org.tizen.ignition": "prime",
                   "111299001912": "youtube", "3201512006963": "plex", "3201907018807": "netflix",
                   "3201512006785": "prime", "3201910019365": "prime", "3201807016597": "apple",
                   "3201907018746": "magenta", "3201908018988": "rtl"}
_PUBLIC_CONSTANTS = {"module": {"LaunchHelper"}, "func": {"deeplink", "native", "launch"},
                     "action": {"DEEP_LINK", "NATIVE_LAUNCH", "action_play_url"},
                     "launch": {"launcher"}, "page": {"browse", "detail", "play"},
                     "placement": {"dedicated_row"}, "placementDetail": {"eden_row"},
                     "distributionPartner": {"samsung"}}
_CHECK_SOURCES = {"samsung_device_api": "samsung_http_api", "dial_discovery": "dial_discovery",
                  "smart_hub_preview": "samsung_eden", "samsung_app_status": "samsung_http_app_status",
                  "manufacturer_protocol": "integration_capabilities", "zuhause_bridge": "zuhause_http_app_status"}


def _record_check(report: dict, check: dict) -> None:
    source = _CHECK_SOURCES.get(check["id"], "collector")
    if check["id"] == "device_metadata":
        source = "ha_main_device_registry" if check.get("source") == "main_instance" else "ha_device_registry"
    check["capture"] = {"source": source, "observed_at": datetime.now(UTC).isoformat()}
    level = "not_checked"
    if check["id"] in ("samsung_device_api", "dial_discovery", "smart_hub_preview"):
        if check["status"] == "available":
            known_patterns = {"youtube_launcher_with_ephemeral_id", "netflix_watch_url", "action_play_url"}
            has_format = check.get("templates") or known_patterns.intersection(check.get("payload_patterns", {}))
            level = "start_format_detected" if has_format else "service_detected"
        elif (check["status"] == "unavailable" and not
              (check["id"] == "smart_hub_preview" and check.get("reason") == "response_timeout"
               and report.get("connection", {}).get("authorization") == "verified")):
            level = "access_blocked"
    elif check["id"] in ("samsung_app_status", "zuhause_bridge") and check["status"] == "available":
        level = "service_detected"
    check["evidence_level"] = level
    report["checks"].append(check)


def _public_text(value: object) -> str | None:
    """Keep public product/version text only; never echo arbitrary state strings."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if (_METADATA.fullmatch(value) and not _SENSITIVE.search(value)
                     and not re.fullmatch(r"[a-f0-9]{32,}", value, re.I)) else None


def _private_host(value: object) -> IPv4Address | None:
    try:
        host = IPv4Address(value)
    except (ValueError, TypeError):
        return None
    return host if any(host in network for network in _LAN) else None


def _app_provider(app: str) -> str | None:
    return app if app in PROVIDERS else _APP_ALIASES.get(re.sub(r"[\s_-]+", "", app.casefold()))


def _validate_app(manager, target: dict, app: object) -> str:
    if (not isinstance(app, str) or not app or len(app) > 512
            or re.fullmatch(r"(?:TV|HDMI(?:[\s-]*\d+)?)", app.strip(), re.I)):
        raise CinemaError("Bitte eine vorhandene App für den Bericht wählen.", "invalid_app")
    state = manager.hass.states.get(target["entity_id"])
    sources = state.attributes.get("source_list", []) if state is not None else []
    allowed = set(PROVIDERS) | set(target.get("apps", {}))
    if isinstance(sources, list):
        allowed.update(item for item in sources[:250] if isinstance(item, str) and len(item) <= 512)
    if app not in allowed:
        raise CinemaError("Bitte eine vorhandene App für den Bericht wählen.", "invalid_app")
    return app


def _device_values(values: object) -> dict:
    values = values if isinstance(values, dict) else {}
    result = {key: _public_text(values.get(key)) for key in ("manufacturer", "model", "firmware")}
    unknown = {"unknown", "unavailable", "none", "null", "n/a", "unbekannt", "nicht verfügbar"}
    return {key: None if value is not None and value.casefold() in unknown else value for key, value in result.items()}


def _model_identity(expected: object, observed: object) -> bool | None:
    """Compare specific public model codes, not names or unique device identifiers."""
    def specific(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        model = re.sub(r"[^A-Z0-9]", "", value.upper(), flags=re.ASCII)
        if model.startswith("SAMSUNG"):
            model = model[7:]
        letters = re.sub(r"[0-9]", "", model)
        generic = {"TV", "SMARTTV", "SMARTMONITOR", "MONITOR", "FERNSEHER", "UNKNOWN", "UNBEKANNT"}
        if (len(model) < 8 or not re.search(r"[0-9]", model) or letters in generic
                or model.startswith(("SMARTTV", "SMARTMONITOR", "GENERIC", "VORHANDENER"))):
            return None
        return model

    left, right = specific(expected), specific(observed)
    if left is None or right is None:
        return None
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    suffix = longer[len(shorter):] if longer.startswith(shorter) else ""
    return bool(suffix and re.fullmatch(r"[A-Z]{1,8}", suffix))


async def _device_metadata(manager, player: str, bridge) -> tuple[dict, str]:
    if bridge is not None:
        # Entity IDs originate from the existing validated bridge configuration.
        entity = getattr(bridge, "tv", None)
        if not isinstance(entity, str) or not re.fullmatch(r"media_player\.[a-z0-9_]+", entity):
            return _device_values({}), "unavailable"
        template = "{{ {'manufacturer': device_attr('" + entity + "', 'manufacturer'), 'model': device_attr('" + entity + "', 'model'), 'firmware': device_attr('" + entity + "', 'sw_version')} | to_json }}"
        async with asyncio.timeout(4):
            values = await bridge.request("POST", "/api/template", {"template": template}, timeout=3)
        return _device_values(values), "main_instance"
    registered = er.async_get(manager.hass).async_get(player)
    device = dr.async_get(manager.hass).async_get(registered.device_id) if registered and registered.device_id else None
    return _device_values({"manufacturer": getattr(device, "manufacturer", None),
                           "model": getattr(device, "model", None), "firmware": getattr(device, "sw_version", None)}), "local_registry"


def _samsung_connection(manager, room: dict, target: dict) -> tuple[IPv4Address | None, tuple | None, str]:
    """Locate only the selected TV, using existing configuration/pairing internally."""
    device_access = getattr(manager, "device_access", None)
    if device_access is not None:
        access = device_access.resolve(manager, room, target)
        return (access.host, access.pairing, access.client_name) if access else (None, None, SAMSUNG_DEFAULT_CLIENT_NAME)
    host, pairing, name = None, None, SAMSUNG_DEFAULT_CLIENT_NAME
    registered = er.async_get(manager.hass).async_get(target["entity_id"])
    if registered and registered.config_entry_id:
        entry = manager.hass.config_entries.async_get_entry(registered.config_entry_id)
        if entry and entry.domain == "samsungtv":
            host = _private_host(entry.data.get("host"))
            if entry.data.get("method") == "websocket":
                pairing = samsung_youtube_pairing(entry.data)
    return host, pairing, name


async def _bounded_get(session, url: str) -> tuple[int, bytes, str | None]:
    async with session.get(url, allow_redirects=False, timeout=ClientTimeout(total=2)) as response:
        body = bytearray()
        async for chunk in response.content.iter_chunked(4096):
            body.extend(chunk)
            if len(body) > _MAX_BODY:
                raise ValueError("Oversized device response")
        return response.status, bytes(body), response.headers.get("Application-URL")


async def _samsung_http(session, host: IPv4Address, report: dict) -> None:
    application_service = None
    try:
        status, body, _ = await _bounded_get(session, f"http://{host}:8001/api/v2/")
        check = {"id": "samsung_device_api", "status": "available" if status == 200 else "unavailable", "http_status": status}
        if status == 200:
            payload = json.loads(body)
            device = payload.get("device", {}) if isinstance(payload, dict) else {}
            if isinstance(device, dict):
                values = _device_values({"manufacturer": "Samsung", "model": device.get("modelName"),
                                         "firmware": device.get("firmwareVersion") or device.get("firmware_version")})
                identity_match = _model_identity(report["device"].get("model"), values.get("model"))
                report["identity_match"] = identity_match
                check["identity_scope"] = "public_model"
                check["identity_match"] = identity_match
                if identity_match is False:
                    check.update(status="identity_mismatch", reason="device_identity_mismatch")
                    if "device_identity_mismatch" not in report["limitations"]:
                        report["limitations"].append("device_identity_mismatch")
                else:
                    expected_firmware, observed_firmware = report["device"].get("firmware"), values.get("firmware")
                    if expected_firmware and observed_firmware and expected_firmware != observed_firmware:
                        check["firmware_disagreement"] = True
                        report["limitations"].append("firmware_metadata_disagreement")
                    for key, value in values.items():
                        if value is not None and report["device"].get(key) is None:
                            report["device"][key] = value
        _record_check(report, check)
    except (ClientError, TimeoutError, OSError, ValueError, RecursionError):
        _record_check(report, {"id": "samsung_device_api", "status": "unavailable"})
    try:
        status, _, application_url = await _bounded_get(session, f"http://{host}:7678/nservice/")
        check = {"id": "dial_discovery", "status": "available" if status == 200 else "unavailable", "http_status": status}
        if status == 200 and isinstance(application_url, str) and len(application_url) <= 512:
            application_service = parse_dial_service(host, application_url)
            if application_service is not None:
                check["application_service"] = dict(application_service)
        _record_check(report, check)
    except (ClientError, TimeoutError, OSError, ValueError):
        _record_check(report, {"id": "dial_discovery", "status": "unavailable"})
    if report.get("identity_match") is not False:
        # These status requests do not need the optional EDEN or app-list replies.
        # They never launch an app and never establish content compatibility.
        for check in await async_probe_samsung_apps(session, host, application_service):
            _record_check(report, check)


def _template_layout(value: object, key: str = "", depth: int = 0) -> object:
    """Preserve only known layout keys/constants; replace every content/session value."""
    if depth > 6:
        return "{nested_value}"
    if isinstance(value, dict):
        result = {name: _template_layout(child, name, depth + 1) for name, child in value.items() if name in _FIELDS}
        return result or "{object}"
    if isinstance(value, list):
        return [_template_layout(child, key, depth + 1) for child in value[:2]]
    if isinstance(value, str):
        if key in ("appId", "app_id"):
            return value if value in _PUBLIC_APP_IDS else "{app_id}"
        if value in _PUBLIC_CONSTANTS.get(key, set()):
            return value
        if _YOUTUBE_PREVIEW.fullmatch(value):
            prefix = "https://www.youtube.com/tv#play?v=" if value.startswith("https://") else ""
            return prefix + "{video_id}&launch=launcher&deeplinkId={ephemeral_start_id}"
        if re.fullmatch(r"https://(?:www\.)?netflix\.com/watch/[0-9]{1,12}(?:\?[^\s]*)?", value):
            return "https://www.netflix.com/watch/{content_id}"
        if value.startswith("action_play_url&"):
            return "action_play_url&{content_url}"
        if re.match(r"\s*--deepLinkParameter=detail\?gti=", value):
            return "--deepLinkParameter=detail?gti={content_id}&refMarker={placement}"
        if value.lstrip().startswith(("{", "[")):
            try:
                return {"encoding": "json_string", "layout": _template_layout(json.loads(value), key, depth + 1)}
            except (ValueError, RecursionError):
                pass
        if not value and key in ("contentId", "checkUpdate", "amazonType"):
            return ""
        if key in ("contentId", "content_id", "videoId"):
            return "{content_id}"
        return "{string}"
    if value is None:
        return None
    return "{boolean}" if isinstance(value, bool) else "{number}" if isinstance(value, (int, float)) else "{value}"


def summarize_preview(data: object) -> dict:
    """Describe known launch field shapes, never their personal or ephemeral values."""
    fields, patterns, query_keys = Counter(), Counter(), set()
    templates, app_ids, constants = {}, set(), set()
    templates_truncated = False
    remaining = 16384

    def visit(value: object, depth: int = 0) -> None:
        nonlocal remaining, templates_truncated
        remaining -= 1
        if remaining < 0 or depth > 20:
            raise ValueError("Preview exceeds diagnostics bounds")
        if isinstance(value, dict):
            launch_fields = {"action_data", "actionData", "action_play_url", "action_url", "actionUrl", "deeplink", "deepLink"}
            if (launch_fields.intersection(value) or value.get("module") == "LaunchHelper"
                    or (any(key in value for key in ("appId", "app_id")) and "payload" in value)):
                layout = _template_layout(value)
                encoded = json.dumps(layout, sort_keys=True, separators=(",", ":"))
                if len(templates) < 16 and len(encoded) <= 4096:
                    templates[encoded] = {"layout": layout, "scope": "device_all_apps"}
                elif encoded not in templates:
                    templates_truncated = True
            for key, child in value.items():
                if key in _FIELDS:
                    kind = "object" if isinstance(child, dict) else "array" if isinstance(child, list) else "string" if isinstance(child, str) else "other"
                    fields[(key, kind)] += 1
                    if key in ("appId", "app_id") and isinstance(child, str) and child in _PUBLIC_APP_IDS:
                        app_ids.add(child)
                    if isinstance(child, str) and child in _PUBLIC_CONSTANTS.get(key, set()):
                        constants.add((key, child))
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
        elif isinstance(value, str):
            if len(value) > _MAX_BODY:
                raise ValueError("Preview string exceeds diagnostics bounds")
            if _YOUTUBE_PREVIEW.fullmatch(value):
                patterns["youtube_launcher_with_ephemeral_id"] += 1
            if re.fullmatch(r"https://(?:www\.)?netflix\.com/watch/[0-9]{1,12}(?:\?[^\s]*)?", value):
                patterns["netflix_watch_url"] += 1
            if value.startswith("action_play_url&"):
                patterns["action_play_url"] += 1
            if value.startswith(("http://", "https://")):
                patterns["http_url"] += 1
                try:
                    parsed = urlsplit(value)
                    query_keys.update(key for key, _ in parse_qsl(parsed.query, max_num_fields=64) if key in _QUERY_KEYS)
                    if parsed.fragment.startswith("play?"):
                        query_keys.update(key for key, _ in parse_qsl(parsed.fragment[5:], max_num_fields=64) if key in _QUERY_KEYS)
                except ValueError:
                    pass
            if value.lstrip().startswith(("{", "[", '"')):
                try:
                    decoded = json.loads(value)
                except (ValueError, RecursionError):
                    return
                patterns["json_encoded"] += 1
                visit(decoded, depth + 1)

    visit(data)
    return {"field_shapes": [{"field": key, "type": kind, "count": count} for (key, kind), count in sorted(fields.items())],
            "payload_patterns": dict(sorted(patterns.items())), "query_keys": sorted(query_keys),
            "known_app_ids": [{"id": key, "provider": _PUBLIC_APP_IDS[key]} for key in sorted(app_ids)],
            "known_constants": [{"field": key, "value": value} for key, value in sorted(constants)],
            "templates": [templates[key] for key in sorted(templates)], "templates_truncated": templates_truncated,
            "values_included": False}


async def _samsung_preview(session, host, pairing, name, report: dict, *,
                           capture: SamsungCapture | None = None,
                           capture_sink: list | None = None) -> None:
    """Project one shared transient capture; never attach raw payloads to the report."""
    if capture is None:
        access = TVAccess(host, pairing, name, AccessSource.NATIVE)
        capture = await async_capture_samsung(access)
    if capture_sink is not None:
        capture_sink.append(capture)
    report["connection"] = {"authorization": capture.authorization, **capture.details}
    details = dict(capture.preview_details)
    if capture.preview is not None:
        try:
            summary = summarize_preview(capture.preview)
            _record_check(report, {"id": "smart_hub_preview", "status": "available",
                                  "scope": "device_all_apps", "selected_app_match": "not_determined",
                                  **details, **summary})
            return
        except (ValueError, TypeError, RecursionError):
            details.update(phase="preview", reason="invalid_response")
    _record_check(report, {"id": "smart_hub_preview",
                          "status": "skipped" if details.get("reason") == "existing_pairing_required" else "unavailable",
                          **details})


async def _samsung_zuhause(host, pairing, name, report: dict) -> None:
    check = await async_probe_zuhause(TVAccess(host, pairing, name, AccessSource.NATIVE))
    _record_check(report, {**check, "scope": "device_all_apps"})


def _candidates(report: dict, provider: str | None) -> list[dict]:
    candidate = {
        "samsungtv": "samsung_smart_hub_preview", "webostv": "webos_application_launch",
        "androidtv": "android_view_intent", "androidtv_remote": "android_view_intent", "roku": "roku_ecp",
    }.get(report["platform"])
    result = []
    if candidate:
        preview_available = any(check["id"] == "smart_hub_preview" and check["status"] == "available" for check in report["checks"])
        result.append({"id": candidate, "status": "observed" if preview_available else "candidate",
                       "scope": "device_protocol", "evidence_level": report.get("evidence_level", "not_checked"), "playback_verified": False})
    if provider and report["capabilities"].get(provider + "_title"):
        result.append({"id": "configured_" + provider + "_route", "status": "configured", "playback_verified": False})
    if report["platform"] == "samsungtv" and provider == "netflix" and any(
            check.get("id") == "zuhause_bridge" and check.get("status") == "available" for check in report["checks"]):
        result.append({"id": "samsung_zuhause", "app": "netflix", "status": "candidate",
                       "scope": "device_protocol", "evidence_level": "service_detected",
                       "playback_verified": False, "requires_title_test": True})
    if report["platform"] == "samsungtv" and provider == "netflix" and any(
            check.get("id") == "samsung_app_status" and check.get("app") == "netflix"
            and check.get("protocol") == "dial" and check.get("status") == "available"
            for check in report["checks"]):
        result.append({"id": "samsung_dial_netflix", "app": "netflix", "status": "candidate",
                       "scope": "device_protocol", "evidence_level": "service_detected",
                       "playback_verified": False, "requires_title_test": True})
    return result


def _adapter_descriptors(platform: str) -> list[dict]:
    descriptor = {
        "samsungtv": ("samsung_smart_hub_preview", "paired_smart_hub_preview", True),
        "webostv": ("webos_application_launch", "application_launch_parameters", False),
        "androidtv": ("android_view_intent", "android_action_view", False),
        "androidtv_remote": ("android_view_intent", "android_action_view", False),
        "roku": ("roku_ecp", "ecp_launch_parameters", False),
    }.get(platform)
    if descriptor is None:
        return []
    result = [{"id": descriptor[0], "protocol": descriptor[1], "passive_capture_implemented": descriptor[2],
               "protocol_candidate_only": True, "executable": False, "requires_per_app_confirmation": True}]
    if platform == "samsungtv":
        result.append({"id": "samsung_zuhause", "app": "netflix", "protocol": "zuhause_content_v1",
                       "passive_capture_implemented": True, "protocol_candidate_only": True,
                       "executable": False, "requires_per_app_confirmation": True})
        result.append({"id": "samsung_dial_netflix", "app": "netflix", "protocol": "dial_json_content",
                       "passive_capture_implemented": True, "protocol_candidate_only": True,
                       "executable": False, "requires_per_app_confirmation": True})
    return result


def _selected_format_level(report: dict, provider: str | None) -> str:
    if provider is None:
        return "not_checked"
    for check in report["checks"]:
        if check["id"] != "smart_hub_preview":
            continue
        patterns = check.get("payload_patterns", {})
        if ((provider == "youtube" and "youtube_launcher_with_ephemeral_id" in patterns)
                or (provider == "netflix" and "netflix_watch_url" in patterns)):
            return "start_format_detected"
        for template in check.get("templates", []):
            encoded = json.dumps(template.get("layout"), sort_keys=True)
            known_app = any(owner == provider and json.dumps(app_id) in encoded for app_id, owner in _PUBLIC_APP_IDS.items())
            if known_app or (provider == "youtube" and "{ephemeral_start_id}" in encoded):
                return "start_format_detected"
    return "not_checked"


def _technical_values(report: dict) -> dict:
    """A small fixed comparison vocabulary excludes timestamps, observations and counts."""
    result = {"platform": report.get("platform"), "profile": report.get("profile"),
              "capabilities": report.get("capabilities"), "evidence_level": report.get("evidence_level"),
              "adapters": report.get("adapters"), "identity_match": report.get("identity_match")}
    connection = report.get("connection")
    if isinstance(connection, dict):
        result["connection"] = {key: connection.get(key) for key in
                                ("authorization", "phase", "port", "reason", "http_status", "close_code")
                                if key in connection}
    for group, names in {"device": ("manufacturer", "model", "firmware"),
                         "reported": ("firmware", "app_version"),
                         "app": ("id", "provider", "version", "version_source", "evidence_level")}.items():
        values = report.get(group)
        values = values if isinstance(values, dict) else {}
        for name in names:
            result[group + "." + name] = values.get(name)
    checks = report.get("checks")
    checks = checks if isinstance(checks, list) else []
    for check_id in ("device_metadata", "samsung_device_api", "dial_discovery", "smart_hub_preview", "standard_app_listing", "samsung_app_status", "zuhause_bridge", "manufacturer_protocol", "collection"):
        values = []
        for check in checks:
            if not isinstance(check, dict) or check.get("id") != check_id:
                continue
            value = {key: check.get(key) for key in ("status", "reason", "phase", "app", "app_id", "app_version", "protocol", "state", "port", "http_status", "close_code", "application_service", "evidence_level", "templates", "templates_truncated", "known_app_ids", "known_constants", "query_keys", "identity_match", "firmware_disagreement") if key in check}
            capture = check.get("capture")
            value["capture_source"] = capture.get("source") if isinstance(capture, dict) else check.get("source")
            shapes = check.get("field_shapes")
            if isinstance(shapes, list):
                value["field_shapes"] = [{"field": shape.get("field"), "type": shape.get("type")} for shape in shapes if isinstance(shape, dict)]
            patterns = check.get("payload_patterns")
            if isinstance(patterns, dict):
                value["payload_patterns"] = sorted(patterns)
            values.append(value)
        result["checks." + check_id] = values
    return result


def diff_reports(previous: dict | None, current: dict) -> dict:
    """Return fixed field paths only, never previous report values or volatile timings."""
    if not isinstance(previous, dict):
        return {"changed": ["baseline_missing"], "same_technical_findings": False}
    before, after = _technical_values(previous), _technical_values(current)
    changed = [key for key in sorted(after) if before.get(key) != after[key]]
    return {"changed": changed, "same_technical_findings": not changed}


async def async_collect_diagnostics(manager, room: dict, target: dict, app: str,
                                    observation: str = "not_tested", *,
                                    samsung_capture: SamsungCapture | None = None,
                                    capture_sink: list | None = None) -> dict:
    """Inspect the selected target only. Partial reports survive missing access."""
    app = _validate_app(manager, target, app)
    if observation not in OBSERVATIONS:
        raise CinemaError("Bitte ein gültiges beobachtetes Ergebnis auswählen.", "invalid_observation")
    player = target["entity_id"]
    platform = manager.platform(player)
    platform = platform if platform in _PLATFORMS else "other"
    provider = _app_provider(app)
    public_app = _public_text(app)
    report = {"schema_version": 2, "integration_version": VERSION, "created_at": datetime.now(UTC).isoformat(),
              "mode": "passive", "device": _device_values({}), "platform": platform,
              "profile": target.get("profile") if target.get("profile") in _PROFILES else "other",
              "app": {"id": public_app, "provider": provider, "version": None, "version_source": "unknown", "evidence_level": "not_checked"},
              "reported": {"firmware": None, "app_version": None, "source": "user"},
              "evidence_level": "not_checked", "evidence_scope": "device_protocol",
              "identity_match": None, "identity_scope": "public_model",
              "capabilities": {}, "checks": [], "candidates": [],
              "user_observation": {"result": observation, "source": "user", "establishes_compatibility": False},
              "limitations": ["passive_no_playback_test", "no_automatic_profile_changes"]}
    bridge = manager.hass.data.get(DOMAIN, {}).get("youtube_bridge_clients", {}).get(player)
    device_access = getattr(manager, "device_access", None)
    if device_access is not None:
        hints = device_access.public_binding(room, target, app)
        report["reported"].update({"firmware": _public_text(hints.get("firmware")), "app_version": _public_text(hints.get("app_version"))})
        report["app"]["version"] = report["reported"]["app_version"]
        report["app"]["version_source"] = "user" if report["app"]["version"] else "unknown"
    try:
        capabilities = manager.capabilities(room, target)
        report["capabilities"] = {key: value is True for key, value in capabilities.items() if key in _CAPABILITIES}
        async with asyncio.timeout(18):
            try:
                values, source = await _device_metadata(manager, player, bridge)
                report["device"] = values
                _record_check(report, {"id": "device_metadata", "status": "available" if all(values.values()) else "partial", "source": source})
            except Exception:
                # Exception strings can contain HA URLs, credentials or private entity names.
                _record_check(report, {"id": "device_metadata", "status": "unavailable",
                                       "source": "main_instance" if bridge is not None else "local_registry"})
            if platform == "samsungtv":
                host, pairing, name = _samsung_connection(manager, room, target)
                if host is None:
                    _record_check(report, {"id": "samsung_device_api", "status": "skipped", "reason": "direct_address_unavailable"})
                    if bridge is not None:
                        report["limitations"].append("main_instance_does_not_proxy_tv_network")
                else:
                    async with ClientSession(timeout=ClientTimeout(total=10), trust_env=False,
                                             trace_configs=[samsung_youtube_trace_config()]) as session:
                        tasks = [_samsung_http(session, host, report)]
                        if provider == "netflix":
                            tasks.append(_samsung_zuhause(host, pairing, name, report))
                        if pairing is not None:
                            tasks.append(_samsung_preview(session, host, pairing, name, report,
                                                          capture=samsung_capture, capture_sink=capture_sink))
                        else:
                            _record_check(report, {"id": "smart_hub_preview", "status": "skipped", "reason": "existing_pairing_required"})
                            report["connection"] = {"authorization": "not_checked", "phase": "authorization",
                                                    "reason": "existing_pairing_required"}
                        await asyncio.gather(*tasks)
            else:
                _record_check(report, {"id": "manufacturer_protocol", "status": "not_implemented"})
    except TimeoutError:
        _record_check(report, {"id": "collection", "status": "partial", "reason": "timeout"})
    except Exception:
        _record_check(report, {"id": "collection", "status": "partial", "reason": "unavailable"})
    if report["device"]["firmware"] is None:
        report["limitations"].append("firmware_not_reported")
    if public_app is None:
        report["limitations"].append("application_name_omitted")
    if report["reported"]["firmware"] is not None or report["reported"]["app_version"] is not None:
        report["limitations"].append("reported_versions_are_user_hints")
        if (report["reported"]["firmware"] is not None and report["device"]["firmware"]
                and report["device"]["firmware"] != report["reported"]["firmware"]):
            report["limitations"].append("reported_firmware_differs")
    report["checks"].sort(key=lambda item: item["id"])
    report["evidence_level"] = max((check["evidence_level"] for check in report["checks"]), key=EVIDENCE_LEVELS.index, default="not_checked")
    report["app"]["evidence_level"] = _selected_format_level(report, provider)
    report["candidates"] = _candidates(report, provider)
    report["adapters"] = _adapter_descriptors(platform)
    fingerprint = {"platform": report["platform"], "device": report["device"],
                   "app": provider or (public_app.casefold() if public_app else None),
                   "app_version": report["app"]["version"], "app_version_source": report["app"]["version_source"],
                   "reported_firmware": report["reported"]["firmware"]}
    for check in report["checks"]:
        if provider == "netflix" and check["id"] == "zuhause_bridge" and check["status"] == "available":
            fingerprint["zuhause_bridge"] = {key: check.get(key) for key in ("status", "app_version")}
    report["fingerprint"] = sha256(json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return report
