"""Authenticated WebSocket API. Configuration requires an administrator."""

from __future__ import annotations

from uuid import uuid4

import probatio as vol

from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.components import websocket_api
from homeassistant.core import Context, HomeAssistant, callback

from .const import DOMAIN
from .manager import CinemaManager
from .models import CinemaError, record_id


def manager(hass: HomeAssistant) -> CinemaManager:
    value = hass.data[DOMAIN].get("manager")
    if value is None or value.closed:
        raise CinemaError("Die Kinosteuerung ist nicht geladen. Bitte die Integration einrichten.", "not_loaded")
    return value


def permitted(connection, entities: set[str], policy: str) -> None:
    if not all(connection.user.permissions.check_entity(entity, policy) for entity in entities):
        raise CinemaError("Du hast keine Berechtigung für alle Geräte dieses Ablaufs.", "unauthorized")


def snapshot(hass, connection) -> dict:
    result = manager(hass).snapshot(lambda entity: connection.user.permissions.check_entity(entity, POLICY_READ))
    result["can_configure"] = connection.user.is_admin
    for room in result["rooms"]:
        for target in room["targets"]:
            target["can_control"] = all(connection.user.permissions.check_entity(e, POLICY_CONTROL)
                                         for e in manager(hass).job_entities(room, target))
    return result


@websocket_api.websocket_command({vol.Required("type"): "cinema_control/get"})
@callback
def ws_get(hass, connection, msg) -> None:
    try:
        connection.send_result(msg["id"], snapshot(hass, connection))
    except CinemaError as err:
        connection.send_error(msg["id"], err.code, str(err))


@websocket_api.websocket_command({vol.Required("type"): "cinema_control/subscribe"})
@callback
def ws_subscribe(hass, connection, msg) -> None:
    try:
        current = manager(hass)
    except CinemaError as err:
        connection.send_error(msg["id"], err.code, str(err))
        return

    @callback
    def update() -> None:
        connection.send_event(msg["id"], snapshot(hass, connection))

    current.listeners.add(update)
    connection.subscriptions[msg["id"]] = lambda: current.listeners.discard(update)
    connection.send_result(msg["id"])
    update()


@websocket_api.websocket_command({
    vol.Required("type"): "cinema_control/command",
    vol.Required("operation"): vol.In(("save_room", "save_favorite", "delete_room", "delete_favorite", "start", "app", "prepare_netflix", "confirm_profile", "cancel", "control", "list_apps", "uninstall_app", "pair_youtube", "diagnose", "assistant", "standard_diagnose")),
    vol.Optional("room_id"): str,
    vol.Optional("target_id"): str,
    vol.Optional("favorite_id"): str,
    vol.Optional("job_id"): str,
    vol.Optional("provider"): str,
    vol.Optional("app"): str,
    vol.Optional("observation"): str,
    vol.Optional("action"): str,
    vol.Optional("package"): str,
    vol.Optional("device_ref"): str,
    vol.Optional("value"): str,
    vol.Optional("item_id"): str,
    vol.Optional("revision"): vol.All(int, vol.Range(min=0)),
    vol.Optional("data"): dict,
})
@websocket_api.async_response
async def ws_command(hass, connection, msg) -> None:
    try:
        current = manager(hass)
        operation = msg["operation"]
        result = None
        if operation in ("diagnose", "assistant", "standard_diagnose") and not connection.user.is_admin:
            raise CinemaError("Die Beta-Diagnose ist Administratoren vorbehalten.", "unauthorized")
        if operation == "standard_diagnose":
            result = await current.standard_diagnose(msg.get("data", {}))
        elif operation.startswith(("save_", "delete_")):
            if not connection.user.is_admin:
                raise CinemaError("Nur Administratoren dürfen die Einrichtung ändern.", "unauthorized")
            kind = "rooms" if operation.endswith("room") else "favorites"
            item_id = record_id(msg.get("item_id") or uuid4().hex)
            if "revision" not in msg or (operation.startswith("save_") and "data" not in msg):
                raise CinemaError("Für das Speichern fehlen Angaben.")
            if operation.startswith("delete_") and "item_id" not in msg:
                raise CinemaError("Bitte den zu löschenden Eintrag angeben.")
            await current.async_save(kind, msg.get("data") if operation.startswith("save_") else None,
                                     item_id, msg["revision"])
            result = {"id": item_id, "revision": current.config["revision"]}
        else:
            if not msg.get("room_id") or not msg.get("target_id"):
                raise CinemaError("Bitte Raum und Wiedergabeziel auswählen.")
            room, target = current.resolve(msg["room_id"], msg["target_id"])
            permitted(connection, current.job_entities(room, target), POLICY_CONTROL)
            context = Context(user_id=connection.user.id)
            if operation == "assistant":
                permitted(connection, current.job_entities(room, target), POLICY_READ)
                result = await current.assistant(room, target, msg.get("app", ""), msg.get("action", ""), msg.get("data", {}), context)
            elif operation == "diagnose":
                permitted(connection, current.job_entities(room, target), POLICY_READ)
                result = await current.diagnose(room, target, msg.get("app", ""), msg.get("observation", "not_tested"))
            elif operation in ("start", "app", "prepare_netflix"):
                if operation == "start" and not msg.get("favorite_id"):
                    raise CinemaError("Bitte einen Favoriten wählen.")
                result = current.start(room["id"], target["id"], msg.get("favorite_id") if operation == "start" else None,
                                       "netflix" if operation == "prepare_netflix" else msg.get("provider"), context,
                                       prepare_netflix=operation == "prepare_netflix")
            elif operation == "pair_youtube":
                if not connection.user.is_admin:
                    raise CinemaError("Nur Administratoren dürfen YouTube mit dem Fernseher verbinden.", "unauthorized")
                await current.pair_youtube(room, target)
            elif operation in ("list_apps", "uninstall_app"):
                if not connection.user.is_admin:
                    raise CinemaError("Die App-Verwaltung ist Administratoren vorbehalten.", "unauthorized")
                if operation == "uninstall_app" and (not msg.get("package") or not msg.get("device_ref")):
                    raise CinemaError("Bitte Gerät und App zur Deinstallation bestätigen.")
                result = await current.manage_apps(
                    room, target, context,
                    package=msg["package"] if operation == "uninstall_app" else None,
                    device_ref=msg.get("device_ref"),
                )
            elif operation == "confirm_profile":
                result = current.confirm_profile(room["id"], target["id"], record_id(msg.get("job_id")), context)
            elif operation == "cancel":
                job = current.jobs.get(room["id"])
                if job:
                    _, active_target = current.resolve(room["id"], job["target_id"])
                    permitted(connection, current.job_entities(room, active_target), POLICY_CONTROL)
                await current.cancel(room["id"])
            else:
                await current.control(room["id"], target["id"], msg.get("action", ""), msg.get("value"), context)
        connection.send_result(msg["id"], result)
    except CinemaError as err:
        connection.send_error(msg["id"], err.code, str(err))
    except TimeoutError:
        connection.send_error(msg["id"], "timeout", "Das Gerät hat die Aktion nicht rechtzeitig beantwortet.")
    except Exception:
        connection.send_error(msg["id"], "failed", "Die Aktion konnte nicht abgeschlossen werden. Bitte erneut versuchen.")


@callback
def async_register_api(hass: HomeAssistant) -> None:
    for command in (ws_get, ws_subscribe, ws_command):
        websocket_api.async_register_command(hass, command)
