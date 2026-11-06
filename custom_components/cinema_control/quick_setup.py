"""One-title, local setup trials with bounded, evidence-aware route selection."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
import re
from uuid import uuid4

from homeassistant.helpers.storage import Store

from .const import DOMAIN, PROVIDERS, VERSION
from .models import CinemaError, content_url, record_id
from .samsung_youtube import SamsungPairingError
from .start_assistant import ADAPTERS, _identity, _safe_report, _scope

_MAX_PROFILES = 64
_MAX_SESSIONS = 32
_JOB_TIMEOUT = 45
_POLL_INTERVAL = 0.05
_OUTCOMES = frozenset({"playing", "profile_selection", "app_only", "title_page", "wrong_title", "failed"})
_SAFE_BEFORE_SEND = frozenset({"unsupported_adapter", "start_format_unknown", "netflix_start_format_unknown",
                              "unsupported_content", "invalid_content", "app_source_unavailable",
                              "app_source_ambiguous", "profile_required", "samsung_youtube_pairing",
                              "dial_not_supported", "dial_unavailable"})
_PUBLIC_ERRORS = _SAFE_BEFORE_SEND | {"busy", "start_outcome_unknown", "start_failed", "transport_unavailable",
                                    "netflix_start_unavailable", "netflix_start_rejected", "not_found", "invalid",
                                    "profile_timeout", "profile_not_confirmed", "dial_start_rejected", "dial_start_unknown"}
_EVIDENCE = {"confirmed_cases": 1, "titles": 1, "source": "user", "situations": 1,
             "establishes_family_compatibility": False}
_MESSAGES = {
    "ready": "Ein weiterer passender Startweg kann geprüft werden.",
    "running": "Verbindung und Startweg werden geprüft. Bitte das Bild am Fernseher beobachten.",
    "connection_required": "Der ausgewählte Fernseher ist noch nicht erreichbar. Prüfe das Gerät und versuche es erneut.",
    "awaiting_result": "Der Titelbefehl wurde gesendet. Läuft der richtige Titel am Fernseher?",
    "awaiting_profile": "Wähle das gewünschte Profil und gib eine PIN bei Bedarf am Fernseher ein. Fahre anschließend hier fort.",
    "uncertain": "Der Ausgang des Aufrufs ist unklar. Prüfe das Bild am TV; es wird kein weiterer Befehl automatisch gesendet.",
    "saved": "Der richtige Titel wurde von dir bestätigt. Der Startweg ist für dieses Gerät und diese App gespeichert; ein Titel und eine Ausgangssituation wurden geprüft.",
    "exhausted": "Kein weiterer bekannter Startweg ist für diese Kombination verfügbar. Lade den Bericht herunter.",
    "cancelled": "Einrichtung beendet. Bereits gesendete TV-Befehle bleiben wirksam.",
}
_LAUNCH_VALUES = frozenset({"partnervdid", "rrid", "iid", "movieid", "trackid", "source_type_payload",
                            "action_play_url", "action_data", "actiondata", "metatag", "payload"})
_CONNECTION_PHASES = frozenset({"connect", "tls", "websocket_handshake", "authorization", "app_list", "preview",
                                "complete", "response", "connection"})
_CONNECTION_REASONS = frozenset({"connection_refused", "connection_timeout", "connection_failed", "tls_failed",
    "handshake_failed", "redirect_rejected", "channel_closed", "channel_error", "tv_denied", "tv_authorization_timeout",
    "authorization_timeout", "tv_protocol_error", "invalid_response", "token_missing", "response_limit",
    "response_timeout", "server_error", "existing_pairing_required", "invalid_device_access", "event_limit", "complete",
    "connection_not_completed", "device_off", "device_unavailable"})
_AUTH_REASONS = frozenset({"tv_denied", "tv_authorization_timeout", "authorization_timeout", "token_missing"})


def _connection_details(value: object) -> dict:
    """Keep fixed connection codes, never arbitrary device or exception strings."""
    value = value if isinstance(value, dict) else {}
    verified = value.get("authorization") == "verified"
    phase, reason = value.get("phase"), value.get("reason")
    details = {"phase": phase if isinstance(phase, str) and phase in _CONNECTION_PHASES else "complete" if verified else "connection",
               "reason": reason if isinstance(reason, str) and reason in _CONNECTION_REASONS else "complete" if verified else "connection_not_completed"}
    for key, lower, upper in (("port", 8001, 8002), ("http_status", 100, 599), ("close_code", 0, 65535)):
        code = value.get(key)
        if type(code) is int and lower <= code <= upper:
            details[key] = code
    attempts = value.get("attempts")
    if isinstance(attempts, (list, tuple)) and len(attempts) > 1:
        details["attempts"] = [_connection_details({key: item[key] for key in
            ("phase", "reason", "port", "http_status", "close_code") if key in item})
            for item in attempts[:2] if isinstance(item, dict)]
    return details


def _connection_result(value: object) -> tuple[dict, str, str]:
    """A failed socket is not evidence that the TV rejected authorization."""
    details = _connection_details(value)
    verified = isinstance(value, dict) and value.get("authorization") == "verified"
    auth_failure = (details["reason"] in _AUTH_REASONS
                    or details["phase"] == "websocket_handshake" and details.get("http_status") in (401, 403))
    authorization = "verified" if verified else "failed" if auth_failure else "not_checked"
    action = "pair" if auth_failure or details["reason"] == "existing_pairing_required" else "retry"
    return details, authorization, action


def _connection_message(reason: str | None, action: str | None) -> str:
    if reason == "device_off":
        return "Der ausgewählte Fernseher ist ausgeschaltet. Schalte ihn ein und prüfe die Verbindung erneut."
    if reason == "device_unavailable":
        return "Der ausgewählte Fernseher ist in Home Assistant nicht verfügbar. Prüfe das ausgewählte Gerät und seine Verbindung."
    if action == "pair":
        return "Die Geräteverbindung benötigt eine Freigabe. Verbinde den Fernseher und bestätige gegebenenfalls Zulassen am TV."
    return "Der ausgewählte Fernseher hat keine erreichbare Verbindung bestätigt. Prüfe Gerät und Netzwerk und versuche es erneut."


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _report(value: object) -> dict:
    """Retain protocol findings, never private launch values even from a malformed caller."""
    def strip(child):
        if isinstance(child, dict):
            return {key: strip(item) for key, item in child.items() if key.casefold() not in _LAUNCH_VALUES}
        if isinstance(child, list):
            return [strip(item) for item in child]
        return child
    return strip(_safe_report(value))


def _public_profile(profile: dict | None) -> dict | None:
    if profile is None:
        return None
    return {"adapter_id": profile["adapter_id"], "app": profile["app"], "scope": "local_device",
            "confirmed_at": profile["confirmed_at"], "evidence": deepcopy(_EVIDENCE),
            "requires_recheck": not bool(profile["identity"]["device"].get("firmware")
                                          and profile["identity"]["app"].get("version"))}


def _available_routes(options: list[dict], report: dict, platform: str, app: str) -> tuple[list[dict], list[dict]]:
    """Do not repeat a preview-dependent route when this capture disproved its prerequisite."""
    checks = report.get("checks", [])
    preview_unavailable = any(check.get("id") == "smart_hub_preview"
                              and check.get("status") in ("unavailable", "skipped") for check in checks)
    dial_observed = (report.get("identity_match") is not False and any(
        check.get("id") == "samsung_app_status" and check.get("app") == "netflix"
        and check.get("protocol") == "dial" and check.get("status") == "available" for check in checks))
    available, skipped = [], []
    for option in options:
        if not option.get("can_test"):
            continue
        reason = None
        if option["id"] == "samsung_dial_netflix" and not dial_observed:
            reason = "service_not_observed"
        elif (platform == "samsungtv" and preview_unavailable
              and (option["id"] == "samsung_ws" or option["id"] == "existing" and option.get("requires_preview") is True)):
            reason = "preview_unavailable"
        if reason:
            skipped.append({"adapter_id": option["id"], "reason": reason})
        else:
            available.append(option)
    return available, skipped


def _has_netflix_bridge(options: list[dict], app: str) -> bool:
    """An existing main-instance route does not require local Samsung WS access."""
    return app == "netflix" and any(option.get("id") == "existing" and option.get("can_test") is True
                                    and option.get("requires_preview") is False for option in options)


class QuickSetup:
    """Transient attempts and separately labelled, single-situation confirmations."""

    def __init__(self, manager) -> None:
        self.manager = manager
        self.store = Store(manager.hass, 1, f"{DOMAIN}.quick_setup", private=True, atomic_writes=True)
        self._profiles: list[dict] = []
        self._reports: dict[tuple, dict] = {}
        self._sessions: dict[tuple, dict] = {}
        self._active: dict[tuple, asyncio.Task] = {}
        self._actions = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self._closed = False
        self._cancelled_ids: set[str] = set()

    async def async_load(self) -> None:
        self._profiles.clear()
        self._reports.clear()
        saved = await self.store.async_load()
        records = saved.get("profiles") if isinstance(saved, dict) else None
        for value in records[:_MAX_PROFILES] if isinstance(records, list) else ():
            try:
                if (not isinstance(value, dict) or value.get("schema_version") != 1
                        or value.get("adapter_id") not in ADAPTERS or value.get("app") not in PROVIDERS
                        or value.get("evidence") != _EVIDENCE):
                    continue
                key = _scope({"id": value["scope"]["room_id"]},
                             {"id": value["scope"]["target_id"], "entity_id": value["scope"]["entity_id"]})
                report = _report(value["report"])
                identity, signature = _identity({**value["identity"], "binding_signature": value["binding_signature"]}, value["app"])
                if report["fingerprint"] != identity["fingerprint"]:
                    continue
                profile = {"schema_version": 1, "scope": dict(zip(("room_id", "target_id", "entity_id"), key, strict=True)),
                           "app": value["app"], "adapter_id": value["adapter_id"], "identity": identity,
                           "binding_signature": signature, "report": report,
                           "confirmed_at": datetime.fromisoformat(value["confirmed_at"]).isoformat(),
                           "evidence": deepcopy(_EVIDENCE)}
                self._profiles.append(profile)
                self._reports[key + (profile["app"],)] = report
            except (KeyError, TypeError, ValueError, CinemaError):
                continue

    def close(self) -> None:
        self._closed = True
        for task in tuple(self._active.values()):
            if not task.done():
                task.cancel()
        self._active.clear()

    def invalidate(self, room: dict, target: dict) -> None:
        key = _scope(room, target)
        self._sessions.pop(key, None)
        self._reports = {scope: report for scope, report in self._reports.items() if scope[:3] != key}
        self._profiles = [profile for profile in self._profiles if self._profile_scope(profile) != key]
        pending = self._active.get(key)
        if pending and pending is not asyncio.current_task() and not pending.done():
            pending.cancel()
        if not self._closed:
            self.manager.hass.async_create_task(self._persist(), "Kinosteuerung Startweg verwerfen")

    @staticmethod
    def _profile_scope(profile: dict) -> tuple:
        return tuple(profile["scope"][field] for field in ("room_id", "target_id", "entity_id"))

    def get_report(self, room: dict, target: dict, app: str) -> dict | None:
        report = self._reports.get(_scope(room, target) + (app,))
        return deepcopy(report) if report is not None else None

    def matching_profile(self, room: dict, target: dict, app: str, identity: dict) -> dict | None:
        key = _scope(room, target)
        try:
            current, signature = _identity(identity, app)
        except CinemaError:
            return None
        for profile in reversed(self._profiles):
            if (self._profile_scope(profile) == key and profile["app"] == app
                    and profile["identity"] == current and profile["binding_signature"] == signature):
                return deepcopy(profile)
        return None

    def _status(self, room: dict, target: dict) -> dict:
        key = _scope(room, target)
        session = self._sessions.get(key)
        public = None
        report = None
        profile = None
        if session is not None:
            public = {name: deepcopy(session[name]) for name in ("id", "state", "app", "favorite_id", "created_at", "attempts")}
            public["message"] = _MESSAGES[session["state"]]
            public["can_next"] = session["state"] == "ready" and session["next_index"] < len(session["candidates"])
            public["can_record"] = session["state"] in ("awaiting_result", "uncertain")
            required = session["state"] == "connection_required"
            public["connection_action"] = session.get("connection_action", "retry") if required else None
            public["connection_reason"] = session.get("connection_reason") if required else None
            public["can_connect"] = required and public["connection_action"] == "pair"
            public["can_retry"] = required and public["connection_action"] == "retry"
            if required:
                public["message"] = _connection_message(public["connection_reason"], public["connection_action"])
            public["can_continue_profile"] = session["state"] == "awaiting_profile"
            report = deepcopy(self._reports.get(key + (session["app"],)))
            if report is not None:
                report["quick_setup_summary"] = {
                    "state": session["state"], "scope": "local_device", "confirmed_cases": int(session["state"] == "saved"),
                    "titles": int(session["state"] == "saved"), "situations": int(session["state"] == "saved"),
                    "establishes_family_compatibility": False,
                    "connection_action": public["connection_action"], "connection_reason": public["connection_reason"],
                    "attempts": [{name: attempt[name] for name in ("adapter_id", "state", "error_code", "outcome", "evidence_level") if name in attempt}
                                 for attempt in session["attempts"]],
                }
            if session.get("identity") is not None and session["state"] != "connection_required":
                profile = self.matching_profile(room, target, session["app"],
                                                {**session["identity"], "binding_signature": session["binding_signature"]})
        return {"session": public, "profile": _public_profile(profile), "report": report}

    def _session(self, room: dict, target: dict, session_id: object) -> dict:
        current = self._sessions.get(_scope(room, target))
        if current is None or current["id"] != session_id:
            raise CinemaError("Diese Einrichtung ist nicht mehr aktuell. Bitte erneut öffnen.", "stale_session")
        return current

    def _validate_context(self, room: dict, target: dict, session: dict) -> dict:
        if (self._closed or self.manager.closed or self._sessions.get(_scope(room, target)) is not session
                or session["state"] == "cancelled" or session["id"] in self._cancelled_ids):
            raise CinemaError("Die Einrichtung wurde beendet oder ersetzt.", "stale_session")
        current_room, current_target = self.manager.resolve(room["id"], target["id"])
        if (_scope(current_room, current_target) != _scope(room, target)
                or current_room.get("tv_entity_id") != room.get("tv_entity_id")
                or any(current_target.get(name) != target.get(name)
                       for name in ("profile", "remote_entity_id", "hdmi_source", "apps"))):
            raise CinemaError("Das Wiedergabeziel wurde geändert.", "stale_device")
        favorite = next((item for item in self.manager.config["favorites"] if item["id"] == session["favorite_id"]), None)
        original = session["favorite"]
        fields = ("provider", "url", "playback", "prime_auto_profile", "prime_auto_continue")
        if favorite is None or any(favorite.get(name) != original.get(name) for name in fields):
            raise CinemaError("Der Favorit wurde geändert. Bitte die Einrichtung neu starten.", "stale_session")
        return favorite

    def _validate(self, room: dict, target: dict, session: dict) -> dict:
        favorite = self._validate_context(room, target, session)
        current_room, current_target = self.manager.resolve(room["id"], target["id"])
        identity, signature = _identity(self.manager._assistant_identity(current_room, current_target, session["app"]), session["app"])
        if identity != session["identity"] or signature != session["binding_signature"]:
            raise CinemaError("Geräteverbindung oder Version wurden geändert. Bitte erneut prüfen.", "stale_device")
        return favorite

    async def async_action(self, room: dict, target: dict, action: str, data: dict, context) -> dict:
        fields = {"status": set(), "begin": {"favorite_id", "session_id"}, "connect": {"session_id"}, "retry": {"session_id"}, "next": {"session_id"},
                  "record": {"session_id", "outcome"}, "continue_profile": {"session_id"}, "cancel": {"session_id"}}
        if self._closed or not isinstance(action, str) or action not in fields or not isinstance(data, dict) or set(data) - fields[action]:
            raise CinemaError("Ungültige Angabe zur schnellen Einrichtung.", "invalid_quick_setup")
        key = _scope(room, target)
        if action == "status":
            return self._status(room, target)
        if action == "cancel":
            session_id = data.get("session_id")
            if isinstance(session_id, str) and re.fullmatch(r"[a-f0-9]{32}", session_id):
                if len(self._cancelled_ids) >= 128:
                    self._cancelled_ids.pop()
                self._cancelled_ids.add(session_id)
            session = self._session(room, target, session_id)
            session["state"] = "cancelled"
            active = self._active.get(key)
            if active and active is not asyncio.current_task() and not active.done():
                active.cancel()
            await self._cancel_job(room, session)
            return self._status(room, target)
        if self._actions.locked():
            raise CinemaError("Eine Einrichtung läuft bereits. Bitte kurz warten.", "busy")
        async with self._actions:
            self._active[key] = asyncio.current_task()
            try:
                if action == "begin":
                    await self._begin(room, target, data.get("favorite_id"), context, data.get("session_id"))
                else:
                    session = self._session(room, target, data.get("session_id"))
                    if action == "retry":
                        self._validate_context(room, target, session)
                        if session["state"] != "connection_required" or session.get("connection_action") != "retry":
                            raise CinemaError("Eine erneute Verbindungsprüfung ist hier nicht erforderlich.", "invalid_quick_setup")
                        self._sessions.pop(key, None)
                        await self._begin(room, target, session["favorite_id"], context, session["id"], allow_pairing=False)
                    elif action == "connect":
                        self._validate_context(room, target, session)
                        if session["state"] != "connection_required" or session.get("connection_action") != "pair":
                            raise CinemaError("Eine neue TV-Verbindung ist hier nicht erforderlich.", "invalid_quick_setup")
                        readiness = self._device_readiness(target)
                        if readiness is not None:
                            self._connection_failure(room, target, session, CinemaError("Gerät nicht bereit.", readiness))
                            return self._status(room, target)
                        try:
                            await self.manager.async_quick_pair(room, target, force=True)
                        except (CinemaError, TimeoutError) as error:
                            self._connection_failure(room, target, session, error)
                        else:
                            if self._sessions.get(key) is not session or session["state"] == "cancelled":
                                raise CinemaError("Die Einrichtung wurde beendet.", "stale_session")
                            self._sessions.pop(key, None)
                            await self._begin(room, target, session["favorite_id"], context, session["id"], allow_pairing=False)
                    elif action == "next":
                        self._validate(room, target, session)
                        if session["state"] != "ready":
                            raise CinemaError("Bitte zuerst das Ergebnis des letzten Aufrufs bestätigen.", "confirmation_required")
                        await self._try_next(room, target, session, context)
                    elif action == "continue_profile":
                        self._validate(room, target, session)
                        if session["state"] != "awaiting_profile":
                            raise CinemaError("Hier wartet keine Profilauswahl.", "invalid_quick_setup")
                        job = self.manager.jobs.get(room["id"], {})
                        attempt = session["attempts"][-1]
                        if attempt["adapter_id"] == "existing":
                            if job.get("id") != session.get("job_id"):
                                attempt.update(state="uncertain", error_code="start_outcome_unknown")
                                session["state"] = "uncertain"
                            else:
                                if job.get("phase") == "awaiting_profile":
                                    self.manager.confirm_profile(room["id"], target["id"], job["id"], context)
                                    await asyncio.sleep(0)
                                await self._wait_job(room, target, session)
                        else:
                            session["state"] = "awaiting_result"
                    elif action == "record":
                        self._validate(room, target, session)
                        await self._record(room, target, session, data.get("outcome"))
            except asyncio.CancelledError:
                session = self._sessions.get(key)
                if session is not None and session["state"] != "saved":
                    session["state"] = "cancelled"
                    await self._cancel_job(room, session)
            finally:
                if self._active.get(key) is asyncio.current_task():
                    self._active.pop(key, None)
            return self._status(room, target)

    def _device_readiness(self, target: dict) -> str | None:
        """Use available HA state only; never wake or contact a known-off TV."""
        states = getattr(self.manager.hass, "states", None)
        get_state = getattr(states, "get", None)
        if not callable(get_state):
            return None
        state = get_state(target["entity_id"])
        value = getattr(state, "state", None)
        if value in ("off", "standby"):
            return "device_off"
        if state is None or value in ("unknown", "unavailable"):
            return "device_unavailable"
        return None

    async def _begin(self, room: dict, target: dict, favorite_id: object, context, session_id: object = None, *,
                     allow_pairing: bool = True) -> None:
        from .launch_adapters import adapter_options

        if self.manager.tasks or self.manager._control_lock.locked() or self.manager._diagnostic_lock.locked():
            raise CinemaError("Bitte den laufenden Start oder die Geräteaktion zuerst beenden.", "busy")
        favorite = next((item for item in self.manager.config["favorites"] if item["id"] == favorite_id), None)
        if favorite is None or favorite.get("provider") not in PROVIDERS:
            raise CinemaError("Bitte einen vorhandenen Favoriten auswählen.", "invalid_favorite")
        if favorite.get("playback") == "restart":
            raise CinemaError("Wähle für diesen Titelstart-Test einen Favoriten mit Fortsetzen. Der Test bestätigt keine Wiedergabe ab 0:00.",
                              "unsupported_playback")
        content_url(favorite["provider"], favorite.get("url"))
        key = _scope(room, target)
        if session_id is not None and (not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9]{32}", session_id)):
            raise CinemaError("Ungültige Kennung für die Einrichtung.", "invalid_quick_setup")
        if session_id in self._cancelled_ids:
            raise CinemaError("Diese Einrichtung wurde bereits beendet.", "stale_session")
        if session_id is not None and any(value["id"] == session_id for value in self._sessions.values()):
            raise CinemaError("Diese Einrichtungskennung wurde bereits verwendet.", "stale_session")
        if key not in self._sessions and len(self._sessions) >= _MAX_SESSIONS:
            raise CinemaError("Zu viele offene Einrichtungsabläufe. Bitte einen Ablauf beenden.", "busy")
        session = {"id": session_id or uuid4().hex, "state": "running", "app": favorite["provider"], "favorite_id": record_id(favorite["id"]),
                   "favorite": deepcopy(favorite), "created_at": _now(), "attempts": [], "candidates": [], "next_index": 0}
        self._sessions[key] = session
        readiness = self._device_readiness(target)
        if readiness is not None:
            self._connection_failure(room, target, session, CinemaError("Gerät nicht bereit.", readiness))
            return
        pairing_error = None
        try:
            if self.manager.platform(target["entity_id"]) == "samsungtv":
                access = self.manager.device_access.resolve(self.manager, room, target)
                bridge_available = _has_netflix_bridge(adapter_options(self.manager, room, target, session["app"]), session["app"])
                if allow_pairing and (access is None or access.pairing is None) and not bridge_available:
                    try:
                        await self.manager.async_quick_pair(room, target, force=False)
                    except (CinemaError, TimeoutError) as error:
                        # DIAL can work without a Samsung remote-control authorization.
                        # Keep the failure, but let read-only discovery find that route.
                        pairing_error = error
            report = await self.manager.diagnose(room, target, session["app"])
        except (CinemaError, TimeoutError) as error:
            self._connection_failure(room, target, session, error)
            return
        if self._sessions.get(key) is not session or session["state"] == "cancelled" or session["id"] in self._cancelled_ids:
            raise CinemaError("Diese Einrichtung wurde beendet.", "stale_session")
        details, authorization, _action = _connection_result(report.get("connection"))
        report = {**report, "connection": {"authorization": authorization, **details}}
        safe = _report(report)
        self._reports[key + (session["app"],)] = safe
        session["identity"], session["binding_signature"] = _identity(
            {**safe, "binding_signature": self.manager.device_access.signature(room, target)}, session["app"])
        paired_response = any(check.get("status") == "available" and check.get("id") in
                              ("smart_hub_preview", "standard_app_listing") for check in report.get("checks", []))
        available, skipped = _available_routes(adapter_options(self.manager, room, target, session["app"]), report,
                                               self.manager.platform(target["entity_id"]), session["app"])
        safe["quick_setup_skipped_routes"] = skipped
        if (self.manager.platform(target["entity_id"]) == "samsungtv" and authorization != "verified" and not paired_response
                and not any(option["id"] == "samsung_dial_netflix" for option in available)
                and not _has_netflix_bridge(available, session["app"])):
            if pairing_error is not None:
                self._connection_failure(room, target, session, pairing_error)
            else:
                self._set_connection_required(room, target, session, report["connection"])
            return
        identity = self.manager._assistant_identity(room, target, session["app"])
        selector = getattr(self.manager, "_selected_start_profile", None)
        if callable(selector):
            preferred = selector(room, target, session["app"], identity)
        else:
            confirmed = self.manager.start_assistant.matching_profile(room, target, session["app"], identity)
            preferred = confirmed or self.matching_profile(room, target, session["app"], identity)
        ordered = ([preferred["adapter_id"]] if preferred else []) + ["existing", "samsung_ws", "samsung_dial_netflix", "android_url", "webos_app"]
        by_id = {item["id"]: item for item in available}
        for adapter_id in dict.fromkeys(ordered):
            if adapter_id in by_id and len(session["candidates"]) < 3:
                session["candidates"].append({"id": adapter_id, "label": by_id[adapter_id]["label"]})
        await self._try_next(room, target, session, context)

    def _connection_failure(self, room: dict, target: dict, session: dict, error: Exception) -> None:
        """Keep validated pairing phases; never serialize exception text or requests."""
        if isinstance(error, SamsungPairingError):
            details = error.safe_details()
        else:
            reason = error.code if isinstance(error, CinemaError) and error.code in ("device_off", "device_unavailable") else "connection_not_completed"
            details = {"phase": "connection", "reason": reason}
        self._set_connection_required(room, target, session, details)

    def _set_connection_required(self, room: dict, target: dict, session: dict, value: object) -> None:
        key = _scope(room, target)
        if (self._sessions.get(key) is not session or session["state"] == "cancelled"
                or session["id"] in self._cancelled_ids):
            raise CinemaError("Die Einrichtung wurde beendet.", "stale_session")
        details, authorization, action = _connection_result(value)
        report = deepcopy(self._reports.get(key + (session["app"],)))
        if report is None:
            report = {"schema_version": 2, "mode": "passive", "integration_version": VERSION,
                      "platform": self.manager.platform(target["entity_id"]), "device": {}, "reported": {},
                      "app": {"id": session["app"], "provider": session["app"]},
                      "fingerprint": sha256(("unverified|" + self.manager.device_access.signature(room, target)
                                             + "|" + session["app"]).encode()).hexdigest(), "checks": []}
        report["connection"] = {"authorization": authorization, **details}
        report["checks"] = [check for check in report.get("checks", [])
                            if isinstance(check, dict) and check.get("id") != "quick_connection"][-31:]
        report["checks"].append({"id": "quick_connection", "status": "unavailable", **details})
        self._reports[key + (session["app"],)] = _report(report)
        session.update(state="connection_required", connection_action=action, connection_reason=details["reason"])

    async def _try_next(self, room: dict, target: dict, session: dict, context) -> None:
        from .launch_adapters import dispatch

        while session["next_index"] < len(session["candidates"]):
            favorite = self._validate(room, target, session)
            candidate = session["candidates"][session["next_index"]]
            session["next_index"] += 1
            attempt = {"adapter_id": candidate["id"], "label": candidate["label"], "state": "running", "evidence_level": "not_checked"}
            session["attempts"].append(attempt)
            session["state"] = "running"
            try:
                if candidate["id"] == "existing":
                    job = self.manager.start(room["id"], target["id"], favorite["id"], session["app"], context, assistant_existing=True)
                    session["job_id"] = job["id"]
                    safe_failure = await self._wait_job(room, target, session)
                    if safe_failure:
                        continue
                    return
                entities = self.manager.job_entities(room, target)
                if self.manager._control_lock.locked() or entities & self.manager._reserved:
                    raise CinemaError("Das Gerät wird bereits verwendet.", "busy")
                async with self.manager._control_lock:
                    self.manager._reserved.update(entities)
                    try:
                        self._validate(room, target, session)
                        result = await dispatch(self.manager, room, target, favorite, candidate["id"], context)
                    finally:
                        self.manager._reserved.difference_update(entities)
                self._validate(room, target, session)
                if not isinstance(result, dict) or result.get("accepted") is not True:
                    raise CinemaError("Der Startweg hat keinen angenommenen Aufruf bestätigt.", "start_outcome_unknown")
                attempt.update(state="command_sent", evidence_level="command_sent")
                session["state"] = "awaiting_result"
                return
            except CinemaError as error:
                if error.code in ("stale_device", "stale_session"):
                    raise
                attempt.update(state="failed", error_code=error.code if error.code in _PUBLIC_ERRORS else "start_outcome_unknown")
                if error.code in _SAFE_BEFORE_SEND:
                    continue
                session["state"] = "uncertain"
                return
            except (TimeoutError, OSError):
                attempt.update(state="uncertain", error_code="start_outcome_unknown")
                session["state"] = "uncertain"
                return
            except Exception:
                # Integration errors may contain private URLs or token-bearing requests.
                attempt.update(state="uncertain", error_code="start_outcome_unknown")
                session["state"] = "uncertain"
                return
        session["state"] = "exhausted"

    async def _wait_job(self, room: dict, target: dict, session: dict) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _JOB_TIMEOUT
        while True:
            self._validate(room, target, session)
            job = self.manager.jobs.get(room["id"])
            if not job or job.get("id") != session.get("job_id"):
                raise CinemaError("Der zugehörige Titelstart ist nicht mehr aktuell.", "stale_session")
            attempt = session["attempts"][-1]
            phase = job.get("phase")
            if phase == "awaiting_profile" and job.get("profile_confirmation") != "user_confirmed":
                attempt["state"] = "awaiting_profile"
                session["state"] = "awaiting_profile"
                return False
            task = self.manager.tasks.get(room["id"])
            if phase in ("done", "failed", "cancelled") or (task is not None and task.done()):
                if phase == "done" and job.get("result") not in ("failed", "cancelled"):
                    attempt.update(state="command_sent", evidence_level="command_sent")
                    session["state"] = "awaiting_result"
                    return False
                code = job.get("error_code")
                attempt.update(state="failed", error_code=code if code in _PUBLIC_ERRORS else "start_outcome_unknown")
                if code in _SAFE_BEFORE_SEND:
                    return True
                session["state"] = "uncertain"
                return False
            if loop.time() >= deadline:
                await self._cancel_job(room, session)
                attempt.update(state="uncertain", error_code="start_outcome_unknown")
                session["state"] = "uncertain"
                return False
            await asyncio.sleep(_POLL_INTERVAL)

    async def _cancel_job(self, room: dict, session: dict) -> None:
        job = self.manager.jobs.get(room["id"], {})
        if session.get("job_id") and job.get("id") == session["job_id"] and room["id"] in self.manager.tasks:
            await self.manager.cancel(room["id"])

    async def _record(self, room: dict, target: dict, session: dict, outcome: object) -> None:
        if session["state"] not in ("awaiting_result", "uncertain") or not isinstance(outcome, str) or outcome not in _OUTCOMES:
            raise CinemaError("Bitte zuerst einen Titelaufruf ausführen und dann sein sichtbares Ergebnis bestätigen.", "confirmation_required")
        attempt = session["attempts"][-1]
        attempt.update(outcome=outcome, state="recorded")
        if outcome == "profile_selection":
            session["state"] = "awaiting_profile"
        elif outcome == "playing":
            self._validate(room, target, session)
            profile = {"schema_version": 1,
                       "scope": dict(zip(("room_id", "target_id", "entity_id"), _scope(room, target), strict=True)),
                       "app": session["app"], "adapter_id": attempt["adapter_id"], "identity": deepcopy(session["identity"]),
                       "binding_signature": session["binding_signature"], "confirmed_at": _now(),
                       "report": deepcopy(self._reports[_scope(room, target) + (session["app"],)]), "evidence": deepcopy(_EVIDENCE)}
            async with self._save_lock:
                self._validate(room, target, session)
                updated = [item for item in self._profiles if not
                           (self._profile_scope(item) == _scope(room, target) and item["app"] == session["app"])]
                updated = (updated + [profile])[-_MAX_PROFILES:]
                payload = {"profiles": updated}
                try:
                    await self.store.async_save(payload)
                    if await self.store.async_load() != payload:
                        raise ValueError
                except Exception:
                    raise CinemaError("Der bestätigte Startweg konnte nicht gespeichert werden.", "storage_failed") from None
                self._validate(room, target, session)
                self._profiles = updated
            attempt["evidence_level"] = "playback_confirmed"
            session["state"] = "saved"
        else:
            session["state"] = "ready" if session["next_index"] < len(session["candidates"]) else "exhausted"

    async def _persist(self) -> None:
        async with self._save_lock:
            try:
                await self.store.async_save({"profiles": deepcopy(self._profiles)})
            except Exception:
                # A store exception may contain paths or private serialized configuration.
                pass
