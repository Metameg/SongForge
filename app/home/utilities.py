from flask import current_app
import json


def get_queue_info_by_client(client_id):
    """
    Find a client's queue position and conversion_id in the dynamic playlist.

    Returns:
        dict with keys: conversion_id, queue_position, queue_length, found
        If not found, found=False
    """
    r = current_app.extensions["redis"]
    raw_items = r.lrange(current_app.config["PLAYLIST_DYNAMIC_KEY"], 0, -1)
    queue_length = len(raw_items)


    for idx, raw in enumerate(raw_items):
        try:
            item = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            continue

        if item.get("client_id") == client_id:
            return {
                "found": True,
                "conversion_id": item.get("conversion_id"),
                "queue_position": idx + 1,  # 1-indexed position
                "queue_length": queue_length,
            }

    return {
        "found": False,
        "queue_length": queue_length,
    }


def emit_queue_position_to_client(socketio, client_id, to=None):
    """
    Emit queue position update to a specific client.
    Always checks if the client's song is currently on-air first.

    `to` — override the socket target. Pass request.sid on connect so only the
    newly connecting socket is updated, not every tab the user has open.
    Defaults to the client_id room (broadcasts to all tabs for that user).
    """
    target = to or client_id
    r = current_app.extensions["redis"]

    # If this client's song is currently playing, tell them that — not queue position
    now_playing = r.hgetall("radio:now_playing")
    if now_playing.get("source") == "dynamic":
        job_key = now_playing.get("job_key")
        if job_key:
            job = r.hgetall(job_key)
            if job.get("client_id") == client_id:
                socketio.emit(
                    "queue_position_update",
                    {"now_playing": True, "conversion_id": now_playing.get("conversion_id")},
                    to=target,
                )
                return

    queue_info = get_queue_info_by_client(client_id)

    if queue_info["found"]:
        socketio.emit(
            "queue_position_update",
            {
                "conversion_id": queue_info["conversion_id"],
                "queue_position": queue_info["queue_position"],
                "queue_length": queue_info["queue_length"],
                "in_queue": True,
            },
            to=target,
        )
    else:
        has_active_job = bool(r.exists(f"client:{client_id}:active_job"))
        socketio.emit(
            "queue_position_update",
            {
                "in_queue": False,
                "has_active_job": has_active_job,
                "message": "You currently have no songs in queue.",
            },
            to=target,
        )


def emit_job_status(socketio, client_id, status, message=None):
    socketio.emit(
        "job_status_update",
        {
            "status": status,
            "message": message,
        },
        room=client_id,
    )


def remove_from_processing(r, key, job_key):
    items = r.lrange(key, 0, -1)

    for item in items:
        data = json.loads(item)
        if data.get("job_key") == job_key:
            r.lrem(key, 1, item)
            return
