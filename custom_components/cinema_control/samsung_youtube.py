"""Read the YouTube start ID from the Samsung TV's paired EDEN channel."""

import asyncio
from base64 import b64encode
from collections.abc import Mapping
import errno
from ipaddress import IPv4Address, IPv4Network
import json
import re
import ssl
from urllib.parse import parse_qs, urlencode, urlsplit

from aiohttp import (
    ClientConnectorError, ClientError, ClientSession, ClientSSLError, ClientTimeout,
    TraceConfig, WSServerHandshakeError, WSMsgType,
)

from .models import CinemaError, content_url


_LAN_NETWORKS = tuple(map(IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
SAMSUNG_DEFAULT_CLIENT_NAME = "HomeAssistant"
YOUTUBE_PAIR_CLIENT_NAME = "Kinosteuerung"
_EDEN_EVENTS = ("ed.edenApp.get", "ed.edenApp.update")
_PAIR_TOTAL_TIMEOUT = 40
_PAIR_PHASES = frozenset({"connect", "tls", "websocket_handshake", "authorization", "response"})
_PAIR_MESSAGES = {
    "connection_refused": "Die Verbindung zu diesem Port wurde abgelehnt.",
    "connection_timeout": "Der Verbindungsaufbau zum Fernseher hat zu lange gedauert.",
    "connection_failed": "Die Verbindung zum Fernseher konnte nicht aufgebaut werden.",
    "tls_failed": "Die verschlüsselte Verbindung zum Fernseher konnte nicht aufgebaut werden.",
    "handshake_failed": "Die WebSocket-Verbindung wurde nicht geöffnet.",
    "redirect_rejected": "Die Kopplungsadresse wurde unerwartet umgeleitet. Die Umleitung wurde nicht verfolgt.",
    "channel_closed": "Die Verbindung wurde vor der Kopplungsbestätigung geschlossen.",
    "tv_denied": "Der Fernseher hat die Kopplung abgelehnt. Bitte die Geräteliste am TV prüfen.",
    "tv_authorization_timeout": "Der Fernseher meldet, dass die Freigabe abgelaufen ist. Bitte erneut zulassen.",
    "tv_protocol_error": "Der Fernseher hat bei der Kopplung einen Protokollfehler gemeldet.",
    "invalid_response": "Der Fernseher hat keine gültige Kopplungsantwort gesendet.",
    "token_missing": "Die Verbindung wurde geöffnet, aber der Fernseher hat keinen Kopplungsschlüssel geliefert.",
    "authorization_timeout": "Die Freigabe am Fernseher wurde nicht rechtzeitig bestätigt.",
    "response_limit": "Der Fernseher hat innerhalb der begrenzten Abfrage keine Kopplung bestätigt.",
}
_YOUTUBE_ACTION = re.compile(
    r"(?:https://www\.youtube\.com/tv#play\?v=)?"
    r"[A-Za-z0-9_-]{11}&launch=launcher&deeplinkId="
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})"
)


def _lan_address(host: str | IPv4Address) -> IPv4Address:
    try:
        address = IPv4Address(host)
    except ValueError:
        raise CinemaError("Für Samsung-YouTube wird eine lokale TV-Adresse benötigt.") from None
    if not any(address in network for network in _LAN_NETWORKS):
        raise CinemaError("Für Samsung-YouTube wird eine lokale TV-Adresse benötigt.")
    return address


def _remote_url(address: IPv4Address, port: int, client_name: str, token: str | None = None) -> str:
    query = {"name": b64encode(client_name.encode()).decode("ascii")}
    if token is not None:
        query["token"] = token
    scheme = "wss" if port == 8002 else "ws"
    return f"{scheme}://{address}:{port}/api/v2/channels/samsung.remote.control?{urlencode(query)}"


async def _reject_redirect(*_args) -> None:
    raise _SamsungRedirectRejected("Samsung launcher requests may not redirect.")


class _SamsungRedirectRejected(ClientError):
    """Distinguish a rejected redirect from a connector failure without a raw URL."""


class SamsungPairingError(CinemaError):
    """A fixed message and a bounded technical stage, never a raw network exception."""

    def __init__(self, reason: str, port: int = 8002, phase: str = "authorization", *,
                 http_status: int | None = None, close_code: int | None = None, attempts=None) -> None:
        reason = reason if isinstance(reason, str) and reason in _PAIR_MESSAGES else "connection_failed"
        phase = phase if isinstance(phase, str) and phase in _PAIR_PHASES else "response"
        port = port if type(port) is int and port in (8001, 8002) else 8002
        super().__init__(_PAIR_MESSAGES[reason], "samsung_youtube_pairing")
        self.reason, self.port, self.phase = reason, port, phase
        self._details = {"port": port, "phase": phase, "reason": reason}
        if type(http_status) is int and 100 <= http_status <= 599:
            self._details["http_status"] = http_status
        if type(close_code) is int and 0 <= close_code <= 65535:
            self._details["close_code"] = close_code
        self._attempts = []
        if isinstance(attempts, (list, tuple)):
            for item in attempts[:2]:
                if not isinstance(item, dict):
                    continue
                safe = SamsungPairingError(item.get("reason"), item.get("port"), item.get("phase"),
                                           http_status=item.get("http_status"), close_code=item.get("close_code"))
                self._attempts.append(safe.safe_details())

    def safe_details(self) -> dict:
        result = dict(self._details)
        if len(self._attempts) > 1:
            result["attempts"] = [dict(item) for item in self._attempts]
        return result


