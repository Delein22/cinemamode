"""Smart View start wire format and correlated replies, without HA or networking."""

import json

APP_ID = "BofenoHome.Zuhause"
CHANNEL_ID = "com.bofeno.zuhause.cinema.v1"


def build_start_request(request: dict, rpc_id: str) -> dict:
    """Wrap our already validated title request in the SDK's start-argument map."""
    payload = json.dumps({"values": json.dumps(request, separators=(",", ":"))}, separators=(",", ":"))
    return {"id": rpc_id, "method": "ms.application.start", "params": {
        "id": APP_ID, "data": {"id": payload}}}


def classify_start_reply(reply: object, rpc_id: str) -> str | None:
    """Return fixed outcomes only; unrelated channel events are not RPC replies."""
    if not isinstance(reply, dict):
        return "unknown"
    if reply.get("id") != rpc_id or reply.get("event") is not None:
        return None
    if isinstance(reply.get("error"), dict) or reply.get("result") is False:
        return "rejected"
    if reply.get("error") is None and reply.get("result") is True:
        return "accepted"
    return "unknown"
