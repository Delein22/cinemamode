"""One bounded, read-only Samsung connection for app and preview discovery.

Raw device values stay in this transient result. Callers publish only fixed
technical details and their existing redacted preview summaries.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network
import json

from aiohttp import (
    ClientConnectorCertificateError, ClientConnectorError, ClientConnectorSSLError, ClientError, ClientSession,
    ClientTimeout, WSMsgType, WSServerHandshakeError,
)

from .device_access import TVAccess
from .samsung_youtube import (
    _SamsungRedirectRejected, _remote_url, samsung_youtube_pairing, samsung_youtube_trace_config,
)

_LAN = tuple(map(IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
_EDEN = frozenset({"ed.edenApp.get", "ed.edenApp.update"})


@dataclass(slots=True)
class SamsungCapture:
    apps: list | None = field(default=None, repr=False)
    preview: object | None = field(default=None, repr=False)
    authorization: str = "not_checked"
    details: dict = field(default_factory=dict)
    apps_details: dict = field(default_factory=dict)
    preview_details: dict = field(default_factory=dict)
    queries_sent: bool = False


def _details(port: int | None, phase: str, reason: str, **codes) -> dict:
    values = {"phase": phase, "reason": reason}
    if type(port) is int and port in (8001, 8002):
        values["port"] = port
    for key, lower, upper in (("http_status", 100, 599), ("close_code", 0, 65535)):
        value = codes.get(key)
        if type(value) is int and lower <= value <= upper:
            values[key] = value
    return values


def _decode(value: object) -> object:
    for _ in range(3):
        if not isinstance(value, str) or not value.lstrip().startswith(("{", "[", '"')):
            return value
        if len(value) > 2 * 1024 * 1024:
            raise ValueError
        value = json.loads(value)
    if isinstance(value, str) and value.lstrip().startswith(("{", "[", '"')):
        raise ValueError
    return value


async def async_capture_samsung(access: TVAccess | None) -> SamsungCapture:
    """Use one authorized socket and retain successful independent partial results."""
    capture = SamsungCapture()
    pairing = None
    if access is not None and isinstance(access.pairing, tuple) and len(access.pairing) == 2:
        pairing = samsung_youtube_pairing({"port": access.pairing[0], "token": access.pairing[1]})
    if access is None or pairing is None:
        capture.details = _details(None, "authorization", "existing_pairing_required")
        capture.apps_details = dict(capture.details)
        capture.preview_details = dict(capture.details)
        return capture
    port, token = pairing
    if (not isinstance(access.host, IPv4Address) or not any(access.host in network for network in _LAN)
            or not isinstance(access.client_name, str) or len(access.client_name) > 160):
        capture.details = _details(port, "connect", "invalid_device_access")
        capture.apps_details = dict(capture.details)
        capture.preview_details = dict(capture.details)
        return capture
    phase = "connect"
    terminal = _details(port, phase, "connection_failed")
    try:
        async with asyncio.timeout(12):
            async with ClientSession(timeout=ClientTimeout(total=12, sock_connect=3), trust_env=False,
                                     trace_configs=[samsung_youtube_trace_config()]) as session:
                async with session.ws_connect(_remote_url(access.host, port, access.client_name, token),
                                              ssl=False, max_msg_size=2 * 1024 * 1024) as ws:
                    phase = "authorization"
                    for _ in range(64):
                        message = await ws.receive()
                        if message.type != WSMsgType.TEXT:
                            reason = "channel_error" if message.type == WSMsgType.ERROR else "channel_closed"
                            close_code = message.data if type(getattr(message, "data", None)) is int else getattr(ws, "close_code", None)
                            terminal = _details(port, phase, reason, close_code=close_code)
                            break
                        event = _decode(message.json())
                        if not isinstance(event, dict):
                            raise ValueError
                        kind = event.get("event")
                        if kind in ("ms.channel.unauthorized", "ms.channel.timeOut"):
                            capture.authorization = "failed"
                            terminal = _details(port, "authorization", "tv_denied" if kind == "ms.channel.unauthorized" else "authorization_timeout")
                            break
                        if kind == "ms.error":
                            terminal = _details(port, phase, "server_error")
                            break
                        if kind == "ms.channel.connect" and capture.authorization != "verified":
                            capture.authorization = "verified"
                            for query in ("ed.installedApp.get", "ed.edenApp.get"):
                                await ws.send_json({"method": "ms.channel.emit", "params": {"event": query, "to": "host"}})
                                capture.queries_sent = True
                            phase = "app_list"
                        elif capture.authorization == "verified" and kind == "ed.installedApp.get":
                            try:
                                data = _decode(event.get("data"))
                                apps = _decode(data.get("data")) if isinstance(data, dict) else data
                                if not isinstance(apps, list) or len(apps) > 512:
                                    raise ValueError
                                capture.apps = apps
                                capture.apps_details = _details(port, "app_list", "complete")
                            except (ValueError, TypeError, RecursionError):
                                capture.apps_details = _details(port, "app_list", "invalid_response")
                        elif capture.authorization == "verified" and isinstance(kind, str) and kind in _EDEN:
                            try:
                                preview = _decode(event.get("data"))
                                if not isinstance(preview, (dict, list, str)):
                                    raise ValueError
                                capture.preview = preview
                                capture.preview_details = _details(port, "preview", "complete")
                            except (ValueError, TypeError, RecursionError):
                                capture.preview_details = _details(port, "preview", "invalid_response")
                        if capture.apps is not None and capture.preview is not None:
                            terminal = _details(port, "complete", "complete")
                            break
                        if capture.authorization == "verified":
                            phase = "app_list" if capture.apps is None else "preview"
                    else:
                        terminal = _details(port, phase, "event_limit")
    except _SamsungRedirectRejected:
        terminal = _details(port, "websocket_handshake", "redirect_rejected")
    except WSServerHandshakeError as error:
        terminal = _details(port, "websocket_handshake", "handshake_failed", http_status=error.status)
    except (ClientConnectorCertificateError, ClientConnectorSSLError):
        terminal = _details(port, "tls", "tls_failed")
    except ClientConnectorError as error:
        terminal = _details(port, "connect", "connection_refused" if isinstance(error.os_error, ConnectionRefusedError) else "connection_failed")
    except TimeoutError:
        reason = "connection_timeout" if phase == "connect" else "authorization_timeout" if phase == "authorization" else "response_timeout"
        terminal = _details(port, phase, reason)
    except (ValueError, TypeError, AttributeError, RecursionError):
        terminal = _details(port, phase, "invalid_response")
    except (ClientError, OSError):
        terminal = _details(port, phase, "connection_failed")
    if capture.authorization == "not_checked":
        capture.authorization = "failed"
    capture.details = terminal
    if capture.apps is None and not capture.apps_details:
        capture.apps_details = {**terminal, "phase": "app_list" if capture.authorization == "verified" else terminal["phase"]}
    if capture.preview is None and not capture.preview_details:
        capture.preview_details = {**terminal, "phase": "preview" if capture.authorization == "verified" else terminal["phase"]}
    return capture