def samsung_youtube_trace_config() -> TraceConfig:
    """Reject HTTP redirects before a request can leave the configured TV."""
    trace = TraceConfig()
    trace.on_request_redirect.append(_reject_redirect)
    return trace


def samsung_youtube_pairing(settings: object) -> tuple[int, str | None] | None:
    """Read the credentials for an existing Samsung pairing."""
    if not isinstance(settings, Mapping):
        return None
    port, token = settings.get("port"), settings.get("token")
    if type(port) is not int or port not in (8001, 8002):
        return None
    if port == 8001:
        return port, None
    if isinstance(token, str) and token.strip() and len(token) <= 256:
        return port, token
    return None


def _pair_fallback_allowed(error: SamsungPairingError) -> bool:
    details = error.safe_details()
    return ((error.phase == "connect" and error.reason in {"connection_refused", "connection_timeout", "connection_failed"})
            or (error.phase == "tls" and error.reason == "tls_failed")
            or (error.phase == "websocket_handshake" and error.reason == "handshake_failed"
                and details.get("http_status") in (400, 404, 426)))


async def _pair_attempt(session: ClientSession, address: IPv4Address, port: int, deadline: float,
                        state: dict) -> tuple[int, str | None]:
    state.update(port=port, phase="connect")
    try:
        # Some TVs wait for user approval before completing the HTTP upgrade.
        # Keep their full authorization window; only socket connection has a shorter cap.
        async with asyncio.timeout(max(0.001, deadline - asyncio.get_running_loop().time())):
            ws = await session.ws_connect(_remote_url(address, port, YOUTUBE_PAIR_CLIENT_NAME),
                                          ssl=False, max_msg_size=2 * 1024 * 1024)
    except _SamsungRedirectRejected:
        raise SamsungPairingError("redirect_rejected", port, "websocket_handshake") from None
    except WSServerHandshakeError as error:
        raise SamsungPairingError("handshake_failed", port, "websocket_handshake", http_status=error.status) from None
    except (ClientSSLError, ssl.SSLError):
        raise SamsungPairingError("tls_failed", port, "tls") from None
    except TimeoutError:
        raise SamsungPairingError("connection_timeout", port, "connect") from None
    except (ClientConnectorError, OSError) as error:
        underlying = error.os_error if isinstance(error, ClientConnectorError) else error
        refused = isinstance(underlying, ConnectionRefusedError) or getattr(underlying, "errno", None) in (errno.ECONNREFUSED, 10061)
        raise SamsungPairingError("connection_refused" if refused else "connection_failed", port, "connect") from None
    except ClientError:
        raise SamsungPairingError("connection_failed", port, "connect") from None
    except (ValueError, RecursionError):
        raise SamsungPairingError("handshake_failed", port, "websocket_handshake") from None

    state["phase"] = "authorization"
    try:
        for _ in range(32):
            try:
                message = await ws.receive()
            except TimeoutError:
                raise SamsungPairingError("authorization_timeout", port, "authorization") from None
            except (ClientError, OSError):
                raise SamsungPairingError("channel_closed", port, "authorization", close_code=ws.close_code) from None
            if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING, WSMsgType.ERROR):
                code = message.data if message.type == WSMsgType.CLOSE and type(message.data) is int else ws.close_code
                raise SamsungPairingError("channel_closed", port, "authorization", close_code=code)
            if message.type != WSMsgType.TEXT:
                raise SamsungPairingError("invalid_response", port, "response")
            try:
                payload = message.json()
            except (ValueError, TypeError, RecursionError):
                raise SamsungPairingError("invalid_response", port, "response") from None
            if not isinstance(payload, dict):
                raise SamsungPairingError("invalid_response", port, "response")
            event = payload.get("event")
            if event == "ms.channel.unauthorized":
                raise SamsungPairingError("tv_denied", port, "authorization")
            if event == "ms.channel.timeOut":
                raise SamsungPairingError("tv_authorization_timeout", port, "authorization")
            if event == "ms.error":
                raise SamsungPairingError("tv_protocol_error", port, "response")
            if event == "ms.channel.connect":
                data = payload.get("data")
                token = data.get("token") if isinstance(data, dict) else None
                pairing = samsung_youtube_pairing({"port": port, "token": token})
                if pairing is None:
                    raise SamsungPairingError("token_missing", port, "response")
                return pairing
        raise SamsungPairingError("response_limit", port, "authorization")
    finally:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining > 0:
            try:
                async with asyncio.timeout(min(1, remaining)):
                    await ws.close()
            except Exception:
                pass


