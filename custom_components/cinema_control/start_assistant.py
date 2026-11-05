"""Explicit six-case trials and local, versioned start profiles.

The state engine never launches a device itself. A caller supplies one trusted
dispatch callback for the exact case the administrator has explicitly started.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
import json
import re
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from homeassistant.helpers.storage import Store

from .const import DOMAIN, PROVIDERS
from .models import CinemaError, content_url, entity_id, record_id

ADAPTERS = frozenset({"existing", "samsung_ws", "samsung_dial_netflix", "android_url", "webos_app"})
SCENARIOS = ("app_open", "app_closed", "profile_selection")
OUTCOMES = frozenset({"app_only", "profile_selection", "title_page", "playing", "wrong_title", "failed"})
_PLATFORMS = frozenset({"samsungtv", "webostv", "androidtv", "androidtv_remote", "roku", "cast", "apple_tv", "other"})
_MAX_PROFILES = 64
_MAX_REPORTS = 32
_REPORT_LIMIT = 128 * 1024
_CACHE_LIMIT = 1024 * 1024
_HEX = re.compile(r"[a-f0-9]{64}")
_PUBLIC = re.compile(r"[\w .+(),/-]{1,120}", re.ASCII)
_PRIVATE_TEXT = re.compile(r"https?://|(?:\d{1,3}\.){3}\d{1,3}|(?:[a-f0-9]{2}[:-]){5}[a-f0-9]{2}|"
                           r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}|\b(?:media_player|remote)\.|"
                           r"\b(?:bearer|password|authorization|secret|token)\b", re.I)
_PRIVATE_KEYS = frozenset({"host", "ip", "ip_address", "address", "mac", "mac_address", "token", "password",
                            "secret", "authorization", "cookie", "headers", "serial", "unique_id", "entity_id",
                            "room_id", "target_id", "favorite_id", "profile_name", "media_title", "raw", "raw_preview",
                            "launcher_key", "start_id", "deeplinkId", "session_id", "binding_signature"})
_PUBLIC_TEMPLATE_URLS = frozenset({
    "https://www.netflix.com/watch/{content_id}",
    "https://www.youtube.com/tv#play?v={video_id}&launch=launcher&deeplinkId={ephemeral_start_id}",
})
_ALIASES = {"netflix":"netflix", "disney":"disney", "disney+":"disney", "disneyplus":"disney",
            "prime":"prime", "primevideo":"prime", "amazonprimevideo":"prime", "amazonvideo":"prime",
            "youtube":"youtube", "plex":"plex", "apple":"apple", "appletv":"apple", "appletv+":"apple"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _timestamp(value: object) -> str:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) and len(value) <= 64 else None
        return parsed.isoformat() if parsed is not None and parsed.tzinfo is not None else ""
    except ValueError:
        return ""


def _app_key(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise CinemaError("Bitte eine gültige App wählen.", "invalid_app")
    return _ALIASES.get(re.sub(r"[\s_-]+", "", value.casefold()), value)


def _scope(room: dict, target: dict) -> tuple[str, str, str]:
    return record_id(room.get("id")), record_id(target.get("id")), entity_id(target.get("entity_id"))


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _PUBLIC.fullmatch(value) and not _PRIVATE_TEXT.search(value) and not re.fullmatch(r"[a-f0-9]{32,}", value, re.I) else None


def _signature(value: object) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise CinemaError("Die Geräteverbindung ist nicht mehr eindeutig. Bitte neu prüfen.", "stale_device")
    return value if _HEX.fullmatch(value) else sha256(value.encode()).hexdigest()


def _identity(value: object, app: str) -> tuple[dict, str]:
    if not isinstance(value, dict) or not isinstance(value.get("fingerprint"), str) or not _HEX.fullmatch(value["fingerprint"]):
        raise CinemaError("Bitte zuerst einen aktuellen Diagnosebericht erstellen.", "diagnostic_required")
    declared_app = value.get("app")
    provider = declared_app.get("provider") or declared_app.get("id") if isinstance(declared_app, dict) else declared_app
    if provider is None or _app_key(provider) != app:
        raise CinemaError("Der Diagnosebericht gehört zu einer anderen App.", "stale_diagnostic")
    device = value.get("device") if isinstance(value.get("device"), dict) else {}
    reported = value.get("reported") if isinstance(value.get("reported"), dict) else {}
    app_version = value.get("app_version") or (declared_app.get("version") if isinstance(declared_app, dict) else None)
    result = {"fingerprint": value["fingerprint"], "platform": value.get("platform") if value.get("platform") in _PLATFORMS else "other",
              "device": {key: _text(device.get(key)) for key in ("manufacturer", "model", "firmware")},
              "app": {"id": app, "provider": app, "version": _text(app_version)},
              "reported": {"firmware": _text(reported.get("firmware")), "app_version": _text(reported.get("app_version")), "source":"user"}}
    return result, _signature(value.get("binding_signature", ""))


def _content_identity(favorite: dict) -> tuple[str, str]:
    provider = favorite.get("provider")
    if provider not in PROVIDERS:
        raise CinemaError("Bitte zwei Favoriten desselben Streamingdienstes wählen.", "invalid_favorites")
    url = content_url(provider, favorite.get("url"))
    parsed = urlsplit(url)
    if provider == "netflix":
        key = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    elif provider == "youtube":
        key = parse_qs(parsed.query).get("v", [""])[0] if parsed.path == "/watch" else parsed.path.rstrip("/").rsplit("/", 1)[-1]
    elif provider == "plex":
        key = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    else:
        key = parsed.path.rstrip("/").casefold()
    return provider, key


def _safe_report(value: object) -> dict:
    """Keep collector output bounded and remove private fields defensively."""
    if not isinstance(value, dict) or value.get("schema_version") not in (1, 2) or value.get("mode") != "passive":
        raise CinemaError("Der Diagnosebericht ist ungültig.", "invalid_diagnostic")
    if not isinstance(value.get("fingerprint"), str) or not _HEX.fullmatch(value["fingerprint"]):
        raise CinemaError("Der Diagnosebericht ist ungültig.", "invalid_diagnostic")
    remaining = 16384

    def redact(child: object, key: str = "", depth: int = 0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 20:
            raise CinemaError("Der Diagnosebericht ist zu groß.", "invalid_diagnostic")
        if isinstance(child, dict):
            result = {}
            for name, item in child.items():
                if not isinstance(name, str) or len(name) > 80:
                    continue
                if name == "authorization" and key == "connection" and item in ("verified", "failed", "not_checked"):
                    result[name] = item
                elif name not in _PRIVATE_KEYS:
                    result[name] = redact(item, name, depth + 1)
            return result
        if isinstance(child, list):
            return [redact(item, key, depth + 1) for item in child]
        if isinstance(child, str):
            if key == "phase" and child in ("connect", "tls", "websocket_handshake", "authorization",
                                             "app_list", "preview", "complete", "response", "connection"):
                return child
            # These exact constant templates contain placeholders, never live IDs.
            if child in _PUBLIC_TEMPLATE_URLS:
                return child
            if len(child) > 4096 or _PRIVATE_TEXT.search(child):
                return None
            if re.fullmatch(r"[a-f0-9]{32,}", child, re.I) and key not in ("fingerprint", "compatibility_fingerprint"):
                return None
            return child
        if child is None or type(child) in (int, float, bool):
            return child
        return None

    result = redact(value)
    result.pop("guided_test", None)
    result.pop("report_cache", None)
    try:
        size = len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode())
    except (ValueError, TypeError):
        raise CinemaError("Der Diagnosebericht ist ungültig.", "invalid_diagnostic") from None
    if size > _REPORT_LIMIT:
        raise CinemaError("Der Diagnosebericht ist zu groß.", "invalid_diagnostic")
    return result


def _confirmed_evidence(value: object, fingerprint: str) -> dict | None:
    """Restore only the fixed, fully confirmed six-case record from a local cache."""
    if (not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("adapter_id") not in ADAPTERS
            or value.get("compatibility_fingerprint") != fingerprint or not isinstance(value.get("cases"), list)
            or len(value["cases"]) != 6):
        return None
    cases, seen = [], set()
    for item in value["cases"]:
        if not isinstance(item, dict):
            return None
        pair = (item.get("title"), item.get("scenario"))
        if (pair[0] not in ("title_a","title_b") or pair[1] not in SCENARIOS or pair in seen
                or item.get("state") != "recorded" or item.get("outcome") != "playing"):
            return None
        seen.add(pair)
        case = {"title":pair[0],"scenario":pair[1],"state":"recorded","outcome":"playing",
                "evidence_level":"playback_confirmed","source":"user"}
        for field in ("started_at","observed_at"):
            if stamp := _timestamp(item.get(field)):
                case[field] = stamp
        cases.append(case)
    return {"schema_version":1,"adapter_id":value["adapter_id"],"scope":"local_device",
            "compatibility_fingerprint":fingerprint,"state":"complete","cases":cases,
            "confirmed_cases":6,"all_cases_confirmed":True,"establishes_family_compatibility":False}


class StartAssistant:
    """Sessions are transient. Only confirmed local profiles and safe reports persist."""

    def __init__(self, hass) -> None:
        self.store = Store(hass, 1, f"{DOMAIN}.start_profiles", private=True, atomic_writes=True)
        self._profiles: list[dict] = []
        self._sessions: dict[tuple, dict] = {}
        self._reports: dict[tuple, dict] = {}
        self._evidence: dict[tuple, dict] = {}
        self._run_lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self._closed = False

    async def async_load(self) -> None:
        saved = await self.store.async_load()
        self._profiles.clear()
        self._reports.clear()
        self._evidence.clear()
        self._sessions.clear()
        if not isinstance(saved, dict):
            return
        profiles = saved.get("profiles")
        for item in profiles[:_MAX_PROFILES] if isinstance(profiles, list) else ():
            try:
                scope = item["scope"]
                key = _scope({"id":scope["room_id"]}, {"id":scope["target_id"],"entity_id":scope["entity_id"]})
                app = _app_key(item["app"])
                identity, signature = _identity({**item["identity"], "binding_signature":item["binding_signature"]}, app)
                if (app not in PROVIDERS or item.get("schema_version") != 1 or item["adapter_id"] not in ADAPTERS
                        or item.get("recipe_id") != item["adapter_id"] + ":" + app + ":1"
                        or item.get("evidence") != {"confirmed_cases":6,"titles":2,"source":"user"}):
                    continue
                profile = {"id":record_id(item["id"]), "schema_version":1, "revision":max(1, min(1_000_000, int(item.get("revision", 1)))),
                           "scope":{"room_id":key[0],"target_id":key[1],"entity_id":key[2]}, "app":app,
                           "adapter_id":item["adapter_id"],"recipe_id":item["recipe_id"],"identity":identity,
                           "binding_signature":signature, "confirmed_at":_timestamp(item.get("confirmed_at")),
                           "evidence":{"confirmed_cases":6,"titles":2,"source":"user"}}
                self._profiles.append(profile)
            except (KeyError, TypeError, ValueError, CinemaError):
                continue
        reports = saved.get("reports")
        total = 0
        for item in reports[:_MAX_REPORTS] if isinstance(reports, list) else ():
            try:
                scope = item["scope"]
                key = _scope({"id":scope["room_id"]}, {"id":scope["target_id"],"entity_id":scope["entity_id"]}) + (_app_key(item["app"]),)
                report = _safe_report(item["report"])
                total += len(json.dumps(report).encode())
                if total > _CACHE_LIMIT:
                    break
                report["report_cache"] = {"source":"saved_profile", "refresh_recommended":True}
                self._reports[key] = report
                if evidence := _confirmed_evidence(item.get("guided_test"), report["fingerprint"]):
                    self._evidence[key] = evidence
            except (KeyError, TypeError, ValueError, CinemaError):
                continue

    def set_report(self, room: dict, target: dict, app: str, report: dict) -> None:
        key = _scope(room, target) + (_app_key(app),)
        safe = _safe_report(report)
        session = self._sessions.get(key[:3])
        if session and session["app"] == key[3] and session["identity"]["fingerprint"] != safe["fingerprint"]:
            self._evidence[key] = self._export_evidence(session, "identity_changed")
            self._sessions.pop(key[:3], None)
        if key not in self._reports and len(self._reports) >= _MAX_REPORTS:
            self._reports.pop(next(iter(self._reports)))
        self._reports[key] = safe

    def get_report(self, room: dict, target: dict, app: str) -> dict | None:
        key = _scope(room, target) + (_app_key(app),)
        report = self._reports.get(key)
        if report is None:
            return None
        result = deepcopy(report)
        session = self._sessions.get(key[:3])
        evidence = self._export_evidence(session) if session and session["app"] == key[3] else self._evidence.get(key)
        if evidence:
            result["guided_test"] = deepcopy(evidence)
            result["guided_test"]["matches_report"] = evidence["compatibility_fingerprint"] == result["fingerprint"]
        return result

    @staticmethod
    def _public_profile(profile: dict) -> dict:
        identity = profile["identity"]
        known = {"firmware":bool(identity["device"].get("firmware") or identity.get("reported", {}).get("firmware")),
                 "app":bool(identity["app"].get("version") or identity.get("reported", {}).get("app_version"))}
        return {"id":profile["id"], "schema_version":1, "revision":profile["revision"], "app":profile["app"],
                "adapter_id":profile["adapter_id"], "confirmed_at":profile["confirmed_at"], "scope":"local_device",
                "fingerprint":identity["fingerprint"], "version_known":known, "requires_recheck":not all(known.values()),
                "evidence":deepcopy(profile["evidence"])}

    @staticmethod
    def _public_session(session: dict) -> dict:
        cases = [{key:case.get(key) for key in ("id","favorite_id","title_alias","scenario","state","outcome","evidence_level","failure_reason") if key in case}
                 for case in session["cases"]]
        return {"id":session["id"], "app":session["app"], "adapter_id":session["adapter_id"], "created_at":session["created_at"],
                "cases":cases,"can_save_profile":all(case["state"] == "recorded" and case["outcome"] == "playing" for case in cases)}

    def status(self, room: dict, target: dict, app: str | None = None) -> dict:
        key = _scope(room, target)
        session = self._sessions.get(key)
        selected_app = _app_key(app) if app is not None else session["app"] if session else None
        result = {"session":self._public_session(session) if session and (selected_app is None or session["app"] == selected_app) else None,
                  "profiles":[self._public_profile(profile) for profile in self._profiles
                              if tuple(profile["scope"].get(name) for name in ("room_id","target_id","entity_id")) == key
                              and (selected_app is None or profile["app"] == selected_app)]}
        for profile in result["profiles"]:
            current = self._reports.get(key + (profile["app"],))
            profile["matches_current_report"] = bool(current and current["fingerprint"] == profile["fingerprint"])
            profile["requires_recheck"] |= not profile["matches_current_report"]
        if selected_app is not None:
            result["report"] = self.get_report(room, target, selected_app)
        return result

    def plan(self, room: dict, target: dict, favorite_a: dict, favorite_b: dict, adapter_id: str, identity: dict) -> dict:
        if self._closed:
            raise CinemaError("Der Testassistent wird beendet. Bitte erneut öffnen.", "not_loaded")
        key = _scope(room, target)
        current = self._sessions.get(key)
        if current and any(case["state"] == "running" for case in current["cases"]):
            raise CinemaError("Bitte den laufenden Test abwarten oder abbrechen.", "busy")
        if adapter_id not in ADAPTERS:
            raise CinemaError("Bitte einen angebotenen Startweg wählen.", "invalid_adapter")
        first, second = _content_identity(favorite_a), _content_identity(favorite_b)
        if first[0] != second[0] or first == second or record_id(favorite_a.get("id")) == record_id(favorite_b.get("id")):
            raise CinemaError("Bitte zwei unterschiedliche Titel desselben Streamingdienstes wählen.", "invalid_favorites")
        public_identity, signature = _identity(identity, first[0])
        report = self._reports.get(key + (first[0],))
        if report is None or report["fingerprint"] != public_identity["fingerprint"]:
            raise CinemaError("Bitte zuerst einen passenden Diagnosebericht erstellen.", "diagnostic_required")
        if key not in self._sessions and len(self._sessions) >= 64:
            raise CinemaError("Zu viele offene Testläufe. Bitte einen Testlauf beenden.", "busy")
        favorites = {record_id(favorite_a["id"]):deepcopy(favorite_a),record_id(favorite_b["id"]):deepcopy(favorite_b)}
        cases = []
        for scenario in SCENARIOS:
            for alias, favorite in (("title_a",favorite_a),("title_b",favorite_b)):
                cases.append({"id":"case_" + str(len(cases) + 1),"favorite_id":favorite["id"],"title_alias":alias,
                              "scenario":scenario,"state":"planned","outcome":None,"evidence_level":"not_checked"})
        session = {"id":uuid4().hex,"app":first[0],"adapter_id":adapter_id,"created_at":_now(),
                   "identity":public_identity,"binding_signature":signature,"favorites":favorites,"cases":cases}
        self._sessions[key] = session
        self._evidence.pop(key + (first[0],), None)
        return self._public_session(session)

    def _session(self, room: dict, target: dict, session_id: str) -> tuple[tuple, dict]:
        key = _scope(room, target)
        session = self._sessions.get(key)
        if session is None or session["id"] != session_id:
            raise CinemaError("Dieser Testlauf ist nicht mehr aktuell. Bitte neu öffnen.", "stale_session")
        report = self._reports.get(key + (session["app"],))
        if report is None or report["fingerprint"] != session["identity"]["fingerprint"]:
            raise CinemaError("Die Geräteangaben haben sich geändert. Bitte neu prüfen.", "stale_diagnostic")
        return key, session

    @staticmethod
    def _case(session: dict, case_id: str) -> dict:
        case = next((case for case in session["cases"] if case["id"] == case_id), None)
        if case is None:
            raise CinemaError("Dieser Testfall existiert nicht.", "invalid_case")
        return case

    def validate_session(self, room: dict, target: dict, session_id: str, identity: dict) -> dict:
        """Let callers check fresh connection identity without exposing private scope."""
        _, session = self._session(room, target, session_id)
        public_identity, signature = _identity(identity, session["app"])
        if public_identity != session["identity"] or signature != session["binding_signature"]:
            raise CinemaError("Die Geräteverbindung oder Version hat sich geändert. Bitte erneut diagnostizieren und den Testlauf neu planen.", "stale_device")
        return self._public_session(session)

    async def run_case(self, room: dict, target: dict, session_id: str, case_id: str, prepared: bool, dispatch) -> dict | None:
        key, session = self._session(room, target, session_id)
        case = self._case(session, case_id)
        if prepared is not True:
            raise CinemaError("Bitte zuerst die Ausgangslage am Gerät herstellen und bestätigen.", "preparation_required")
        if self._run_lock.locked() or case["state"] != "planned" or any(item["state"] in ("running","awaiting_observation") for item in session["cases"]):
            raise CinemaError("Bitte den laufenden Test zuerst abschließen.", "busy")
        async with self._run_lock:
            case.update(state="running",started_at=_now())
            try:
                async with asyncio.timeout(30):
                    response = await dispatch(deepcopy(session["favorites"][case["favorite_id"]]), session["adapter_id"])
                if self._sessions.get(key) is not session:
                    return None
                if not isinstance(response, dict) or response.get("accepted") is not True:
                    case.update(state="blocked",outcome="failed",failure_reason="dispatch_not_accepted",evidence_level="access_blocked")
                else:
                    level = response.get("evidence_level")
                    case.update(state="awaiting_observation",evidence_level=level if level in ("command_sent","command_accepted") else "command_sent")
            except asyncio.CancelledError:
                if self._sessions.get(key) is session:
                    case.update(state="blocked",outcome="failed",failure_reason="cancelled",evidence_level="not_checked")
                raise
            except Exception as error:
                if self._sessions.get(key) is not session:
                    return None
                reason = "timeout" if isinstance(error, TimeoutError) else "route_unavailable" if isinstance(error, CinemaError) else "dispatch_failed"
                case.update(state="blocked",outcome="failed",failure_reason=reason,evidence_level="access_blocked")
        return self._public_session(session) if self._sessions.get(key) is session else None

    def record_case(self, room: dict, target: dict, session_id: str, case_id: str, outcome: str) -> dict:
        _, session = self._session(room, target, session_id)
        case = self._case(session, case_id)
        if outcome not in OUTCOMES:
            raise CinemaError("Bitte das Ergebnis am Gerät auswählen.", "invalid_observation")
        if case["state"] != "awaiting_observation":
            raise CinemaError("Bitte diesen Test zuerst einmal starten und dann das Ergebnis bestätigen.", "invalid_case_state")
        level = "playback_confirmed" if outcome == "playing" else "title_opened" if outcome == "title_page" else case["evidence_level"]
        case.update(state="recorded",outcome=outcome,observed_at=_now(),evidence_level=level)
        return self._public_session(session)

    @staticmethod
    def _export_evidence(session: dict, state: str | None = None) -> dict:
        cases = [{"title":case["title_alias"],"scenario":case["scenario"],"state":case["state"],
                  "outcome":case["outcome"],"evidence_level":case["evidence_level"],"source":"user" if case["state"] == "recorded" else "protocol"}
                 for case in session["cases"]]
        for source, exported in zip(session["cases"], cases, strict=True):
            for field in ("started_at", "observed_at"):
                if stamp := _timestamp(source.get(field)):
                    exported[field] = stamp
            if source.get("failure_reason") in ("dispatch_not_accepted","cancelled","timeout","route_unavailable","dispatch_failed"):
                exported["failure_reason"] = source["failure_reason"]
        complete = all(case["state"] == "recorded" for case in cases)
        confirmed = complete and all(case["outcome"] == "playing" for case in cases)
        return {"schema_version":1,"adapter_id":session["adapter_id"],"scope":"local_device",
                "compatibility_fingerprint":session["identity"]["fingerprint"],
                "state":state or ("complete" if complete else "in_progress"),"cases":cases,
                "confirmed_cases":sum(case["state"] == "recorded" and case["outcome"] == "playing" for case in cases),
                "all_cases_confirmed":confirmed,"establishes_family_compatibility":False}

    def cancel(self, room: dict, target: dict, session_id: str) -> None:
        key, session = self._session(room, target, session_id)
        self._evidence[key + (session["app"],)] = self._export_evidence(session, "cancelled")
        self._sessions.pop(key, None)

    def invalidate(self, room: dict, target: dict) -> None:
        """Discard stale transport-bound observations when the caller changes a binding."""
        key = _scope(room, target)
        session = self._sessions.pop(key, None)
        if session:
            self._evidence[key + (session["app"],)] = self._export_evidence(session, "connection_changed")
        for item in tuple(self._reports):
            if item[:3] == key:
                self._reports.pop(item, None)

    def close(self) -> None:
        """Make late callbacks inert; an HA restart never resumes a test session."""
        self._closed = True
        self._sessions.clear()

    def matching_profile(self, room: dict, target: dict, app: str, identity: dict) -> dict | None:
        key, app = _scope(room, target), _app_key(app)
        try:
            public_identity, signature = _identity(identity, app)
        except CinemaError:
            return None
        report = self._reports.get(key + (app,))
        if report is not None and report["fingerprint"] != public_identity["fingerprint"]:
            return None
        for profile in reversed(self._profiles):
            scope = profile["scope"]
            if ((scope["room_id"],scope["target_id"],scope["entity_id"]) == key and profile["app"] == app
                    and profile["binding_signature"] == signature and profile["identity"] == public_identity):
                return deepcopy(profile)
        return None

    async def async_save_profile(self, room: dict, target: dict, session_id: str) -> dict:
        key, session = self._session(room, target, session_id)
        if not self._public_session(session)["can_save_profile"]:
            raise CinemaError("Bitte zuerst alle sechs Testfälle mit dem richtigen laufenden Titel bestätigen.", "confirmation_required")
        async with self._save_lock:
            previous = next((item for item in reversed(self._profiles) if item["scope"] == {"room_id":key[0],"target_id":key[1],"entity_id":key[2]}
                             and item["app"] == session["app"] and item["adapter_id"] == session["adapter_id"]), None)
            profile = {"id":previous["id"] if previous else uuid4().hex,"schema_version":1,"revision":previous["revision"] + 1 if previous else 1,
                       "scope":{"room_id":key[0],"target_id":key[1],"entity_id":key[2]},"app":session["app"],
                       "adapter_id":session["adapter_id"],"recipe_id":session["adapter_id"] + ":" + session["app"] + ":1",
                       "identity":deepcopy(session["identity"]),"binding_signature":session["binding_signature"],"confirmed_at":_now(),
                       "evidence":{"confirmed_cases":6,"titles":2,"source":"user"}}
            updated = [item for item in self._profiles if item is not previous] + [profile]
            updated = updated[-_MAX_PROFILES:]
            reports, total = [], 0
            current_key = key + (session["app"],)
            keys = [current_key] + [item for item in reversed(self._reports) if item != current_key]
            for report_key in keys:
                report = _safe_report(self._reports[report_key])
                size = len(json.dumps(report).encode())
                if len(reports) >= _MAX_REPORTS or total + size > _CACHE_LIMIT:
                    continue
                total += size
                item = {"scope":{"room_id":report_key[0],"target_id":report_key[1],"entity_id":report_key[2]},"app":report_key[3],"report":report}
                active = self._sessions.get(report_key[:3])
                evidence = self._export_evidence(active) if active and active["app"] == report_key[3] else self._evidence.get(report_key)
                if confirmed := _confirmed_evidence(evidence, report["fingerprint"]):
                    item["guided_test"] = confirmed
                reports.append(item)
            payload = {"profiles":updated,"reports":reports}
            try:
                await self.store.async_save(payload)
                if await self.store.async_load() != payload:
                    raise ValueError("Stored profile did not match")
            except Exception:
                raise CinemaError("Das bestätigte Geräteprofil konnte nicht gespeichert werden.", "storage_failed") from None
            # A cancelled or replaced run cannot reactivate its transient state.
            self._profiles = updated
            return self._public_profile(profile)
