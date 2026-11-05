"""Persistence, capability routing and bounded start jobs for existing HA entities."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
import logging
import re
import shlex
from typing import Callable
from urllib.parse import urlsplit
from uuid import uuid4

from homeassistant.components.media_player.const import MediaPlayerEntityFeature as Feature
from homeassistant.core import Context, HomeAssistant, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .const import (DOMAIN, FEEDBACK_TIMEOUT, OFF_STATES, PROFILE_TIMEOUT, PROVIDERS, READY_STATES,
                    SAMSUNG_PLEX_APP_ID, SERVICE_TIMEOUT, SOURCE_TIMEOUT, STORAGE_VERSION, WAKE_TIMEOUT)
from .main_ha import BridgeError, MainHAClient
from .models import CinemaError, content_url, favorite_record, room_record
from .device_access import DeviceAccess
from .start_assistant import StartAssistant
from .plex import PROFILE_FILE as PLEX_PROFILE_FILE, PlexLauncher
from .samsung_disney import PLAY_DELAY, PROFILE_FILE, SamsungDisneyLauncher, disney_title_id
from .samsung_youtube import (
    SAMSUNG_DEFAULT_CLIENT_NAME, YOUTUBE_PAIR_CLIENT_NAME, SamsungPairingError, async_pair_samsung_youtube,
    samsung_youtube_pairing, youtube_video_id,
)


class _StandardDiagnosticAccess:
    """Resolve an existing local Samsung pairing for this exact submitted address."""

    def __init__(self, manager, host):
        self.manager, self.host = manager, host

    def resolve(self, context, room, target):
        from .device_access import AccessSource, TVAccess
        entries = getattr(self.manager.hass, "config_entries", None)
        if entries is not None:
            for entry in entries.async_entries("samsungtv"):
                if (entry.data.get("method") == "websocket" and entry.data.get("host") == str(self.host)
                        and (pairing := samsung_youtube_pairing(entry.data))):
                    return TVAccess(self.host, pairing, SAMSUNG_DEFAULT_CLIENT_NAME, AccessSource.NATIVE)
        return self.manager.device_access.resolve(context, room, target)

    def public_binding(self, room, target, app=""):
        return self.manager.device_access.public_binding(room, target, app)


class _StandardDiagnosticContext:
    """A private endpoint identity, not a newly created Home Assistant entity."""

    def __init__(self, manager, host, system):
        self.manager, self.hass, self.host, self.system = manager, manager.hass, host, system
        key = sha256(f"{host}|{system}".encode()).hexdigest()[:20]
        self.room = {"id":"standard_" + key, "tv_entity_id":"media_player.cinema_diagnostic_" + key}
        self.target = {"id":"endpoint", "entity_id":self.room["tv_entity_id"], "profile":system, "apps":{}}
        self.room["targets"] = [self.target]
        self.device_access = _StandardDiagnosticAccess(manager, host)

    @property
    def closed(self):
        return self.manager.closed

    def resolve(self, room_id, target_id):
        if room_id != self.room["id"] or target_id != self.target["id"] or self.closed:
            raise CinemaError("Diese Geräteverbindung ist nicht mehr aktuell.", "stale_device")
        return self.room, self.target

    def platform(self, _entity_id):
        return self.system

    def capabilities(self, _room, _target):
        return {}

    def _disney_launcher(self, _room, _target):
        return None

_LOGGER = logging.getLogger(__name__)
_PACKAGE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", re.ASCII)
_LIST_COMPLETE = "CINEMA_PACKAGE_LIST_OK"
_MEDIA_ACTIONS = {"volume_up": Feature.VOLUME_STEP, "volume_down": Feature.VOLUME_STEP,
                  "mute": Feature.VOLUME_MUTE, "play": Feature.PLAY, "pause": Feature.PAUSE,
                  "stop": Feature.STOP, "turn_on": Feature.TURN_ON, "turn_off": Feature.TURN_OFF,
                  "source": Feature.SELECT_SOURCE}
_NAV = {
    "samsungtv": {"up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT",
                  "ok": "KEY_ENTER", "back": "KEY_RETURN", "home": "KEY_HOME"},
    "androidtv": {"up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT",
                  "ok": "CENTER", "back": "BACK", "home": "HOME"},
    "androidtv_remote": {"up": "DPAD_UP", "down": "DPAD_DOWN", "left": "DPAD_LEFT",
                         "right": "DPAD_RIGHT", "ok": "DPAD_CENTER", "back": "BACK", "home": "HOME"},
}


class CinemaManager:
    """A single instance. Configuration persists; in-flight work never resumes on startup."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.store = Store(hass, STORAGE_VERSION, DOMAIN, atomic_writes=True)
        self.youtube_pairing_store = Store(hass, 1, f"{DOMAIN}.youtube_pairing", private=True, atomic_writes=True)
        self.config = {"revision": 0, "rooms": [], "favorites": []}
        self.jobs: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.listeners: set[Callable] = set()
        self._save_lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()
        self._youtube_pair_lock = asyncio.Lock()
        self._diagnostic_lock = asyncio.Lock()
        self._youtube_pairing: dict | None = None
        self._reserved: set[str] = set()
        self._profile_waiters: dict[str, asyncio.Future] = {}
        self._unsub: Callable | None = None
        self._device_unsub: Callable | None = None
        self.closed = False
        self.saving = False
        self.disney_launcher: SamsungDisneyLauncher | None = None
        self.plex_launcher: PlexLauncher | None = None
        self.device_access = DeviceAccess(hass)
        self.start_assistant = StartAssistant(hass)
        self._standard_reports: dict[str, dict] = {}

    async def async_load(self) -> None:
        await self.device_access.async_load()
        await self.start_assistant.async_load()
        if self._unsub:
            self._unsub()
        saved = await self.store.async_load()
        pairing = await self.youtube_pairing_store.async_load()
        self._youtube_pairing = pairing if isinstance(pairing, dict) else None
        try:
            self.disney_launcher = await self.hass.async_add_executor_job(
                SamsungDisneyLauncher.load, self.hass.config.path(PROFILE_FILE)
            )
        except (OSError, ValueError, KeyError, TypeError, CinemaError):
            _LOGGER.warning("Das optionale Samsung-Disney+-Profil ist ungültig")
        try:
            self.plex_launcher = await self.hass.async_add_executor_job(
                PlexLauncher.load, self.hass.config.path(PLEX_PROFILE_FILE)
            )
        except (OSError, ValueError, KeyError, TypeError, CinemaError):
            _LOGGER.warning("Das optionale lokale Plex-Profil ist ungültig")
        if saved is not None:
            # Invalid data fails setup visibly; it is never silently discarded.
            self.config = {"revision": int(saved["revision"]),
                           "rooms": [room_record(x, x["id"]) for x in saved["rooms"]],
                           "favorites": [favorite_record(x, x["id"]) for x in saved["favorites"]]}
        self._unsub = self.hass.bus.async_listen("state_changed", self._state_changed)
        if self._device_unsub:
            self._device_unsub()
        self._device_unsub = self.hass.bus.async_listen("device_registry_updated", self._device_changed)

    @callback
    def _device_changed(self, event) -> None:
        changes = event.data.get("changes", {})
        if event.data.get("action") == "update" and isinstance(changes, dict) and not set(changes).intersection({"sw_version", "model", "manufacturer", "hw_version"}):
            return
        device_id = event.data.get("device_id")
        registry = er.async_get(self.hass)
        for room in self.config["rooms"]:
            for target in room["targets"]:
                entry = registry.async_get(target["entity_id"])
                if entry and entry.device_id == device_id:
                    self.start_assistant.invalidate(room, target)
        self.notify()

    @callback
    def _state_changed(self, event) -> None:
        if event.data.get("entity_id") in self.configured_entities():
            self.notify()

    @callback
    def notify(self) -> None:
        for listener in tuple(self.listeners):
            listener()

    async def async_close(self) -> None:
        self.closed = True
        self.start_assistant.close()
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self._device_unsub:
            self._device_unsub()
            self._device_unsub = None
        self.listeners.clear()
        tasks = tuple(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self._reserved.clear()

    def configured_entities(self, room: dict | None = None) -> set[str]:
        entities = set()
        for item in ([room] if room else self.config["rooms"]):
            entities.add(item["tv_entity_id"])
            for target in item["targets"]:
                entities.add(target["entity_id"])
                if target["remote_entity_id"]:
                    entities.add(target["remote_entity_id"])
        return entities

    def resolve(self, room_id: str, target_id: str) -> tuple[dict, dict]:
        room = next((r for r in self.config["rooms"] if r["id"] == room_id), None)
        if room is None:
            raise CinemaError("Der Raum existiert nicht mehr. Bitte neu laden.", "not_found")
        target = next((t for t in room["targets"] if t["id"] == target_id), None)
        if target is None:
            raise CinemaError("Das Wiedergabeziel existiert nicht mehr.", "not_found")
        return room, target

    def job_entities(self, room: dict, target: dict) -> set[str]:
        return {x for x in (room["tv_entity_id"], target["entity_id"], target["remote_entity_id"]) if x}

    async def diagnose(self, room: dict, target: dict, app: str, observation: str = "not_tested") -> dict:
        """Collect a bounded, passive beta report for an already configured device."""
        from .diagnostics import async_collect_diagnostics

        if (self.closed or self._diagnostic_lock.locked()
                or (self.tasks.get(room["id"]) is not None and not self.tasks[room["id"]].done())):
            raise CinemaError("Bitte den laufenden Ablauf abwarten und die Diagnose erneut starten.", "busy")
        state = self.hass.states.get(target["entity_id"])
        sources = state.attributes.get("source_list", []) if state else []
        allowed = set(PROVIDERS)
        if isinstance(sources, list):
            allowed.update(source for source in sources if isinstance(source, str) and len(source) <= 512)
        if (not isinstance(app, str) or not app or len(app) > 512 or app not in allowed
                or re.fullmatch(r"(?:TV|HDMI(?:[\s-]*\d+)?)", app.strip(), re.I)):
            raise CinemaError("Bitte eine vorhandene App oder einen Streamingdienst auswählen.", "invalid_app")
        if observation not in ("not_tested", "app_only", "profile_selection", "title_page", "playing", "failed"):
            raise CinemaError("Bitte ein gültiges Testergebnis auswählen.", "invalid_observation")
        async with self._diagnostic_lock:
            async with asyncio.timeout(20):
                from .diagnostics import diff_reports
                previous = self.start_assistant.get_report(room, target, app)
                report = await async_collect_diagnostics(self, room, target, app, observation)
                diff = diff_reports(previous, report)
                report["diff"] = {"changed_fields":diff["changed"], "same_technical_findings":diff["same_technical_findings"]}
                self.start_assistant.set_report(room, target, app, report)
                return self.start_assistant.get_report(room, target, app)

    def _assistant_identity(self, room: dict, target: dict, app: str) -> dict:
        report = self.start_assistant.get_report(room, target, app)
        if report is None:
            raise CinemaError("Bitte zuerst einen aktuellen Diagnosebericht erstellen.", "diagnostic_required")
        if report.get("identity_match") is False:
            raise CinemaError("Die TV-Adresse gehört offenbar zu einem anderen Gerätemodell. Bitte die Geräteverbindung prüfen.", "device_identity_mismatch")
        return {**report, "binding_signature":self.device_access.signature(room, target)}

    async def standard_diagnose(self, data: dict) -> dict:
        """One explicit connection-and-capture action for an address, independent of HA rooms."""
        from .device_access import _host
        from .standard_diagnostics import (async_collect_standard_diagnostics, diff_standard_reports,
                                           normalize_system, validate_extra_app)
        if (not isinstance(data, dict) or set(data) - {"host","system","extra_app"}
                or self.closed):
            raise CinemaError("Bitte TV-Adresse und Gerätesystem angeben.", "invalid_standard_diagnostic")
        host = _host(data.get("host"))
        system = normalize_system(data.get("system"))
        extra_app = validate_extra_app(data.get("extra_app", ""))
        if self._diagnostic_lock.locked() or self._control_lock.locked():
            raise CinemaError("Eine Diagnose oder Geräteaktion läuft bereits. Bitte kurz warten.", "busy")
        context = _StandardDiagnosticContext(self, host, system)
        cache_key = context.room["id"]
        async with self._diagnostic_lock:
            async with asyncio.timeout(75):
                await self.device_access.async_bind(context.room, context.target, str(host))
                access = context.device_access.resolve(context, context.room, context.target)
                pairing_status = "not_required"
                pairing_details = {}
                if system == "samsungtv":
                    if access is not None and access.pairing is not None:
                        pairing_status = "existing"
                    else:
                        try:
                            await self.device_access.async_pair(context, context.room, context.target)
                            pairing_status = "paired"
                        except SamsungPairingError as error:
                            pairing_status = "unavailable"
                            pairing_details = error.safe_details()
                        except TimeoutError:
                            pairing_status = "unavailable"
                            pairing_details = {"port":8002,"phase":"authorization","reason":"authorization_timeout"}
                        except CinemaError:
                            # A refused/unreachable pairing must still yield a useful partial report.
                            pairing_status = "unavailable"
                            pairing_details = {"port":8002,"phase":"authorization","reason":"pairing_not_completed"}
                report = await async_collect_standard_diagnostics(context, context.room, context.target, system, extra_app)
                report["connection"] = {"pairing":pairing_status, **pairing_details}
                if pairing_status == "unavailable" and "pairing_unavailable" not in report["limitations"]:
                    report["limitations"].append("pairing_unavailable")
                report["diff"] = diff_standard_reports(self._standard_reports.get(cache_key), report)
                if cache_key not in self._standard_reports and len(self._standard_reports) >= 16:
                    self._standard_reports.pop(next(iter(self._standard_reports)))
                self._standard_reports[cache_key] = deepcopy(report)
                return report

    def _assistant_profile(self, room: dict, target: dict, app: str) -> dict | None:
        try:
            identity = self._assistant_identity(room, target, app)
            profile = self.start_assistant.matching_profile(room, target, app, identity)
            # Existing routes already go through their established preparation and confirmations.
            return profile if profile and profile["adapter_id"] != "existing" else None
        except CinemaError:
            return None

    def _assistant_status(self, room: dict, target: dict, app: str) -> dict:
        from .launch_adapters import adapter_options
        return {**self.start_assistant.status(room, target, app),
                "binding":self.device_access.public_binding(room, target, app),
                "adapters":adapter_options(self, room, target, app)}

    async def assistant(self, room: dict, target: dict, app: str, action: str, data: dict, context: Context) -> dict:
        """Admin-only dispatcher. Preparing a plan never starts a title or installs software."""
        from .diagnostics import _app_provider, _validate_app
        from .launch_adapters import adapter_options, dispatch
        app = _validate_app(self, target, app)
        provider = _app_provider(app)
        app = provider or app
        fields = {"status":set(), "bind":{"host","firmware","app_version"}, "pair":set(),
                  "plan":{"favorite_a","favorite_b","adapter_id"},
                  "run":{"session_id","case_id","prepared"},
                  "record":{"session_id","case_id","outcome"}, "save_profile":{"session_id"}, "cancel":{"session_id"}}
        if self.closed or action not in fields or not isinstance(data, dict) or set(data) - fields[action]:
            raise CinemaError("Ungültige Angabe für den Direktstart-Assistenten.", "invalid_assistant_action")
        if action == "status":
            return self._assistant_status(room, target, app)
        if action in ("bind", "pair", "plan", "run", "save_profile") and self.tasks.get(room["id"]) is not None:
            raise CinemaError("Bitte den laufenden Titelstart abwarten oder abbrechen.", "busy")
        if action in ("bind", "pair", "plan", "save_profile") and self._control_lock.locked():
            raise CinemaError("Bitte die laufende Geräteaktion abwarten.", "busy")
        if self._diagnostic_lock.locked() and action not in ("record", "cancel"):
            raise CinemaError("Die Geräteinformationen werden gerade gelesen. Bitte kurz warten.", "busy")
        if action == "bind":
            await self.device_access.async_bind(room, target, data.get("host", ""),
                                                data.get("firmware", ""), data.get("app_version", ""), app=app)
            self.start_assistant.invalidate(room, target)
        elif action == "pair":
            if self.platform(target["entity_id"]) != "samsungtv":
                raise CinemaError("Diese Geräteverbindung benötigt keine Samsung-Kopplung.", "unsupported")
            await self.device_access.async_pair(self, room, target)
            self.start_assistant.invalidate(room, target)
        elif action == "plan":
            first = next((item for item in self.config["favorites"] if item["id"] == data.get("favorite_a")), None)
            second = next((item for item in self.config["favorites"] if item["id"] == data.get("favorite_b")), None)
            if not first or not second or first["provider"] != app or second["provider"] != app:
                raise CinemaError("Bitte zwei unterschiedliche Favoriten der gewählten App auswählen.", "invalid_favorites")
            adapter = next((item for item in adapter_options(self, room, target, app) if item["id"] == data.get("adapter_id") and item["can_test"]), None)
            if adapter is None:
                raise CinemaError("Bitte zuerst einen verfügbaren Startweg auswählen.", "unsupported_adapter")
            self.start_assistant.plan(room, target, first, second, adapter["id"], self._assistant_identity(room, target, app))
        elif action == "run":
            session = self.start_assistant.status(room, target, app).get("session")
            if not session or session["id"] != data.get("session_id"):
                raise CinemaError("Der Testlauf ist nicht mehr aktuell. Bitte neu vorbereiten.", "stale_session")
            identity = self._assistant_identity(room, target, app)
            self.start_assistant.validate_session(room, target, data.get("session_id"), identity)

            async def send(favorite, adapter_id):
                if self.closed or self._control_lock.locked():
                    raise CinemaError("Eine Geräteaktion läuft. Bitte erneut versuchen.", "busy")
                current = next((item for item in self.config["favorites"] if item["id"] == favorite["id"]), None)
                if current is None or current["provider"] != favorite["provider"] or current["url"] != favorite["url"]:
                    raise CinemaError("Der Favorit wurde geändert. Bitte einen neuen Testlauf vorbereiten.", "stale_session")
                current_room, current_target = self.resolve(room["id"], target["id"])
                if current_target["entity_id"] != target["entity_id"]:
                    raise CinemaError("Das Wiedergabeziel wurde geändert.", "stale_session")
                if adapter_id == "existing":
                    return await dispatch(self, current_room, current_target, current, adapter_id, context)
                entities = self.job_entities(current_room, current_target)
                if entities & self._reserved:
                    raise CinemaError("Ein Gerät wird bereits in einem anderen Ablauf verwendet.", "busy")
                async with self._control_lock:
                    self._reserved.update(entities)
                    try:
                        return await dispatch(self, current_room, current_target, current, adapter_id, context)
                    finally:
                        self._reserved.difference_update(entities)

            await self.start_assistant.run_case(room, target, data.get("session_id"), data.get("case_id"), data.get("prepared") is True, send)
        elif action == "record":
            if data.get("outcome") == "not_tested":
                raise CinemaError("Bitte das am Fernseher beobachtete Ergebnis auswählen.", "invalid_observation")
            self.start_assistant.validate_session(room, target, data.get("session_id"), self._assistant_identity(room, target, app))
            self.start_assistant.record_case(room, target, data.get("session_id"), data.get("case_id"), data.get("outcome"))
        elif action == "save_profile":
            identity = self._assistant_identity(room, target, app)
            self.start_assistant.validate_session(room, target, data.get("session_id"), identity)
            await self.start_assistant.async_save_profile(room, target, data.get("session_id"))
        elif action == "cancel":
            self.start_assistant.cancel(room, target, data.get("session_id"))
        self.notify()
        return self._assistant_status(room, target, app)

    def platform(self, entity_id: str) -> str:
        entry = er.async_get(self.hass).async_get(entity_id)
        if (entry and entry.platform == DOMAIN
                and entity_id in self.hass.data.get(DOMAIN, {}).get("bridged_entity_ids", set())):
            return "samsungtv"
        return entry.platform if entry else ""

    def has_feature(self, entity_id: str, feature: Feature) -> bool:
        state = self.hass.states.get(entity_id)
        return bool(state and int(state.attributes.get("supported_features", 0)) & feature)

    def capabilities(self, room: dict, target: dict) -> dict:
        tv, player = room["tv_entity_id"], target["entity_id"]
        remote = target["remote_entity_id"]
        rs = self.hass.states.get(remote) if remote else None
        state = self.hass.states.get(player)
        tv_state = self.hass.states.get(tv)
        ready = bool(state and state.state in READY_STATES)
        tv_ready = bool(tv_state and tv_state.state in READY_STATES)
        nav = ready and bool(rs and rs.state not in ("unavailable", "unknown")) and (
            target["profile"] in _NAV and self.hass.services.has_service("remote", "send_command"))
        result = {key: bool((tv_ready if key in ("volume_up", "volume_down", "mute", "source") else ready)
                            and self.has_feature(tv if key in ("volume_up", "volume_down", "mute", "source")
                                                 else player, feature))
                  for key, feature in _MEDIA_ACTIONS.items()}
        # Power controls apply to the television. Wake remains available when it is off.
        result["turn_on"] = bool(tv_state and tv_state.state in OFF_STATES and self.has_feature(tv, Feature.TURN_ON))
        result["turn_off"] = tv_ready and self.has_feature(tv, Feature.TURN_OFF)
        result["player_turn_on"] = bool(state and state.state in OFF_STATES and self.has_feature(player, Feature.TURN_ON))
        result["player_turn_off"] = ready and self.has_feature(player, Feature.TURN_OFF)
        result["mute"] = result["mute"] and "is_volume_muted" in (tv_state.attributes if tv_state else {})
        for action in tuple(result):
            service = {"play": "media_play", "pause": "media_pause", "stop": "media_stop",
                       "mute": "volume_mute", "source": "select_source", "player_turn_on": "turn_on",
                       "player_turn_off": "turn_off"}.get(action, action)
            result[action] = result[action] and self.hass.services.has_service("media_player", service)
        result.update({key: bool(nav) for key in ("up", "down", "left", "right", "ok", "back", "home")})
        result["seek_start"] = self._can_seek_start(player)
        result["restart_stream"] = bool(result["seek_start"] and state
                                         and state.state in ("playing", "paused"))
        result["manage_apps"] = self._supports_app_management(player)
        result["netflix_title"] = self._netflix_launcher(room, target) is not None
        result["disney_title"] = self._disney_launcher(room, target) is not None
        result["prime_title"] = result["disney_title"]
        result["plex_title"] = self._plex_launcher(room, target) is not None
        youtube_bridge = self._youtube_bridge(room, target)
        result["youtube_via_main"] = youtube_bridge is not None
        result["youtube_title"] = (youtube_bridge.youtube_available if youtube_bridge is not None
                                   else self._youtube_launcher(room, target) is not None)
        result["youtube_pairable"] = youtube_bridge is None and result["disney_title"]
        for provider in PROVIDERS:
            if self._assistant_profile(room, target, provider) is not None:
                result[provider + "_title"] = True
        return result

    def _can_seek_start(self, entity: str) -> bool:
        return (self.has_feature(entity, Feature.SEEK)
                and self.hass.services.has_service("media_player", "media_seek"))

    def _netflix_launcher(self, room: dict, target: dict):
        player = target["entity_id"]
        if target["profile"] == "samsungtv" and player == room["tv_entity_id"]:
            return self.hass.data.get(DOMAIN, {}).get("netflix_launchers", {}).get(player)
        return None

    def _disney_launcher(self, room: dict, target: dict) -> SamsungDisneyLauncher | None:
        launcher = self.disney_launcher
        if (launcher and target["profile"] == "samsungtv"
                and target["entity_id"] == room["tv_entity_id"] == launcher.player):
            return launcher
        return None

    def _youtube_bridge(self, room: dict, target: dict) -> MainHAClient | None:
        player = target["entity_id"]
        if target["profile"] == "samsungtv" and player == room["tv_entity_id"]:
            client = self.hass.data.get(DOMAIN, {}).get("youtube_bridge_clients", {}).get(player)
            if client is not None and client.youtube_available:
                return client
        return None

    def _youtube_launcher(
        self, room: dict, target: dict,
    ) -> tuple[SamsungDisneyLauncher, tuple[int, str | None], str] | None:
        launcher = self._disney_launcher(room, target)
        if launcher is None:
            return None
        saved = self._youtube_pairing
        if (saved and saved.get("profile") == self._youtube_pairing_profile(launcher)
                and (pairing := samsung_youtube_pairing(saved))):
            return launcher, pairing, YOUTUBE_PAIR_CLIENT_NAME
        registered = er.async_get(self.hass).async_get(target["entity_id"])
        if registered and registered.config_entry_id:
            entry = self.hass.config_entries.async_get_entry(registered.config_entry_id)
            if (entry and entry.domain == "samsungtv" and entry.data.get("method") == "websocket"
                    and entry.data.get("host") == str(launcher.host)
                    and (pairing := samsung_youtube_pairing(entry.data))):
                return launcher, pairing, SAMSUNG_DEFAULT_CLIENT_NAME
        if launcher.youtube_pairing:
            return launcher, launcher.youtube_pairing, SAMSUNG_DEFAULT_CLIENT_NAME
        return None

    @staticmethod
    def _youtube_pairing_profile(launcher: SamsungDisneyLauncher) -> list[str | None]:
        return [str(launcher.host), launcher.player, launcher.main_player]

    async def pair_youtube(self, room: dict, target: dict) -> None:
        if self._youtube_bridge(room, target) is not None:
            raise CinemaError("Dieser Fernseher verwendet die Samsung-Kopplung der Hauptinstanz.",
                              "samsung_youtube_via_main")
        launcher = self._disney_launcher(room, target)
        if launcher is None:
            raise CinemaError("Für dieses Wiedergabeziel fehlt ein unterstütztes Samsung-Launcherprofil.",
                              "samsung_youtube_unsupported")
        if self._youtube_pair_lock.locked():
            raise CinemaError("Am Fernseher läuft bereits eine Kopplung für YouTube.", "busy")
        async with self._youtube_pair_lock:
            port, token = await async_pair_samsung_youtube(launcher.host)
            current_room, current_target = self.resolve(room["id"], target["id"])
            if (self.closed or self._youtube_bridge(current_room, current_target) is not None
                    or self._disney_launcher(current_room, current_target) is not launcher):
                raise CinemaError("Das Wiedergabeziel wurde geändert. Bitte erneut versuchen.", "not_found")
            saved = {"profile": self._youtube_pairing_profile(launcher), "port": port, "token": token}
            await self.youtube_pairing_store.async_save(saved)
            if await self.youtube_pairing_store.async_load() != saved:
                raise CinemaError("Die YouTube-Kopplung konnte nicht gespeichert werden.", "storage_failed")
            self._youtube_pairing = saved
            self.notify()

    def _launcher_remote(self, target: dict, launcher: SamsungDisneyLauncher) -> MainHAClient | None:
        bridge = self.hass.data.get(DOMAIN, {}).get("bridge")
        if (not target["remote_entity_id"] and bridge and launcher.main_remote
                and bridge.client.tv == launcher.main_player):
            return MainHAClient(bridge.client.session, {
                **bridge.client.settings, "main_ha_remote": launcher.main_remote,
            })
        return None

    def _plex_launcher(self, room: dict, target: dict) -> PlexLauncher | None:
        launcher = self.plex_launcher
        if (launcher and target["profile"] == "samsungtv"
                and target["entity_id"] == room["tv_entity_id"] == launcher.player):
            return launcher
        return None

    def _supports_app_management(self, entity: str) -> bool:
        return (self.platform(entity) == "androidtv"
                and self.hass.services.has_service("androidtv", "adb_command")
                and self.hass.services.supports_response("androidtv", "adb_command") != SupportsResponse.NONE)

    async def _adb_app_command(self, entity: str, command: str, context: Context) -> str:
        async with asyncio.timeout(SERVICE_TIMEOUT):
            response = await self.hass.services.async_call(
                "androidtv", "adb_command", {"command": command}, blocking=True,
                return_response=True, target={"entity_id": entity}, context=context,
            )
        output = ((response or {}).get(entity) or {}).get("adb_response")
        if not isinstance(output, str) or len(output) > 131072:
            raise CinemaError("Das Gerät hat keine auswertbare ADB-Antwort geliefert.", "adb_response")
        return output

    async def _android_packages(self, entity: str, context: Context, *, third_party: bool) -> list[str]:
        option = " -3" if third_party else ""
        output = await self._adb_app_command(
            entity, f"pm list packages{option} --user 0 && echo {_LIST_COMPLETE}", context,
        )
        lines = output.strip().splitlines()
        if not lines or lines[-1] != _LIST_COMPLETE or any(
            not line.startswith("package:") for line in lines[:-1]
        ):
            raise CinemaError("Das Gerät hat die Paketliste nicht vollständig geliefert.", "adb_response")
        packages = {line.removeprefix("package:") for line in lines[:-1]}
        if not third_party:
            return sorted(packages)
        return sorted(package for package in packages
                      if len(package) <= 255 and _PACKAGE_ID.fullmatch(package))

    async def manage_apps(self, room: dict, target: dict, context: Context, *,
                          package: str | None = None, device_ref: str | None = None) -> dict:
        entity = target["entity_id"]
        async with self._control_lock:
            if self.closed or self.saving or self.job_entities(room, target) & self._reserved:
                raise CinemaError("Das Gerät wird gerade verwendet. Bitte später erneut versuchen.", "busy")
            entry = er.async_get(self.hass).async_get(entity)
            if not entry or not self._supports_app_management(entity):
                raise CinemaError("Diese Anbindung bietet keine App-Deinstallation.", "unsupported")
            state = self.hass.states.get(entity)
            if not state or state.state not in READY_STATES:
                raise CinemaError("Das Wiedergabegerät muss eingeschaltet und erreichbar sein.", "offline")
            if package is None:
                packages = await self._android_packages(entity, context, third_party=True)
                return {"packages": packages, "device_ref": entry.id}
            if device_ref != entry.id or len(package) > 255 or not _PACKAGE_ID.fullmatch(package):
                raise CinemaError("Gerät oder App ist ungültig. Bitte die App-Liste neu laden.", "invalid_app")
            if package not in await self._android_packages(entity, context, third_party=True):
                raise CinemaError("Die App ist nicht mehr als Drittanbieter-App vorhanden.", "invalid_app")
            current_entry = er.async_get(self.hass).async_get(entity)
            state = self.hass.states.get(entity)
            if not current_entry or current_entry.id != device_ref or not state or state.state not in READY_STATES:
                raise CinemaError("Das Wiedergabegerät ist nicht mehr verfügbar.", "offline")

            # Operate only on the primary user. Never use flags that remove system apps.
            try:
                output = await self._adb_app_command(entity, f"pm uninstall --user 0 {package}", context)
                if output.strip() != "Success":
                    raise CinemaError("Das Gerät hat die Deinstallation nicht bestätigt.", "uninstall_unverified")
                remaining = await self._android_packages(entity, context, third_party=False)
            except (TimeoutError, CinemaError, HomeAssistantError) as err:
                raise CinemaError("Entfernung nicht bestätigt. Bitte App-Liste neu laden.",
                                  "uninstall_unverified") from err
            if package in remaining:
                raise CinemaError("Das Paket wird weiterhin als installiert gemeldet.", "uninstall_unverified")
            return {"removed": package}

    def snapshot(self, can_read: Callable[[str], bool]) -> dict:
        registry = er.async_get(self.hass)
        entities = []
        for state in self.hass.states.async_all():
            if state.domain not in ("media_player", "remote") or not can_read(state.entity_id):
                continue
            entry = registry.async_get(state.entity_id)
            entities.append({"entity_id": state.entity_id, "name": state.name, "state": state.state,
                             "platform": self.platform(state.entity_id), "source": state.attributes.get("source"),
                             "sources": state.attributes.get("source_list", []),
                             "area_id": entry.area_id if entry else None,
                             "app_id": state.attributes.get("app_id"),
                             "media_title": state.attributes.get("media_title"),
                             "volume_level": state.attributes.get("volume_level"),
                             "is_volume_muted": state.attributes.get("is_volume_muted")})
        rooms = []
        for room in self.config["rooms"]:
            if not all(can_read(e) for e in self.configured_entities(room)):
                continue
            item = deepcopy(room)
            for target in item["targets"]:
                target["capabilities"] = self.capabilities(room, target)
            rooms.append(item)
        allowed_rooms = {r["id"] for r in rooms}
        areas = [{"id": a.id, "name": a.name} for a in ar.async_get(self.hass).async_list_areas()]
        return {"revision": self.config["revision"], "rooms": rooms, "entities": entities,
                "areas": areas, "favorites": deepcopy(self.config["favorites"]), "providers": PROVIDERS,
                "jobs": {k: deepcopy(v) for k, v in self.jobs.items() if k in allowed_rooms}}

    def _validate_entities(self, room: dict) -> None:
        for existing in self.config["rooms"]:
            if existing["id"] != room["id"] and self.configured_entities(existing) & self.configured_entities(room):
                raise CinemaError(f"Ein Gerät ist bereits dem Raum ‚{existing['name']}‘ zugeordnet.")
        for entity in self.configured_entities(room):
            if self.hass.states.get(entity) is None:
                raise CinemaError(f"Gerät {entity} ist in dieser HA-Instanz nicht vorhanden.")
        for target in room["targets"]:
            profile = target["profile"]
            if profile != "generic" and self.platform(target["entity_id"]) != profile:
                raise CinemaError("Geräteprofil und vorhandene HA-Integration passen nicht zusammen.")
            remote = target["remote_entity_id"]
            if remote:
                registry = er.async_get(self.hass)
                player_entry = registry.async_get(target["entity_id"])
                remote_entry = registry.async_get(remote)
                if profile != "generic" and self.platform(remote) != profile:
                    raise CinemaError("Die Fernbedienung gehört zu einer anderen HA-Integration.")
                if (player_entry and remote_entry and player_entry.device_id and remote_entry.device_id
                        and player_entry.device_id != remote_entry.device_id):
                    raise CinemaError("Fernbedienung und Wiedergabeziel gehören zu verschiedenen Geräten.")

    async def async_save(self, kind: str, data: dict | None, item_id: str, revision: int) -> None:
        async with self._save_lock:
            if revision != self.config["revision"]:
                raise CinemaError("Die Einstellungen wurden inzwischen geändert. Bitte neu laden.", "conflict")
            if self.tasks or self._control_lock.locked():
                raise CinemaError("Bitte den laufenden Start beenden, bevor Einstellungen geändert werden.", "busy")
            if kind not in ("rooms", "favorites"):
                raise CinemaError("Unbekannte Datenart.")
            updated = deepcopy(self.config)
            items = updated[kind]
            existing = next((x for x in items if x["id"] == item_id), None)
            if data is None:
                if existing is None:
                    raise CinemaError("Der Eintrag existiert nicht mehr.", "not_found")
                items.remove(existing)
            else:
                validated = room_record(data, item_id) if kind == "rooms" else favorite_record(data, item_id)
                if kind == "rooms":
                    self._validate_entities(validated)
                if existing is not None:
                    items[items.index(existing)] = validated
                else:
                    if len(items) >= (32 if kind == "rooms" else 500):
                        raise CinemaError("Die maximale Anzahl der Einträge ist erreicht.")
                    items.append(validated)
            updated["revision"] += 1
            self.saving = True
            try:
                await self.store.async_save(updated)
                # HA Store logs some write errors instead of raising them. Verify the saved
                # record through its read API before reporting success to the user.
                if await self.store.async_load() != updated:
                    raise CinemaError("Die Einstellungen konnten nicht dauerhaft gespeichert werden. Bitte Speicherzugriff prüfen.", "storage_failed")
            finally:
                self.saving = False
            self.config = updated
            if kind == "rooms":
                self.jobs.pop(item_id, None)
            self.notify()

    async def _service(self, domain: str, service: str, entity: str, context: Context, **data) -> None:
        if not self.hass.services.has_service(domain, service):
            raise CinemaError(f"Die HA-Aktion {domain}.{service} ist nicht verfügbar.")
        async with asyncio.timeout(SERVICE_TIMEOUT):
            await self.hass.services.async_call(domain, service, data, blocking=True,
                                                target={"entity_id": entity}, context=context)

    async def _wait(self, predicate: Callable[[], bool], seconds: float, message: str) -> None:
        try:
            async with asyncio.timeout(seconds):
                while not predicate():
                    await asyncio.sleep(0.2)
        except TimeoutError as err:
            raise CinemaError(message, "timeout") from err

    def _stage(self, job: dict, phase: str, message: str) -> None:
        job.update(phase=phase, message=message)
        job["steps"].append(message)
        self.notify()

    async def _ensure_on(self, entity: str, context: Context, job: dict) -> None:
        state = self.hass.states.get(entity)
        if not state or state.state in ("unknown", "unavailable"):
            raise CinemaError(f"{state.name if state else entity} ist nicht erreichbar.", "unavailable")
        if state.state in READY_STATES:
            return
        if state.state not in OFF_STATES or not self.has_feature(entity, Feature.TURN_ON):
            raise CinemaError(f"{state.name} kann über diese HA-Integration nicht aufgeweckt werden.")
        self._stage(job, "preparing", f"{state.name} wird aufgeweckt …")
        await self._service("media_player", "turn_on", entity, context)
        await self._wait(lambda: bool((s := self.hass.states.get(entity)) and s.state in READY_STATES),
                         WAKE_TIMEOUT, f"{state.name} hat das Einschalten nicht bestätigt.")

    def start(self, room_id: str, target_id: str, favorite_id: str | None, provider: str | None,
              context: Context, *, prepare_netflix: bool = False, assistant_existing: bool = False) -> dict:
        if self.closed:
            raise CinemaError("Kinosteuerung wird beendet.")
        if self.saving:
            raise CinemaError("Einstellungen werden gespeichert. Bitte kurz warten.", "busy")
        if self._control_lock.locked():
            raise CinemaError("Eine Geräteaktion läuft. Bitte kurz warten.", "busy")
        room, target = self.resolve(room_id, target_id)
        favorite = next((x for x in self.config["favorites"] if x["id"] == favorite_id), None)
        if favorite_id and favorite is None:
            raise CinemaError("Der Favorit existiert nicht mehr.", "not_found")
        provider = favorite["provider"] if favorite else provider
        app = target["apps"].get(provider)
        learned = self._assistant_profile(room, target, provider) if favorite and not assistant_existing else None
        if prepare_netflix:
            if favorite_id or provider != "netflix":
                raise CinemaError("Ungültige Netflix-Vorbereitung.")
            # A reported native app can be prepared without a provider mapping.
            if target["profile"] == "samsungtv":
                state = self.hass.states.get(target["entity_id"])
                if not state or "Netflix" not in state.attributes.get("source_list", []):
                    raise CinemaError("Der Fernseher meldet die Netflix-App derzeit nicht.")
                target = deepcopy(target)
                app = {**(app or {}), "mode": "app", "app_id": "Netflix", "tested": "untested"}
                target["apps"]["netflix"] = app
        direct_netflix = bool(favorite and provider == "netflix" and self._netflix_launcher(room, target))
        direct_disney = bool(favorite and provider == "disney" and self._disney_launcher(room, target))
        direct_prime = bool(favorite and provider == "prime" and self._disney_launcher(room, target))
        direct_youtube = False
        if favorite and provider == "youtube":
            if youtube_bridge := self._youtube_bridge(room, target):
                direct_youtube = True
            else:
                direct_youtube = self._youtube_launcher(room, target) is not None
        plex = self._plex_launcher(room, target) if favorite and provider == "plex" else None
        direct_title = direct_netflix or direct_disney or direct_prime or direct_youtube or plex is not None or learned is not None
        if favorite and provider == "plex":
            if plex is None and learned is None:
                raise CinemaError("Für dieses Wiedergabeziel ist kein lokaler Plex-Player eingerichtet.")
            link = urlsplit(favorite["url"])
            if plex is not None and link.scheme != "plex" and f"{link.scheme}://{link.netloc}" != plex.server_url:
                raise CinemaError("Der Favorit gehört zu einem anderen Plex-Server.")
        if direct_disney:
            disney_title_id(favorite["url"])
        if direct_youtube:
            youtube_video_id(favorite["url"])
        if favorite and provider == "youtube" and not direct_youtube and learned is None and self._disney_launcher(room, target):
            raise CinemaError("Wähle in der Einrichtung „YouTube verbinden“ und bestätige die Kopplung am TV.",
                              "samsung_youtube_pairing")
        if favorite and not direct_title and target["profile"] not in ("androidtv", "androidtv_remote"):
            raise CinemaError("Diese Samsung-/Quellenverbindung öffnet nur Apps. "
                              "Sie bietet keinen automatischen Serien- oder Folgenstart.", "unsupported_content")
        if not direct_title and (not app or app["mode"] == "off"):
            raise CinemaError("Diese App ist auf dem gewählten Wiedergabeziel noch nicht eingerichtet.")
        if favorite and not direct_title and app["mode"] != "link":
            raise CinemaError("Hier ist nur der Appstart eingerichtet. Bitte ausdrücklich ‚App öffnen‘ wählen.")
        if provider == "netflix" and not direct_netflix and learned is None and not (app or {}).get("profile_name", "").strip():
            raise CinemaError("Bitte unter Einrichtung → Raum bearbeiten → Netflix-Profil "
                              "den gewünschten Profilnamen speichern.", "profile_required")
        if favorite and favorite.get("playback") == "restart" and plex is None and (
                direct_title or not self._can_seek_start(target["entity_id"])):
            raise CinemaError("Dieses Wiedergabeziel kann den Titel nicht auf 0:00 setzen. "
                              "Wähle beim Favoriten Fortsetzen oder ein anderes Gerät.", "unsupported")
        active = self.jobs.get(room_id)
        if room_id in self.tasks:
            if (active["target_id"], active["favorite_id"], active["provider"]) == (target_id, favorite_id, provider):
                return deepcopy(active)
            raise CinemaError("In diesem Raum läuft bereits ein Start. Bitte warten oder abbrechen.", "busy")
        entities = self.job_entities(room, target)
        if entities & self._reserved:
            raise CinemaError("Ein zugeordnetes Gerät wird gerade in einem anderen Ablauf verwendet.", "busy")
        job = {"id": uuid4().hex, "room_id": room_id, "target_id": target_id,
               "favorite_id": favorite_id, "provider": provider, "phase": "preparing",
               "message": "Geräte werden vorbereitet …", "steps": [], "result": "pending",
               "started_at": datetime.now(UTC).isoformat()}
        if learned:
            job["assistant_adapter"] = learned["adapter_id"]
        self.jobs[room_id] = job
        self._reserved.update(entities)
        self.tasks[room_id] = self.hass.async_create_task(
            self._run(deepcopy(room), deepcopy(target), deepcopy(favorite), provider, context, job, entities),
            f"Kinosteuerung {room_id}", eager_start=False)
        self.notify()
        return deepcopy(job)

    def confirm_profile(self, room_id: str, target_id: str, job_id: str, context: Context) -> dict:
        room, target = self.resolve(room_id, target_id)
        job = self.jobs.get(room_id)
        future = self._profile_waiters.get(job_id)
        if (not job or job["id"] != job_id or job["target_id"] != target_id
                or job["phase"] != "awaiting_profile" or not future or future.done()):
            raise CinemaError("Diese Profilbestätigung ist nicht mehr aktuell. Bitte neu laden.", "conflict")
        self._check_prepared(room, target)
        # This is a deliberate user assertion, never device-detected profile feedback.
        job["profile_confirmation"] = "user_confirmed"
        job["profile_confirmed_at"] = datetime.now(UTC).isoformat()
        future.set_result(True)
        self.notify()
        return deepcopy(job)

    async def _await_netflix_profile(self, room: dict, target: dict, job: dict) -> None:
        name = target["apps"]["netflix"]["profile_name"]
        future = asyncio.get_running_loop().create_future()
        self._profile_waiters[job["id"]] = future
        job.update(profile_name=name, profile_confirmation="required", result="profile_required")
        def ready() -> bool:
            self._check_prepared(room, target)
            return future.done()
        try:
            self._stage(job, "awaiting_profile", f"Bitte am TV Netflix-Profil ‚{name}‘ wählen und hier bestätigen. "
                        "Der Profilname wird vom Fernseher nicht zurückgemeldet.")
            await self._wait(ready, PROFILE_TIMEOUT,
                             f"Profil ‚{name}‘ wurde innerhalb von zwei Minuten nicht bestätigt. "
                             "Vorbereitung beendet; du kannst sie erneut starten.")
        finally:
            self._profile_waiters.pop(job["id"], None)
            if not future.done():
                future.cancel()

    async def _open_app(self, target: dict, app: dict, context: Context) -> None:
        player = target["entity_id"]
        if target["profile"] == "androidtv_remote":
            if not self.has_feature(player, Feature.PLAY_MEDIA):
                raise CinemaError("Die Integration bietet keinen Appstart über play_media.")
            await self._service("media_player", "play_media", player, context,
                                media_content_type="app", media_content_id=app["app_id"])
        else:
            if not self.has_feature(player, Feature.SELECT_SOURCE):
                raise CinemaError("Diese Integration bietet keinen Appstart über die Quellenwahl.")
            if target["profile"] in ("generic", "samsungtv"):
                if app["app_id"] not in self.hass.states.get(player).attributes.get("source_list", []):
                    raise CinemaError("Die eingerichtete App fehlt in der aktuellen Quellenliste.")
            await self._service("media_player", "select_source", player, context, source=app["app_id"])

    async def _run(self, room: dict, target: dict, favorite: dict | None, provider: str,
                   context: Context, job: dict, entities: set[str]) -> None:
        tv, player = room["tv_entity_id"], target["entity_id"]
        try:
            await self._ensure_on(tv, context, job)
            if player != tv:
                await self._ensure_on(player, context, job)
                state = self.hass.states.get(tv)
                source = target["hdmi_source"]
                if state.attributes.get("source") != source:
                    if not self.has_feature(tv, Feature.SELECT_SOURCE):
                        raise CinemaError("Der Fernseher unterstützt keine Eingangswahl über HA.")
                    if source not in state.attributes.get("source_list", []):
                        raise CinemaError(f"Der zugeordnete Eingang ‚{source}‘ fehlt in der TV-Quellenliste.")
                    self._stage(job, "preparing", f"Fernseher auf {source} umschalten …")
                    await self._service("media_player", "select_source", tv, context, source=source)
                    await self._wait(lambda: bool((s := self.hass.states.get(tv)) and
                                                  s.attributes.get("source") == source), SOURCE_TIMEOUT,
                                     "Der Fernseher hat den HDMI-Eingang nicht bestätigt.")
            self._check_prepared(room, target)
            app = target["apps"].get(provider)
            if favorite and job.get("assistant_adapter"):
                from .launch_adapters import dispatch
                current_profile = self._assistant_profile(room, target, provider)
                if not current_profile or current_profile["adapter_id"] != job["assistant_adapter"]:
                    raise CinemaError("Die Geräteverbindung wurde geändert. Bitte den Startweg erneut prüfen.", "stale_device")
                self._stage(job, "dispatching", "Bestätigten lokalen Startweg verwenden …")
                await dispatch(self, room, target, favorite, job["assistant_adapter"], context)
                job["result"] = "command_sent"
                self._stage(job, "done", "Der bestätigte Startweg hat den Titelbefehl gesendet. Aktuelle Wiedergabe am Fernseher prüfen.")
                return
            if favorite and provider == "youtube" and target["profile"] == "samsungtv":
                if youtube_bridge := self._youtube_bridge(room, target):
                    self._stage(job, "dispatching", "YouTube-Video über die Hauptinstanz starten …")
                    await youtube_bridge.youtube_play(youtube_video_id(favorite["url"]))
                else:
                    youtube = self._youtube_launcher(room, target)
                    if youtube is None:
                        raise CinemaError("Die Samsung-Kopplung für YouTube ist nicht mehr verfügbar.",
                                          "samsung_youtube_pairing")
                    launcher, pairing, client_name = youtube
                    self._stage(job, "dispatching", "YouTube-Video an den Samsung senden …")
                    await launcher.launch_youtube(favorite["url"], pairing, client_name=client_name)
                job["result"] = "command_sent"
                self._stage(job, "done", "Samsung hat den YouTube-Aufruf angenommen. Wiedergabe am TV prüfen.")
                return
            if favorite and provider == "plex" and (plex := self._plex_launcher(room, target)):
                self._stage(job, "dispatching", "Plex am Samsung öffnen und Film anfordern …")
                if self.has_feature(player, Feature.PLAY_MEDIA):
                    await self._service("media_player", "play_media", player, context,
                                        media_content_type="app", media_content_id=SAMSUNG_PLEX_APP_ID)
                else:
                    await self._open_app(target, {"app_id": "Plex"}, context)
                rating_key = urlsplit(favorite["url"]).path.rsplit("/", 1)[-1]
                playing = await plex.launch(async_get_clientsession(self.hass), rating_key,
                                            restart=favorite.get("playback") == "restart")
                job["result"] = "playback_confirmed" if playing else "command_sent"
                self._stage(job, "done", "Plex meldet die Wiedergabe des ausgewählten Films am Fernseher."
                            if playing else "Plex hat den Titelbefehl angenommen. Wiedergabe am TV nicht bestätigt.")
                return
            if favorite and provider in ("disney", "prime") and (launcher := self._disney_launcher(room, target)):
                label = PROVIDERS[provider]["name"]
                self._stage(job, "dispatching", f"Filmseite in {label} öffnen …")
                session = async_get_clientsession(self.hass)
                if provider == "prime":
                    await launcher.launch_prime(session, favorite["url"])
                    auto_ok = favorite.get("prime_auto_profile", False)
                    delay = room["prime_profile_delay"]
                    pending = f"Prime Video geöffnet. Profil-OK folgt in {delay} Sekunden …"
                    done = "Prime-Video-Titel und einmal Profil-OK gesendet. Filmseite am TV prüfen."
                    manual = "Prime-Video-Titel gesendet. Falls nötig, wähle dein Profil am TV."
                else:
                    await launcher.launch(session, favorite["url"])
                    auto_ok = launcher.autoplay
                    delay = PLAY_DELAY
                    pending = f"Disney+-Titel geöffnet. OK folgt in {delay} Sekunden …"
                    done = "Disney+-Titel und OK gesendet. Wiedergabe am TV prüfen."
                    manual = "Disney+-Titel gesendet. Falls die Filmseite erscheint, drücke OK."
                job["result"] = "command_sent"
                remote = self._launcher_remote(target, launcher) if auto_ok or provider == "prime" else None
                if provider == "prime" and (remote or self.capabilities(room, target).get("ok")):
                    job["prime_continue"] = True
                if auto_ok and (remote or self.capabilities(room, target).get("ok")):
                    self._stage(job, "dispatching", pending)
                    await asyncio.sleep(delay)
                    if remote:
                        await remote.call("remote", "send_command", {"command": _NAV["samsungtv"]["ok"]})
                    elif self.capabilities(room, target).get("ok"):
                        await self._service("remote", "send_command", target["remote_entity_id"], context,
                                            command=_NAV["samsungtv"]["ok"])
                    else:
                        self._stage(job, "done", manual)
                        return
                    if provider == "prime" and favorite.get("prime_auto_continue"):
                        continue_delay = room["prime_continue_delay"]
                        self._stage(job, "dispatching",
                                    f"Profil-OK gesendet. Wiedergabe-OK folgt in {continue_delay} Sekunden …")
                        await asyncio.sleep(continue_delay)
                        self._check_prepared(room, target)
                        if remote:
                            await remote.call("remote", "send_command", {"command": _NAV["samsungtv"]["ok"]})
                        elif self.capabilities(room, target).get("ok"):
                            await self._service("remote", "send_command", target["remote_entity_id"], context,
                                                command=_NAV["samsungtv"]["ok"])
                        else:
                            self._stage(job, "done", done)
                            return
                        job["prime_continue"] = False
                        done = "Profil-OK und Wiedergabe-OK gesendet. Wiedergabe am TV prüfen."
                    self._stage(job, "done", done)
                    return
                self._stage(job, "done", manual)
                return
            if favorite and provider == "netflix" and (launcher := self._netflix_launcher(room, target)):
                self._stage(job, "dispatching", "Netflix-Titel über die Hauptinstanz senden …")
                content_id = urlsplit(favorite["url"]).path.rstrip("/").rsplit("/", 1)[-1]
                try:
                    await launcher.launch_netflix(content_id)
                except BridgeError as err:
                    raise CinemaError("Netflix-Aufruf fehlgeschlagen. Prüfe die REST-Aktion "
                                      "cinema_netflix_wohnzimmer auf der Hauptinstanz.") from err
                job["result"] = "command_sent"
                profile = (app or {}).get("profile_name")
                prompt = f"Falls Netflix nachfragt, wähle am TV das Profil ‚{profile}‘." if profile else (
                    "Falls Netflix nachfragt, wähle dein Profil am TV.")
                self._stage(job, "done", f"Titelbefehl angenommen. {prompt} Wiedergabe am TV prüfen.")
                return
            if provider == "netflix":
                self._stage(job, "dispatching", "Netflix-App öffnen …")
                await self._open_app(target, app, context)
                await self._await_netflix_profile(room, target, job)
                if not favorite:
                    job["result"] = "profile_user_confirmed"
                    self._stage(job, "done", f"Profil ‚{app['profile_name']}‘ wurde von dir bestätigt. "
                                "Kein Titel angefordert. Wähle die gewünschte Serie oder Folge am TV.")
                    return
            sent_at = datetime.now(UTC)
            self._stage(job, "dispatching", "Inhaltslink senden …" if favorite else "Appstart senden …")
            if favorite:
                if target["profile"] == "androidtv":
                    # Both values have been validated. Shell quoting is required even for trusted URLs.
                    command = ("am start -a android.intent.action.VIEW -d " + shlex.quote(favorite["url"])
                               + " -p " + shlex.quote(app["app_id"]))
                    await self._service("androidtv", "adb_command", player, context, command=command)
                elif target["profile"] == "androidtv_remote":
                    await self._service("media_player", "play_media", player, context,
                                        media_content_type="url", media_content_id=favorite["url"])
                else:
                    raise CinemaError("Kein Inhaltslink-Verfahren für dieses Gerät eingerichtet.")
            else:
                await self._open_app(target, app, context)
            job["result"] = "command_sent"
            self._stage(job, "observing", "Befehl gesendet. Warte auf Geräterückmeldung …")
            deadline = asyncio.get_running_loop().time() + FEEDBACK_TIMEOUT
            while True:
                self._check_prepared(room, target, check_source=player != tv)
                result = self._feedback(target, favorite, app, sent_at)
                job["result"] = result
                if favorite and favorite.get("playback") == "restart" and result in (
                        "title_confirmed", "playback_confirmed"):
                    if not self._can_seek_start(player):
                        raise CinemaError("Das Gerät kann die Wiedergabeposition nicht mehr ändern.")
                    await self._service("media_player", "media_seek", player, context, seek_position=0)
                    job["result"] = "restart_sent"
                    self._stage(job, "done", "Titel erkannt. Befehl für Position 0:00 gesendet.")
                    return
                if result == "playback_confirmed" or (not favorite and result == "app_confirmed"):
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.3)
            if favorite and favorite.get("playback") == "restart":
                raise CinemaError("Der gewünschte Titel wurde nicht erkannt. Position wurde nicht geändert.")
            messages = {
                "playback_confirmed": "Angeforderter Inhalt und laufende Wiedergabe wurden von HA gemeldet.",
                "title_confirmed": "Angeforderter Inhalt gemeldet. Wiedergabe nicht bestätigt.",
                "app_confirmed": "Passende App gemeldet. Titel und Wiedergabe nicht bestätigt.",
                "command_sent": "Befehl gesendet. App, Titel und Wiedergabe nicht bestätigt.",
            }
            self._stage(job, "done", messages[job["result"]])
        except asyncio.CancelledError:
            job["result"] = "cancelled"
            self._stage(job, "cancelled", "Start abgebrochen. Bereits gesendete Befehle bleiben wirksam.")
            raise
        except CinemaError as err:
            job["result"] = "failed"
            self._stage(job, "failed", str(err))
        except TimeoutError:
            job["result"] = "failed"
            self._stage(job, "failed", "Das Gerät hat die HA-Aktion nicht rechtzeitig beantwortet.")
        except Exception:
            # Device exceptions can contain URLs or connection details: no raw exception in the UI/log.
            _LOGGER.error("Kinosteuerung: Geräteaktion fehlgeschlagen (Raum %s)", room["id"])
            job["result"] = "failed"
            self._stage(job, "failed", "Die Geräteaktion ist fehlgeschlagen. Bitte HA-Gerätestatus prüfen.")
        finally:
            self._reserved.difference_update(entities)
            self.tasks.pop(room["id"], None)
            self.notify()

    def _check_prepared(self, room: dict, target: dict, check_source: bool = True) -> None:
        for entity in {room["tv_entity_id"], target["entity_id"]}:
            state = self.hass.states.get(entity)
            if not state or state.state not in READY_STATES:
                raise CinemaError("Ein Gerät wurde ausgeschaltet oder ist nicht mehr erreichbar. Start beendet.")
        if check_source and room["tv_entity_id"] != target["entity_id"]:
            if self.hass.states.get(room["tv_entity_id"]).attributes.get("source") != target["hdmi_source"]:
                raise CinemaError("Der TV-Eingang wurde geändert. Start beendet; keine automatische Rückschaltung.")

    def _feedback(self, target: dict, favorite: dict | None, app: dict, sent_at: datetime) -> str:
        state = self.hass.states.get(target["entity_id"])
        if not state or state.last_updated < sent_at:
            return "command_sent"
        attrs = state.attributes
        title_matches = False
        if favorite and isinstance(attrs.get("media_content_id"), str):
            try:
                title_matches = content_url(favorite["provider"], attrs["media_content_id"]) == favorite["url"]
            except CinemaError:
                pass
        if title_matches:
            return "playback_confirmed" if state.state == "playing" else "title_confirmed"
        if app["app_id"] in (attrs.get("app_id"), attrs.get("source")):
            return "app_confirmed"
        return "command_sent"

    async def cancel(self, room_id: str) -> None:
        if task := self.tasks.get(room_id):
            job = self.jobs[room_id]
            room, target = self.resolve(room_id, job["target_id"])
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # Cancelling before the coroutine's first turn does not execute its finally block.
            if self.tasks.get(room_id) is task:
                self.tasks.pop(room_id, None)
                self._reserved.difference_update(self.job_entities(room, target))
                job["result"] = "cancelled"
                self._stage(job, "cancelled", "Start abgebrochen. Bereits gesendete Befehle bleiben wirksam.")

    async def control(self, room_id: str, target_id: str, action: str, value: str | None,
                      context: Context) -> None:
        room, target = self.resolve(room_id, target_id)
        async with self._control_lock:
            job = self.jobs.get(room_id)
            if self.job_entities(room, target) & self._reserved:
                profile_navigation = bool(job and job["phase"] == "awaiting_profile"
                                          and job["target_id"] == target_id
                                          and action in {*_NAV.get(target["profile"], {}),
                                                         "volume_up", "volume_down", "mute"})
                profile_exit = bool(job and job["phase"] == "awaiting_profile"
                                    and job["target_id"] == target_id
                                    and action in {"turn_off", "player_turn_off", "stop", "source"})
                if (room_id not in self.tasks or not job
                        or (job["phase"] != "observing" and not profile_navigation and not profile_exit)):
                    raise CinemaError("Geräte werden vorbereitet. Bitte kurz warten oder den Start abbrechen.", "busy")
                # A deliberate manual action ends observation; no later status is attributed to the job.
                if not profile_navigation:
                    await self.cancel(room_id)
            if action == "prime_continue":
                launcher = self._disney_launcher(room, target)
                if (not launcher or not job or job["id"] != value or job["target_id"] != target_id
                        or job["provider"] != "prime" or job["phase"] != "done"
                        or not job.get("prime_continue")
                        or (datetime.now(UTC) - datetime.fromisoformat(job["started_at"])).total_seconds() > PROFILE_TIMEOUT):
                    raise CinemaError("Diese Prime-Video-Bestätigung ist abgelaufen. Bitte den Titel erneut öffnen.")
                remote = self._launcher_remote(target, launcher)
                if not remote and not self.capabilities(room, target).get("ok"):
                    raise CinemaError("Die Fernbedienung ist gerade nicht verfügbar.")
                job["prime_continue"] = False
                entities = self.job_entities(room, target)
                self._reserved.update(entities)
                try:
                    if remote:
                        await remote.call("remote", "send_command", {"command": _NAV["samsungtv"]["ok"]})
                    else:
                        await self._service("remote", "send_command", target["remote_entity_id"], context,
                                            command=_NAV["samsungtv"]["ok"])
                    self._stage(job, "done", "Fortsetzen bestätigt. Wiedergabe am TV prüfen.")
                finally:
                    self._reserved.difference_update(entities)
                    self.notify()
                return
            if not self.capabilities(room, target).get(action):
                raise CinemaError("Diese Funktion ist für das Gerät derzeit nicht verfügbar.", "unsupported")
            tv = room["tv_entity_id"]
            if action in _NAV.get(target["profile"], {}):
                await self._service("remote", "send_command", target["remote_entity_id"], context,
                                    command=_NAV[target["profile"]][action])
                return
            entity = tv if action in ("volume_up", "volume_down", "mute", "source", "turn_on", "turn_off") else target["entity_id"]
            service = {"play": "media_play", "pause": "media_pause", "stop": "media_stop",
                       "mute": "volume_mute", "source": "select_source", "player_turn_on": "turn_on",
                       "player_turn_off": "turn_off"}.get(action, action)
            if action == "restart_stream":
                await self._service("media_player", "media_seek", entity, context, seek_position=0)
                state = self.hass.states.get(entity)
                if (state and state.state == "paused" and self.has_feature(entity, Feature.PLAY)
                        and self.hass.services.has_service("media_player", "media_play")):
                    await self._service("media_player", "media_play", entity, context)
                return
            data = {}
            if action == "mute":
                state = self.hass.states.get(tv)
                if "is_volume_muted" not in state.attributes:
                    raise CinemaError("Der Fernseher meldet keinen Stummstatus.")
                data["is_volume_muted"] = not state.attributes["is_volume_muted"]
            if action == "source":
                if value not in self.hass.states.get(tv).attributes.get("source_list", []):
                    raise CinemaError("Der Eingang ist nicht in der aktuellen TV-Quellenliste vorhanden.")
                data["source"] = value
            await self._service("media_player", service, entity, context, **data)