async def async_pair_samsung_youtube(host: str | IPv4Address) -> tuple[int, str | None]:
    """Prefer encrypted pairing; make at most one fallback before a channel opens."""
    address = _lan_address(host)
    state, attempts = {"port": 8002, "phase": "connect"}, []
    deadline = asyncio.get_running_loop().time() + _PAIR_TOTAL_TIMEOUT
    try:
        async with asyncio.timeout(_PAIR_TOTAL_TIMEOUT):
            async with ClientSession(timeout=ClientTimeout(sock_connect=5), trust_env=False,
                                     trace_configs=[samsung_youtube_trace_config()]) as session:
                for port in (8002, 8001):
                    try:
                        return await _pair_attempt(session, address, port, deadline, state)
                    except SamsungPairingError as error:
                        details = error.safe_details()
                        attempts.append(details)
                        if port == 8002 and _pair_fallback_allowed(error) and asyncio.get_running_loop().time() < deadline:
                            continue
                        raise SamsungPairingError(error.reason, error.port, error.phase,
                            http_status=details.get("http_status"), close_code=details.get("close_code"), attempts=attempts) from None
    except TimeoutError:
        reason = "connection_timeout" if state["phase"] == "connect" else "authorization_timeout"
        raise SamsungPairingError(reason, state["port"], state["phase"], attempts=attempts) from None
    except SamsungPairingError:
        raise
    except (ClientError, OSError, ValueError, RecursionError):
        reason = "connection_failed" if state["phase"] == "connect" else "channel_closed"
        raise SamsungPairingError(reason, state["port"], state["phase"], attempts=attempts) from None
    raise SamsungPairingError("connection_failed", state["port"], state["phase"], attempts=attempts)


def youtube_video_id(url: str) -> str:
    """Read the video ID from a validated watch, share, Shorts or Live URL."""
    parsed = urlsplit(content_url("youtube", url))
    if parsed.path == "/watch":
        return parse_qs(parsed.query)["v"][0]
    return parsed.path.rstrip("/").rsplit("/", 1)[-1]


def _preview_ids(data: object) -> set[str]:
    """Find exact action values in EDEN data, including nested JSON strings."""
    ids: set[str] = set()
    remaining = 16_384

    def visit(value: object, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 20:
            raise CinemaError("Die Samsung-Vorschau ist zu umfangreich.", "samsung_youtube_preview")
        if isinstance(value, str):
            match = _YOUTUBE_ACTION.fullmatch(value)
            if match:
                ids.add(match[1])
            elif value.lstrip().startswith(("{", "[", '"')):
                try:
                    decoded = json.loads(value)
                except (ValueError, RecursionError):
                    return
                visit(decoded, depth + 1)
        elif isinstance(value, dict):
            for child in value.values():
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)

    visit(data, 0)
    return ids


async def async_samsung_youtube_start_id(
    session: ClientSession, host: str | IPv4Address, port: int, token: str | None,
    *, client_name: str = SAMSUNG_DEFAULT_CLIENT_NAME,
) -> str:
    """Read the unique ID from the current YouTube preview without retaining it."""
    address = _lan_address(host)
    pairing = samsung_youtube_pairing({"port": port, "token": token})
    if pairing is None:
        raise CinemaError("Für diesen TV wird eine bestehende Samsung-WebSocket-Kopplung benötigt.",
                          "samsung_youtube_pairing")

    port, token = pairing
    url = _remote_url(address, port, client_name, token)
    connected = False
    try:
        async with asyncio.timeout(8):
            # Samsung supplies its own certificate for the paired LAN connection.
            async with session.ws_connect(url, ssl=False, max_msg_size=2 * 1024 * 1024) as ws:
                for _ in range(32):
                    message = await ws.receive()
                    if message.type != WSMsgType.TEXT:
                        break
                    payload = message.json()
                    if not isinstance(payload, dict):
                        continue
                    event = payload.get("event")
                    if event in ("ms.channel.unauthorized", "ms.channel.timeOut"):
                        raise CinemaError("Der Samsung hat die vorhandene Kopplung abgelehnt.", "samsung_youtube_pairing")
                    if not connected and event == "ms.channel.connect":
                        await ws.send_json({
                            "method": "ms.channel.emit",
                            "params": {"event": "ed.edenApp.get", "to": "host"},
                        })
                        connected = True
                    elif connected and event in _EDEN_EVENTS:
                        ids = _preview_ids(payload.get("data"))
                        if len(ids) == 1:
                            return ids.pop()
                        if ids:
                            break
    except TimeoutError:
        pass
    except (ClientError, OSError, ValueError, RecursionError):
        # An aiohttp exception can include the token in its request URL.
        raise CinemaError("Die Samsung-Vorschau konnte nicht gelesen werden.", "samsung_youtube_eden_unavailable") from None

    if not connected:
        raise CinemaError("Die gekoppelte Samsung-Verbindung ist nicht erreichbar.", "samsung_youtube_eden_unavailable")
    raise CinemaError(
        "Der Samsung meldet keine eindeutige YouTube-Vorschau. Öffne Samsung Home und versuche es erneut.",
        "samsung_youtube_preview",
    )
