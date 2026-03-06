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
    queue_key = current_app.config["PLAYLIST_DYNAMIC_KEY"]
    raw_items = r.lrange(queue_key, 0, -1)
    queue_length = len(raw_items)

    print("queue length", queue_length)
    print("client id", client_id)
    print()

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


def emit_queue_position_to_client(socketio, client_id):
    """
    Emit queue position update to a specific client.
    Always checks if the client's song is currently on-air first.
    """
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
                    to=client_id,
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
            to=client_id,
        )
    else:
        socketio.emit(
            "queue_position_update",
            {
                "in_queue": False,
                "message": "You currently have no songs in queue.",
            },
            to=client_id,
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

    remaining = []
    for item in items:
        data = json.loads(item)
        print(data)
        print("JOBKEY:", job_key, "data", data.get("job_key"))
        if data.get("job_key") != job_key:
            remaining.append(item)

    r.delete(key)

    if remaining:
        r.rpush(key, *remaining)
