"""Private, per-target TV network access, separate from playback firmware profiles."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from ipaddress import IPv4Address, IPv4Network
import json
import re
from uuid import uuid4

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import DOMAIN, PROVIDERS
from .models import CinemaError, entity_id, record_id
from .samsung_youtube import (
    SAMSUNG_DEFAULT_CLIENT_NAME, YOUTUBE_PAIR_CLIENT_NAME, async_pair_samsung_youtube,
    samsung_youtube_pairing,
)

_LAN = tuple(map(IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
_VERSION = re.compile(r"[A-Za-z0-9 ._+()/-]{1,120}")
_PRIVATE = re.compile(r"(?:https?://|(?:\d{1,3}\.){3}\d{1,3}|(?:[a-f0-9]{2}[:-]){5}[a-f0-9]{2}|"
                      r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}|"
                      r"\b(?:token|secret|password|bearer|authorization)\b)", re.I)


class AccessSource(StrEnum):
    NATIVE = "native"
    STORED = "stored"
    LEGACY = "legacy"


@dataclass(frozen=True, slots=True)
class TVAccess:
    host: IPv4Address
    pairing: tuple[int, str | None] | None
    client_name: str
    source: AccessSource


def _host(value: object) -> IPv4Address:
    if not isinstance(value, (str, IPv4Address)):
        raise CinemaError("Bitte die lokale IPv4-Adresse des Fernsehers angeben.", "invalid_tv_address")
    try:
        address = IPv4Address(value.strip() if isinstance(value, str) else value)
    except ValueError:
        raise CinemaError("Bitte die lokale IPv4-Adresse des Fernsehers angeben.", "invalid_tv_address") from None
    if not any(address in network for network in _LAN):
        raise CinemaError("Bitte die lokale IPv4-Adresse des Fernsehers angeben.", "invalid_tv_address")
    return address


def _reported_version(value: object) -> str:
    if not isinstance(value, str) or len(value) > 120:
        raise CinemaError("Firmware und App-Version dürfen höchstens 120 Zeichen enthalten.", "invalid_version")
    value = value.strip()
    if value and (not _VERSION.fullmatch(value) or _PRIVATE.search(value) or re.fullmatch(r"[a-f0-9]{32,}", value, re.I)):
        raise CinemaError("Bitte nur die öffentliche Firmware- oder App-Versionsnummer angeben.", "invalid_version")
    return value


def _scope(room: dict, target: dict) -> tuple[str, str, str]:
    return record_id(room.get("id")), record_id(target.get("id")), entity_id(target.get("entity_id"))


def _app_scope(app: object) -> str:
    if not isinstance(app, str) or len(app) > 512 or any(ord(char) < 32 for char in app):
        raise CinemaError("Bitte eine gültige App auswählen.", "invalid_app")
    key = re.sub(r"[\s_-]+", "", app.strip().casefold())
    aliases = {re.sub(r"[\s_-]+", "", value["name"].casefold()): name for name, value in PROVIDERS.items()}
    aliases.update({"disneyplus": "disney", "amazonprimevideo": "prime", "appletv": "apple"})
    return key if key in PROVIDERS else aliases.get(key, app.strip().casefold())


class DeviceAccess:
    """Store manual bindings privately. No discovery, pairing or TV call on load/bind."""

    def __init__(self, hass) -> None:
        self.hass = hass
        self.store = Store(hass, 1, f"{DOMAIN}.device_access", private=True, atomic_writes=True)
        self._bindings: dict[tuple[str, str], dict] = {}
        self._save_lock = asyncio.Lock()
        self._pair_lock = asyncio.Lock()
        self.invalid_saved_data = False

    async def async_load(self) -> None:
        self.invalid_saved_data = False
        saved = await self.store.async_load()
        if saved is None:
            self._bindings = {}
            return
        try:
            if not isinstance(saved, dict) or not isinstance(saved.get("bindings"), list) or len(saved["bindings"]) > 64:
                raise ValueError
            loaded = {}
            for item in saved["bindings"]:
                if not isinstance(item, dict):
                    raise ValueError
                room_id, target_id = record_id(item.get("room_id")), record_id(item.get("target_id"))
                player = entity_id(item.get("entity_id"))
                pairing = samsung_youtube_pairing(item)
                if (item.get("port") is not None or item.get("token") is not None) and pairing is None:
                    raise ValueError
                revision = item.get("revision")
                if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{32}", revision):
                    raise ValueError
                binding = {"room_id": room_id, "target_id": target_id, "entity_id": player,
                           "host": str(_host(item.get("host"))), "firmware": _reported_version(item.get("firmware", "")),
                           "app_version": _reported_version(item.get("app_version", "")),
                           "version_app": _app_scope(item.get("version_app", "")), "revision": revision}
                if pairing is not None:
                    binding.update(port=pairing[0], token=pairing[1])
                prefer_stored = item.get("prefer_stored", False)
                if type(prefer_stored) is not bool or (prefer_stored and pairing is None):
                    raise ValueError
                if prefer_stored:
                    binding["prefer_stored"] = True
                key = (room_id, target_id)
                if key in loaded:
                    raise ValueError
                loaded[key] = binding
            self._bindings = loaded
        except (CinemaError, TypeError, ValueError):
            # Fail closed without overwriting a potentially recoverable private source.
            self._bindings = {}
            self.invalid_saved_data = True

    def _binding(self, room: dict, target: dict) -> dict | None:
        room_id, target_id, player = _scope(room, target)
        value = self._bindings.get((room_id, target_id))
        return value if value is not None and value["entity_id"] == player else None

    def _native(self, target: dict) -> TVAccess | None:
        registered = er.async_get(self.hass).async_get(entity_id(target.get("entity_id")))
        if registered is None or not registered.config_entry_id:
            return None
        entry = self.hass.config_entries.async_get_entry(registered.config_entry_id)
        if entry is None or entry.domain != "samsungtv":
            return None
        try:
            address = _host(entry.data.get("host"))
        except CinemaError:
            return None
        pairing = samsung_youtube_pairing(entry.data) if entry.data.get("method") == "websocket" else None
        return TVAccess(address, pairing, SAMSUNG_DEFAULT_CLIENT_NAME, AccessSource.NATIVE)

    async def _save(self, bindings: dict) -> None:
        if len(bindings) > 64:
            raise CinemaError("Es können höchstens 64 TV-Verbindungen gespeichert werden.", "device_access_limit")
        await self.store.async_save({"bindings": list(bindings.values())})
        self._bindings = bindings

    async def async_bind(self, room: dict, target: dict, host: str,
                         firmware: str = "", app_version: str = "", app: str = "") -> dict:
        room_id, target_id, player = _scope(room, target)
        address = _host(host)
        firmware, app_version = _reported_version(firmware), _reported_version(app_version)
        version_app = _app_scope(app)
        native = self._native(target)
        if native is not None and native.host != address:
            raise CinemaError("Die Adresse muss zum gewählten Home-Assistant-Gerät passen.", "device_access_mismatch")
        async with self._save_lock:
            current = self._binding(room, target)
            value = {"room_id": room_id, "target_id": target_id, "entity_id": player, "host": str(address),
                     "firmware": firmware, "app_version": app_version, "version_app": version_app, "revision": uuid4().hex}
            if current is not None and current["host"] == str(address):
                pairing = samsung_youtube_pairing(current)
                if pairing is not None:
                    value.update(port=pairing[0], token=pairing[1])
                    if current.get("prefer_stored") is True:
                        value["prefer_stored"] = True
            updated = {**self._bindings, (room_id, target_id): value}
            await self._save(updated)
        return self.public_binding(room, target, app)

    async def async_pair(self, manager, room: dict, target: dict, *, force: bool = False) -> dict:
        if type(force) is not bool:
            raise CinemaError("Ungültige Geräteverbindung.", "invalid_pairing")
        if manager.platform(target["entity_id"]) != "samsungtv":
            raise CinemaError("Die direkte Kopplung ist derzeit für Samsung verfügbar.", "device_access_unsupported")
        if self._pair_lock.locked():
            raise CinemaError("Eine TV-Kopplung läuft bereits. Bitte kurz warten.", "busy")
        async with self._pair_lock:
            before = self._binding(room, target)
            if before is None:
                raise CinemaError("Bitte zuerst die Adresse dieses Fernsehers speichern.", "device_access_missing")
            snapshot = dict(before)
            address = _host(snapshot["host"])
            native = self._native(target)
            if native is not None and native.host != address:
                raise CinemaError("Die gespeicherte Adresse passt nicht mehr zum gewählten Gerät.", "device_access_mismatch")
            if not force and native is not None and native.pairing is not None:
                return self.public_binding(room, target)
            port, token = await async_pair_samsung_youtube(address)
            pairing = samsung_youtube_pairing({"port": port, "token": token})
            if pairing is None:
                raise CinemaError("Der Fernseher hat keinen gültigen Kopplungsschlüssel geliefert.", "device_access_pairing")
            async with self._save_lock:
                if self._binding(room, target) != snapshot or getattr(manager, "closed", False):
                    raise CinemaError("Die TV-Verbindung wurde inzwischen geändert. Bitte erneut verbinden.", "device_access_changed")
                current_room, current_target = manager.resolve(room["id"], target["id"])
                if _scope(current_room, current_target) != _scope(room, target):
                    raise CinemaError("Das gewählte Gerät wurde inzwischen geändert.", "device_access_changed")
                updated = {**snapshot, "port": pairing[0], "token": pairing[1], "revision": uuid4().hex}
                if force:
                    updated["prefer_stored"] = True
                await self._save({**self._bindings, (room["id"], target["id"]): updated})
            return self.public_binding(room, target)

    def resolve(self, manager, room: dict, target: dict) -> TVAccess | None:
        native = self._native(target)
        binding = self._binding(room, target)
        if binding is not None and binding.get("prefer_stored") is True:
            address = _host(binding["host"])
            if native is None or native.host == address:
                pairing = samsung_youtube_pairing(binding)
                if pairing is not None:
                    return TVAccess(address, pairing, YOUTUBE_PAIR_CLIENT_NAME, AccessSource.STORED)
        if native is not None and native.pairing is not None:
            return native
        if binding is None:
            return native
        address = _host(binding["host"])
        if native is not None and native.host != address:
            return native
        pairing = samsung_youtube_pairing(binding)
        if pairing is not None:
            return TVAccess(address, pairing, YOUTUBE_PAIR_CLIENT_NAME, AccessSource.STORED)
        # A pre-existing firmware launcher is only a credential fallback for this exact binding.
        launcher = getattr(manager, "_disney_launcher", lambda *_: None)(room, target)
        if launcher is not None and getattr(launcher, "player", None) == target["entity_id"]:
            try:
                same_host = _host(launcher.host) == address
            except CinemaError:
                same_host = False
            if same_host:
                saved = getattr(manager, "_youtube_pairing", None)
                expected = [str(address), launcher.player, getattr(launcher, "main_player", None)]
                old_pairing = samsung_youtube_pairing(saved) if isinstance(saved, dict) and saved.get("profile") == expected else None
                if old_pairing is not None:
                    return TVAccess(address, old_pairing, YOUTUBE_PAIR_CLIENT_NAME, AccessSource.LEGACY)
                old_pairing = getattr(launcher, "youtube_pairing", None)
                if old_pairing is not None:
                    return TVAccess(address, old_pairing, SAMSUNG_DEFAULT_CLIENT_NAME, AccessSource.LEGACY)
        return TVAccess(address, None, YOUTUBE_PAIR_CLIENT_NAME, AccessSource.STORED)

    def public_binding(self, room: dict, target: dict, app: str = "") -> dict:
        """Administrator form state only. Never include this mapping in exported diagnostics."""
        binding = self._binding(room, target)
        native = self._native(target)
        if native is not None and binding is not None and binding["host"] != str(native.host):
            binding = None
        address = binding["host"] if binding is not None else str(native.host) if native else ""
        paired = bool((native is not None and str(native.host) == address and native.pairing is not None)
                      or (binding is not None and samsung_youtube_pairing(binding) is not None))
        return {"host": address, "firmware": binding["firmware"] if binding else "",
                "app_version": binding["app_version"] if binding and binding.get("version_app", "") == _app_scope(app) else "", "paired": paired}

    def signature(self, room: dict, target: dict) -> str:
        """Internal binding revision for stale-operation detection, never report identity."""
        binding = self._binding(room, target)
        native = self._native(target)
        value = {"scope": _scope(room, target), "binding": binding,
                 "native": {"host": str(native.host), "pairing": native.pairing} if native else None}
        return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
