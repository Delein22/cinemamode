"""One passive device capture, reused for a fixed beta app checklist."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
import json
import re
import unicodedata

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType

from .const import PROVIDERS
from .diagnostics import (
    _PUBLIC_APP_IDS, _SENSITIVE, _app_provider, _public_text, _selected_format_level, async_collect_diagnostics,
    diff_reports as diff_common_reports,
)
from .models import CinemaError
from .samsung_youtube import _remote_url, samsung_youtube_trace_config


STANDARD_APPS = (
    {"id": "netflix", "name": "Netflix", "aliases": ("Netflix",)},
    {"id": "apple", "name": "Apple TV", "aliases": ("Apple TV", "Apple TV+", "Apple TV Plus", "AppleTVPlus")},
    {"id": "magenta", "name": "MagentaTV", "aliases": ("MagentaTV", "MagentaTV 2.0", "MagentaTV2", "Telekom MagentaTV", "Telekom MagentaTV 2.0", "Telekom TV")},
    {"id": "rtl", "name": "RTL+", "aliases": ("RTL+", "RTL Plus", "RTLPlus", "TVNOW", "TV NOW")},
    {"id": "plex", "name": "Plex", "aliases": ("Plex",)},
    {"id": "youtube", "name": "YouTube", "aliases": ("YouTube", "YouTube on TV")},
)
_SYSTEM_ALIASES = {
    "samsung": "samsungtv", "samsungtv": "samsungtv", "tizen": "samsungtv", "samsungtizen": "samsungtv",
    "lg": "webostv", "webos": "webostv", "webostv": "webostv", "lgwebos": "webostv",
    "android": "androidtv", "androidtv": "androidtv", "googletv": "androidtv", "androidgoogletv": "androidtv",
    "androidtvremote": "androidtv", "firetv": "androidtv", "roku": "roku", "other": "other", "andere": "other",
}
_COMMON_FIELDS = ("integration_version", "created_at", "platform", "profile", "capabilities", "checks", "adapters",
                  "identity_match", "identity_scope", "limitations", "evidence_level", "evidence_scope")
_OPAQUE = re.compile(r"(?:[A-Za-z0-9_-]{20,}\.){2}[A-Za-z0-9_-]{10,}|[A-Za-z0-9_-]{40,}")


def _name_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    return "".join(char for char in value if char.isalnum() or char == "+")


def normalize_system(system: object) -> str:
    if (not isinstance(system, str) or not system or len(system) > 40
            or any(unicodedata.category(char).startswith("C") for char in system)):
        raise CinemaError("Bitte ein TV-System auswählen.", "invalid_system")
    key = _name_key(system)
    if key not in _SYSTEM_ALIASES:
        raise CinemaError("Bitte ein vorhandenes TV-System auswählen.", "invalid_system")
    return _SYSTEM_ALIASES[key]


def validate_extra_app(extra_app: object = "") -> str:
    """Only a public app name, never a URL, account identifier or credential."""
    if not isinstance(extra_app, str) or len(extra_app) > 80:
        raise CinemaError("Der zusätzliche App-Name darf höchstens 80 Zeichen enthalten.", "invalid_app")
    if any(unicodedata.category(char).startswith("C") for char in extra_app):
        raise CinemaError("Bitte nur den öffentlichen Namen der zusätzlichen App angeben.", "invalid_app")
    value = extra_app.strip()
    if not value:
        return ""
    allowed_punctuation = " .+&()'’_!-"
    if (not any(char.isalnum() for char in value)
            or any(unicodedata.category(char)[0] not in "LMN" and char not in allowed_punctuation for char in value)
            or _SENSITIVE.search(value) or re.search(r"\bwww\.", value, re.I) or _OPAQUE.fullmatch(value)):
        raise CinemaError("Bitte nur den öffentlichen Namen der zusätzlichen App angeben.", "invalid_app")
    return value


def _aliases(app: dict) -> set[str]:
    names = set(app.get("aliases", ())) | {app["id"], app["name"]}
    provider = PROVIDERS.get(app["id"])
    if provider is not None:
        names.add(provider["package"])
    return {_name_key(name) for name in names}


def _known_owner(name: str) -> str | None:
    key = _name_key(name)
    return next((app["id"] for app in STANDARD_APPS if key in _aliases(app)), None)


def _provider(app: dict) -> str | None:
    return app["id"] if app["id"] in PROVIDERS else _app_provider(app["name"])


def _source_listing(manager, target: dict) -> tuple[list[str], bool, bool]:
    state = manager.hass.states.get(target["entity_id"])
    attrs = state.attributes if state is not None else {}
    values = attrs.get("source_list")
    if not isinstance(values, list):
        return [], False, False
    names = [value for value in values[:250] if isinstance(value, str) and value.strip() and len(value) <= 512
             and not re.fullmatch(r"(?:TV|HDMI(?:[\s-]*\d+)?)", value.strip(), re.I)]
    return names, bool(names), len(values) > 250


def _preview_presence(report: dict) -> tuple[set[str], bool]:
    owners, captured = set(), False
    for check in report.get("checks", []):
        if check.get("id") != "smart_hub_preview" or check.get("status") != "available":
            continue
        captured = True
        for app in check.get("known_app_ids", []):
            app_id = app.get("id") if isinstance(app, dict) else None
            owner = _PUBLIC_APP_IDS.get(app_id)
            if owner:
                owners.add(owner)
    return owners, captured


async def _installed_app_presence(manager, room: dict, target: dict, catalog: list[dict], system: str) -> dict:
    """One paired, read-only app-list request; discard every unmatched device value."""
    result = {"status": "skipped", "reason": "existing_pairing_required", "detected": set(), "attempted": False}
    if system != "samsungtv":
        result["reason"] = "manufacturer_app_listing_not_implemented"
        return result
    access_manager = getattr(manager, "device_access", None)
    try:
        access = access_manager.resolve(manager, room, target) if access_manager is not None else None
        if access is None or access.pairing is None:
            return result
        result["attempted"] = True
        result.update(status="unavailable", reason="paired_app_listing_unavailable")
        async with asyncio.timeout(8):
            async with ClientSession(timeout=ClientTimeout(total=8, sock_connect=3), trust_env=False,
                                     trace_configs=[samsung_youtube_trace_config()]) as session:
                async with session.ws_connect(_remote_url(access.host, access.pairing[0], access.client_name, access.pairing[1]),
                                              ssl=False, max_msg_size=2 * 1024 * 1024) as ws:
                    connected = False
                    for _ in range(32):
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
                            await ws.send_json({"method": "ms.channel.emit", "params": {"event": "ed.installedApp.get", "to": "host"}})
                            connected = True
                        elif connected and kind == "ed.installedApp.get":
                            data = event.get("data")
                            apps = data.get("data") if isinstance(data, dict) else data
                            if not isinstance(apps, list) or len(apps) > 512:
                                break
                            matches = set()
                            for installed in apps:
                                if not isinstance(installed, dict):
                                    continue
                                name, app_id = installed.get("name"), installed.get("appId")
                                key = _name_key(name) if isinstance(name, str) and len(name) <= 512 else None
                                owner = _PUBLIC_APP_IDS.get(app_id) if isinstance(app_id, str) and len(app_id) <= 120 else None
                                for app in catalog:
                                    if key in _aliases(app) or (owner is not None and _provider(app) == owner):
                                        matches.add(app["id"])
                            return {"status": "available", "detected": matches, "attempted": True}
    except (ClientError, TimeoutError, OSError, ValueError, RecursionError, CinemaError):
        pass
    return result


def _app_version(manager, room: dict, target: dict, app: dict, matched_sources: list[str]) -> str | None:
    access = getattr(manager, "device_access", None)
    public_binding = getattr(access, "public_binding", None)
    if not callable(public_binding):
        return None
    # These are local binding lookups only, with the app association checked by DeviceAccess.
    for name in dict.fromkeys((app["id"], app["name"], *matched_sources)):
        value = _public_text(public_binding(room, target, name).get("app_version"))
        if value is not None:
            return value
    return None


async def async_collect_standard_diagnostics(manager, room: dict, target: dict, system: str,
                                              extra_app: str = "") -> dict:
    """Share one passive network capture across six standard apps and one optional app."""
    declared_system = normalize_system(system)
    extra_app = validate_extra_app(extra_app)
    catalog = [dict(app) for app in STANDARD_APPS]
    extra_owner = _known_owner(extra_app) if extra_app else None
    if extra_app and extra_owner is None:
        catalog.append({"id": "extra", "name": extra_app, "aliases": (extra_app,)})

    # The generic collector validates 'netflix' as a known diagnostic identity even when not configured.
    # It reads device metadata/HTTP/EDEN once, and performs no launches or pairing.
    captured = await async_collect_diagnostics(manager, room, target, "netflix")
    installed = ({"status": "skipped", "reason": "device_identity_mismatch", "detected": set(), "attempted": False}
                 if captured.get("identity_match") is False
                 else await _installed_app_presence(manager, room, target, catalog, declared_system))
    report = {key: deepcopy(captured[key]) for key in _COMMON_FIELDS if key in captured}
    device = captured.get("device", {})
    report["device"] = {key: _public_text(device.get(key)) for key in ("manufacturer", "model", "firmware")}
    reported = captured.get("reported", {})
    report["reported"] = {"firmware": _public_text(reported.get("firmware")), "app_version": None, "source": "user"}
    report.update(schema_version=3, mode="standard", standard_apps=True,
                  app={"id": None, "provider": None, "version": None, "version_source": "unknown", "evidence_level": "not_checked"},
                  user_observation={"result": "not_tested", "source": "user", "establishes_compatibility": False})
    platform = declared_system
    report.update(platform=platform, platform_source="user_declared", profile_source="user_declared")
    report["system"] = {"declared": declared_system, "source": "user", "matches_platform": None,
                        "establishes_platform": False}
    limitations = report.setdefault("limitations", [])
    for code in ("shared_capture_reused_for_all_apps", "app_listing_may_be_incomplete", "no_per_app_launch_test"):
        if code not in limitations:
            limitations.append(code)
    if extra_owner is not None:
        limitations.append("extra_app_matches_standard")
    # Only protocol candidates are common to all apps. A configured Netflix route is not batch evidence.
    report["candidates"] = [deepcopy(candidate) for candidate in captured.get("candidates", [])
                            if candidate.get("scope") == "device_protocol" and not candidate.get("id", "").startswith("configured_")]
    for check in report.get("checks", []):
        check["scope"] = "device_all_apps"
        if check.get("id") == "smart_hub_preview":
            check["selected_app_match"] = "not_determined"
    report.setdefault("checks", []).append({"id": "standard_app_listing", "status": installed["status"],
        "scope": "device_all_apps", "evidence_level": "service_detected" if installed["status"] == "available" else "not_checked",
        "reason": installed.get("reason"),
        "capture": {"source": "samsung_installed_apps", "observed_at": datetime.now(UTC).isoformat()}})

    sources, listing_available, listing_truncated = _source_listing(manager, target)
    preview_owners, preview_available = _preview_presence(captured)
    if listing_truncated:
        limitations.append("app_listing_truncated")
    results = []
    for app in catalog:
        alias_keys = _aliases(app)
        matches = [source for source in sources if _name_key(source) in alias_keys]
        provider = _provider(app)
        from_preview = (provider or app["id"]) in preview_owners
        from_installed = app["id"] in installed["detected"]
        if matches or from_preview or from_installed:
            presence = "detected"
        else:
            presence = "not_listed" if listing_available or preview_available or installed["status"] == "available" else "not_checked"
        notes = ["not_an_installation_verdict", "no_playback_test"]
        if matches:
            notes.append("reported_by_cached_tv_source_list")
        if from_preview:
            notes.append("public_app_id_in_shared_preview")
        if from_installed:
            notes.append("reported_by_paired_tv_app_list")
        if presence == "not_listed":
            notes.extend(("not_in_available_listing", "listing_may_be_incomplete"))
        elif presence == "not_checked":
            notes.append("app_listing_unavailable")
        if not preview_available:
            notes.append("shared_preview_unavailable")
        evidence = _selected_format_level(captured, provider)
        if evidence == "start_format_detected":
            notes.append("start_format_requires_device_confirmation")
        version = _app_version(manager, room, target, app, matches)
        if version is not None:
            notes.append("app_version_reported_by_user")
        results.append({"id": app["id"], "name": app["name"], "presence": presence, "evidence_level": evidence,
                        "version": version, "version_source": "user" if version else "unknown",
                        "playback_verified": False, "notes": notes})
    report["app_results"] = results
    report["shared_capture"] = {"generic_capture_count": 1, "installed_app_query_count": int(installed["attempted"]),
                                "per_app_network_requests": False,
                                "app_listing_source": "samsung_installed_apps" if installed["status"] == "available" else "ha_source_list" if listing_available else "unavailable",
                                "app_listing_complete": False, "preview_available": preview_available}
    fingerprint = {"platform": platform, "device": report["device"], "reported_firmware": report["reported"]["firmware"],
                   "catalog": [{"id": app["id"], "name": _name_key(app["name"]), "version": app["version"],
                                "version_source": app["version_source"]} for app in results]}
    report["fingerprint"] = sha256(json.dumps(fingerprint, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return report


def diff_standard_reports(previous: dict | None, current: dict) -> dict:
    """Compare fixed public findings, excluding timings, counts and pairing history."""
    if not isinstance(previous, dict):
        return {"changed_fields": ["baseline_missing"], "same_technical_findings": False}
    changed = list(diff_common_reports(previous, current)["changed"])
    before_system, after_system = previous.get("system", {}), current.get("system", {})
    if ((before_system.get("declared") if isinstance(before_system, dict) else None)
            != (after_system.get("declared") if isinstance(after_system, dict) else None)):
        changed.append("system.declared")
    def rows(report):
        values = report.get("app_results", [])
        return {value["id"]: value for value in values if isinstance(value, dict)
                and value.get("id") in {app["id"] for app in STANDARD_APPS} | {"extra"}} if isinstance(values, list) else {}
    before, after = rows(previous), rows(current)
    for app_id in (*[app["id"] for app in STANDARD_APPS], "extra"):
        left, right = before.get(app_id, {}), after.get(app_id, {})
        for key in ("name", "presence", "evidence_level", "version", "version_source"):
            left_value, right_value = left.get(key), right.get(key)
            if key == "name":
                left_value = _name_key(left_value) if isinstance(left_value, str) else None
                right_value = _name_key(right_value) if isinstance(right_value, str) else None
            if left_value != right_value:
                changed.append("app_results." + app_id + "." + key)
    changed = sorted(set(changed))
    return {"changed_fields": changed, "same_technical_findings": not changed}
