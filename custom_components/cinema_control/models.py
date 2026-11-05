"""Validated, device-independent configuration. No remote code or arbitrary commands."""

from __future__ import annotations

from ipaddress import IPv4Address, IPv4Network
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .const import PRIME_CONTINUE_DELAY, PRIME_DELAY_MAX, PRIME_PROFILE_DELAY, PROVIDERS


_CONTENT_HOSTS = {
    "netflix": {"netflix.com", "www.netflix.com"},
    "disney": {"disneyplus.com", "www.disneyplus.com"},
    "prime": {"primevideo.com", "www.primevideo.com"},
    "apple": {"tv.apple.com"},
    "youtube": {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"},
}
_PLEX_NETWORKS = tuple(map(IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))


class CinemaError(Exception):
    """An actionable error suitable for the local UI."""

    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


def text(value: object, label: str, limit: int = 160, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise CinemaError(f"{label}: ungültiger Wert oder zu lang.")
    value = value.strip()
    if (not value and not empty) or any(ord(c) < 32 for c in value):
        raise CinemaError(f"{label}: bitte einen gültigen Wert eingeben.")
    return value


def record_id(value: object) -> str:
    value = text(value, "ID", 64)
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", value):
        raise CinemaError("Ungültige ID.")
    return value


def entity_id(value: object, domain: str = "media_player") -> str:
    value = text(value, "Gerät", 160)
    if not re.fullmatch(rf"{domain}\.[a-z0-9_]+", value):
        raise CinemaError(f"Bitte ein vorhandenes {domain}-Gerät wählen.")
    return value


def content_url(provider: str, value: object) -> str:
    """Accept only provider content links; never credentials, fragments or shell input."""
    value = text(value, "Inhaltslink", 2048)
    if provider == "plex":
        return plex_content_url(value)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as err:
        raise CinemaError("Der Inhaltslink ist ungültig.") from err
    if provider not in _CONTENT_HOSTS or parsed.scheme != "https" or parsed.hostname not in _CONTENT_HOSTS[provider]:
        raise CinemaError("Bitte einen HTTPS-Inhaltslink des gewählten Dienstes verwenden.")
    if parsed.username or parsed.password or port not in (None, 443) or parsed.fragment:
        raise CinemaError("Inhaltslinks mit Zugangsdaten, Sonderport oder Fragment sind nicht erlaubt.")
    path = parsed.path
    valid = False
    if provider == "netflix":
        valid = bool(re.fullmatch(r"/(?:[a-z]{2}(?:-[A-Z]{2})?/)?(?:title|watch)/\d+/?", path))
    elif provider == "disney":
        valid = bool(re.match(r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?(?:movies|series|video|browse|play)/.+", path))
    elif provider == "prime":
        valid = bool(re.fullmatch(
            r"/(?:-/[a-z]{2}/|dp/)?detail/amzn1\.dv\.gti\."
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/?", path, re.I,
        ))
        if not valid:
            raise CinemaError("Bitte einen Prime-Video-Titellink mit amzn1.dv.gti. im Link verwenden.")
    elif provider == "apple":
        valid = bool(re.match(r"/(?:[a-z]{2}/)?(?:movie|show|episode)/.+", path))
    elif parsed.hostname == "youtu.be":
        valid = bool(re.fullmatch(r"/[A-Za-z0-9_-]{11}/?", path))
    elif path == "/watch":
        valid = bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", parse_qs(parsed.query).get("v", [""])[0]))
    else:
        valid = bool(re.fullmatch(r"/(?:shorts|live)/[A-Za-z0-9_-]{11}/?", path))
    if not valid:
        raise CinemaError("Der Link muss direkt auf einen Film, eine Serie oder ein Video zeigen.")
    # Tracking parameters never leave HA. Keep only content/time parameters for YouTube.
    if provider == "youtube":
        from urllib.parse import urlencode

        query = parse_qs(parsed.query)
        clean = {key: query[key][0] for key in ("v", "t", "start", "list") if key in query}
        return urlunsplit(("https", parsed.hostname, path, urlencode(clean), ""))
    if provider == "netflix":
        return urlunsplit(("https", parsed.hostname, path, "source=30", ""))
    return urlunsplit(("https", parsed.hostname, path, "", ""))


def plex_content_url(value: str) -> str:
    if re.fullmatch(r"plex://movie/[1-9][0-9]{0,11}", value):
        return value
    try:
        parsed = urlsplit(value)
        host = IPv4Address(parsed.hostname or "")
        port = parsed.port
    except ValueError:
        raise CinemaError("Bitte einen lokalen Plex-Filmlink oder plex://movie/<ID> verwenden.") from None
    if (parsed.scheme not in ("http", "https") or not any(host in network for network in _PLEX_NETWORKS)
            or parsed.username is not None or parsed.password is not None or parsed.query
            or not port or "x-plex-token" in value.casefold()):
        raise CinemaError("Plex-Filmlinks müssen auf den lokalen Server zeigen und frei von Zugangsdaten sein.")
    path = parsed.path
    if parsed.fragment:
        if path not in ("/web", "/web/", "/web/index.html"):
            raise CinemaError("Ungültiger Plex-Web-Filmlink.")
        route = urlsplit(parsed.fragment.removeprefix("!"))
        if not re.fullmatch(r"/server/[A-Za-z0-9_-]+/details", route.path):
            raise CinemaError("Bitte die Detailseite des Plex-Films verlinken.")
        keys = parse_qs(route.query).get("key", [])
        path = keys[0] if len(keys) == 1 else ""
    if not re.fullmatch(r"/library/metadata/[1-9][0-9]{0,11}/?", path):
        raise CinemaError("Bitte einen Plex-Link mit einer konkreten Film-ID verwenden.")
    return urlunsplit((parsed.scheme, f"{host}:{port}", path.rstrip("/"), "", ""))


def favorite_record(data: dict, item_id: str) -> dict:
    provider = data.get("provider")
    if not provider:
        raw_url = text(data.get("url"), "Inhaltslink", 2048)
        try:
            hostname = urlsplit(raw_url).hostname
        except ValueError as err:
            raise CinemaError("Der Inhaltslink ist ungültig.") from err
        provider = next((key for key, hosts in _CONTENT_HOSTS.items() if hostname in hosts), None)
        if provider is None and (raw_url.startswith("plex://") or "/web/" in raw_url or
                                 "/library/metadata/" in raw_url):
            provider = "plex"
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise CinemaError("Bitte einen Inhaltslink von Netflix, YouTube, Disney+, Prime Video, Apple TV+ oder Plex eingeben.")
    url = content_url(provider, data.get("url"))
    name = text(data.get("name", ""), "Name", empty=True)
    if not name:
        parsed = urlsplit(url)
        item = parse_qs(parsed.query).get("v", [None])[0] or parsed.path.rstrip("/").rsplit("/", 1)[-1]
        label = PROVIDERS[provider]["name"]
        name = f"{label} · {item}"[:160] if item else label
    image = text(data.get("image", ""), "Bild", 512, empty=True)
    # Only HA-local artwork. Remote images could leak the current page or user address.
    if image and (not re.fullmatch(r"/local/[A-Za-z0-9_./%-]+\.(?:png|jpe?g|webp)", image, re.I)
                  or ".." in image):
        raise CinemaError("Optionales Bild: einen lokalen /local/…png, jpg oder webp Pfad verwenden.")
    playback = data.get("playback", "resume")
    if playback not in ("resume", "restart"):
        raise CinemaError("Bitte Fortsetzen oder Von Anfang an wählen.")
    result = {"id": record_id(item_id), "name": name, "provider": provider, "url": url,
              "image": image, "playback": playback}
    if provider == "prime":
        result["prime_auto_profile"] = data.get("prime_auto_profile") is True
        result["prime_auto_continue"] = (
            result["prime_auto_profile"] and data.get("prime_auto_continue") is True
        )
    return result


def room_record(data: dict, item_id: str) -> dict:
    targets = data.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 12:
        raise CinemaError("Ein Raum benötigt ein bis zwölf Wiedergabeziele.")
    tv = entity_id(data.get("tv_entity_id"))
    prime_profile_delay = data.get("prime_profile_delay", PRIME_PROFILE_DELAY)
    prime_continue_delay = data.get("prime_continue_delay", PRIME_CONTINUE_DELAY)
    if any(type(delay) is not int or not 1 <= delay <= PRIME_DELAY_MAX
           for delay in (prime_profile_delay, prime_continue_delay)):
        raise CinemaError(f"Bitte für Prime Video ganze Sekunden zwischen 1 und {PRIME_DELAY_MAX} angeben.")
    result = {"id": record_id(item_id), "name": text(data.get("name"), "Raumname"),
              "tv_entity_id": tv, "targets": [], "prime_profile_delay": prime_profile_delay,
              "prime_continue_delay": prime_continue_delay}
    seen: set[str] = set()
    devices: set[str] = set()
    for target in targets:
        if not isinstance(target, dict):
            raise CinemaError("Ungültiges Wiedergabeziel.")
        tid = record_id(target.get("id"))
        ent = entity_id(target.get("entity_id"))
        if tid in seen or ent in devices:
            raise CinemaError("Wiedergabeziele dürfen nicht doppelt vorkommen.")
        seen.add(tid)
        devices.add(ent)
        profile = target.get("profile", "generic")
        if profile not in ("generic", "samsungtv", "androidtv", "androidtv_remote"):
            raise CinemaError("Unbekanntes Geräteprofil.")
        remote = target.get("remote_entity_id", "")
        remote = entity_id(remote, "remote") if remote else ""
        source = text(target.get("hdmi_source", ""), "HDMI-Eingang", 160, empty=ent == tv)
        apps = target.get("apps", {})
        if not isinstance(apps, dict) or set(apps) - set(PROVIDERS):
            raise CinemaError("Ungültige App-Zuordnung.")
        normalized_apps = {}
        for provider, app in apps.items():
            if not isinstance(app, dict) or app.get("mode") not in ("off", "app", "link"):
                raise CinemaError("Ungültiges App-Startverfahren.")
            mode = app["mode"]
            if mode == "link" and profile not in ("androidtv", "androidtv_remote"):
                raise CinemaError("Dieses Geräteprofil bietet keinen Inhaltslink-Start.")
            app_id = text(app.get("app_id", ""), "App-ID", 160, empty=True)
            source_mode = None
            if profile in ("samsungtv", "generic"):
                source_mode = app.get("source_mode", "manual" if app_id else "auto")
                if source_mode not in ("auto", "manual"):
                    raise CinemaError("Ungültige App-Quellenzuordnung.")
                if source_mode == "auto":
                    app_id = ""
            if profile == "androidtv" and mode != "off":
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", app_id):
                    raise CinemaError("Für Fire TV / ADB wird eine gültige App-Paket-ID benötigt.")
            if profile == "androidtv_remote" and mode != "off" and not app_id:
                raise CinemaError("Bitte die App-ID eintragen.")
            if source_mode == "manual" and mode != "off" and not app_id:
                raise CinemaError("Bitte eine App-Quelle auswählen oder die automatische Zuordnung verwenden.")
            tested = app.get("tested", "untested")
            if tested not in ("untested", "app", "title", "playback"):
                raise CinemaError("Ungültiger Teststatus.")
            if mode != "link" and tested in ("title", "playback"):
                raise CinemaError("Titel-/Wiedergabenachweis benötigt einen Inhaltslink-Start.")
            normalized_apps[provider] = {"mode": mode, "app_id": app_id, "tested": tested}
            if source_mode is not None:
                normalized_apps[provider]["source_mode"] = source_mode
            if provider == "netflix":
                normalized_apps[provider]["profile_name"] = text(
                    app.get("profile_name", ""), "Netflix-Profil", 80, empty=True)
        result["targets"].append({"id": tid, "name": text(target.get("name"), "Zielname"),
                                  "entity_id": ent, "remote_entity_id": remote, "profile": profile,
                                  "hdmi_source": source, "apps": normalized_apps})
    return result
