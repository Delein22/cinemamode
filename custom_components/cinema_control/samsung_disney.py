"""Opt-in title launch for the tested Samsung Tizen firmware."""

import asyncio
from datetime import datetime
from hashlib import sha256
from ipaddress import IPv4Address, IPv4Network
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from aiohttp import ClientError, ClientSession, WSMsgType

from .models import CinemaError, content_url, entity_id
from .samsung_youtube import (
    SAMSUNG_DEFAULT_CLIENT_NAME,
    async_samsung_youtube_start_id,
    samsung_youtube_pairing,
    samsung_youtube_trace_config,
    youtube_video_id,
)

APP_ID = "3201901017640"
YOUTUBE_APP_ID = "111299001912"
LAUNCHER = "org.volt.firstscreen"
PROFILE_FILE = "cinema_disney_samsung.json"
PLAY_DELAY = 20
LAN_NETWORKS = tuple(map(IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))


def disney_title_id(url: str) -> str:
    path = urlsplit(content_url("disney", url)).path
    match = re.fullmatch(
        r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?(?:browse/entity-|play/)"
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?",
        path, re.I,
    )
    if not match:
        raise CinemaError("Für diesen Samsung einen Disney+-Link mit entity-ID oder /play/-ID verwenden.")
    return "entity-" + match[1].lower()


class SamsungDisneyLauncher:
    def __init__(self, settings: dict) -> None:
        self.player = entity_id(settings["entity_id"])
        self.host = IPv4Address(settings["host"])
        if not any(self.host in network for network in LAN_NETWORKS):
            raise ValueError("Expected a private LAN address")
        self.key = settings["launcher_key"]
        if not re.fullmatch(r"[0-9a-f]{64}", self.key) or settings["firmware"] != "T-KTMDEUC-1410.1":
            raise ValueError("Unsupported Samsung launcher profile")
        self.timezone = ZoneInfo(settings["timezone"])
        self.autoplay = settings.get("autoplay") is True
        self.youtube_pairing = samsung_youtube_pairing(settings.get("youtube_pairing"))
        self.main_player = entity_id(settings["main_player"]) if "main_player" in settings else None
        self.main_remote = (
            entity_id(settings["main_remote"], "remote") if self.main_player else None
        )

    @classmethod
    def load(cls, path: str):
        try:
            return cls(json.loads(Path(path).read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None

    async def launch(self, session: ClientSession, url: str) -> None:
        tile = {
            "page": "browse", "id": disney_title_id(url), "distributionPartner": "samsung",
            "placement": "dedicated_row", "placementDetail": "eden_row",
        }
        await self._send(session, "Disney+", "deeplink", {
            "appId": APP_ID, "checkUpdate": "", "contentId": "", "amazonType": "",
            "payload": json.dumps({"values": json.dumps(tile, separators=(",", ":"))}),
        })

    async def launch_prime(self, session: ClientSession, url: str) -> None:
        gti = urlsplit(content_url("prime", url)).path.rstrip("/").rsplit("/", 1)[-1].lower()
        await self._send(session, "Prime Video", "native", {
            "appId": "org.tizen.ignition",
            "bundle": {
                "--root": "app", "--app-js": "amazon", "callerid": LAUNCHER,
                "action_play_url": f" --deepLinkParameter=detail?gti={gti}&refMarker=dvm_liv_sam_de_bd_x_rec_pear_0",
                "AppUseType": "UI_DEFAULT", "__K_RELOCATE_BELOW": LAUNCHER,
            },
        })

    async def launch_youtube(
        self, url: str, pairing: tuple[int, str | None], *, client_name: str = SAMSUNG_DEFAULT_CLIENT_NAME,
    ) -> None:
        video_id = youtube_video_id(url)
        port, token = pairing
        async with ClientSession(trace_configs=[samsung_youtube_trace_config()]) as session:
            start_id = await async_samsung_youtube_start_id(session, self.host, port, token, client_name=client_name)
            await self._send(session, "YouTube", "deeplink", {
                "appId": YOUTUBE_APP_ID, "checkUpdate": "", "contentId": "", "amazonType": "",
                "payload": (f"https://www.youtube.com/tv#play?v={video_id}"
                            f"&launch=launcher&deeplinkId={start_id}"),
            })

    async def _send(self, session: ClientSession, provider: str, action: str, params: dict) -> None:
        date = datetime.now(self.timezone).strftime("%Y%m%d")
        digest = sha256(f"{LAUNCHER}|{self.key}|{date}".encode()).hexdigest()
        try:
            async with asyncio.timeout(8):
                async with session.ws_connect(
                    f"ws://{self.host}:9012", protocols=(f"{LAUNCHER}|{digest}|seal-async",),
                    max_msg_size=16384,
                ) as ws:
                    await ws.send_json({"module": "LaunchHelper", "func": action, "params": params})
                    message = await ws.receive()
                    if message.type == WSMsgType.TEXT:
                        result = message.json().get("rsp", {})
                        reported_app = result.get("appId")
                        # The tested YouTube reply can acknowledge the request by result alone.
                        matched_app = reported_app == params["appId"] or (
                            reported_app is None and params["appId"] == YOUTUBE_APP_ID
                        )
                        if (type(result.get("result")) is int and result["result"] == 0
                                and (action == "native" or matched_app)):
                            return
        except (ClientError, TimeoutError, ValueError, AttributeError) as err:
            raise CinemaError(f"Samsung-Launcher für {provider} nicht erreichbar.") from err
        raise CinemaError(f"Der Samsung hat den Titelaufruf für {provider} nicht bestätigt.")
