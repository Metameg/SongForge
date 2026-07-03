from flask import current_app
import time
import json
from app.extensions import socketio


def select_next_track(r):
    raw = r.lpop(current_app.config["PLAYLIST_DYNAMIC_KEY"])
    if raw:
        item = json.loads(raw)
        return item["conversion_id"], "dynamic", item.get("client_id")

    # 2️⃣ Static fallback (circular)
    static_key = current_app.config["PLAYLIST_STATIC_KEY"]
    index_key = "playlist:static:index"

    static_len = r.llen(static_key)
    if static_len == 0:
        return None, None, None

    index = int(r.get(index_key) or 0)
    cid = r.lindex(static_key, index % static_len)
    r.set(index_key, index + 1)

    return cid, "static", None


def advance_radio():
    r = current_app.extensions["redis"]

    # Capture the outgoing dynamic track's owner before popping the next track
    prev = r.hgetall("radio:now_playing")
    prev_client_id = None
    if prev.get("source") == "dynamic":
        prev_job = r.hgetall(prev.get("job_key", "")) if prev.get("job_key") else {}
        if prev_job.get("status") != "played":
            r.hset(prev["job_key"], "status", "played")
            prev_client_id = prev_job.get("client_id")
            if prev_client_id:
                r.delete(f"client:{prev_client_id}:active_job")

    cid, source, owner_client_id = select_next_track(r)
    if not cid:
        return

    if isinstance(cid, bytes):
        cid = cid.decode()

    job_key = f"job:{cid}"
    path = r.hget(job_key, "conversion_path")
    duration = float(r.hget(job_key, "duration") or 0)
    album_cover_path = r.hget(job_key, "album_cover") or ""
    title = r.hget(job_key, "title")
    now = int(time.time() * 1000)

    if not path:
        # Job hash expired or is broken — the queue entry is already consumed,
        # so unlock its owner instead of leaving their active_job stuck
        if source == "dynamic" and owner_client_id:
            r.delete(f"client:{owner_client_id}:active_job")
            current_app.logger.warning(
                f"advance_radio: dropped dead queue entry cid={cid}, unlocked client {owner_client_id}"
            )
            socketio.emit(
                "queue_position_update",
                {"in_queue": False, "has_active_job": False,
                 "queue_length": r.llen(current_app.config["PLAYLIST_DYNAMIC_KEY"])},
                to=owner_client_id,
            )
        return

    r.hset("radio:now_playing", "job_key", job_key)
    r.hset("radio:now_playing", "conversion_id", cid)
    r.hset("radio:now_playing", "conversion_path", path)
    r.hset("radio:now_playing", "source", source)
    r.hset("radio:now_playing", "duration", duration)
    r.set("radio:started_at", now)

    # Notify clients
    r.publish(
        "radio_events",
        json.dumps(
            {
                "event": "track_changed",
                "cid": cid,
                "conversion_path": path,
                "title": title,
                "album_cover": album_cover_path,
                "started_at": now,
                "duration": duration,
                "source": source,
                "index_key": r.get("playlist:static:index"),
            }
        ),
    )

    # Update all queued/processing clients with their new positions and queue_length
    queue_length = emit_queue_positions(r)
    current_app.logger.debug(f"advance_radio: source={source}, cid={cid}")

    # Notify the outgoing dynamic track's owner — song is done, send updated count
    if prev_client_id:
        socketio.emit(
            "queue_position_update",
            {"in_queue": False, "has_active_job": False, "queue_length": queue_length},
            to=prev_client_id,
        )

    # Notify the incoming dynamic track's owner — song is now playing
    if source == "dynamic":
        incoming_client_id = r.hget(job_key, "client_id")
        if incoming_client_id:
            if isinstance(incoming_client_id, bytes):
                incoming_client_id = incoming_client_id.decode()
            socketio.emit(
                "queue_position_update",
                {"conversion_id": cid, "now_playing": True, "queue_length": queue_length},
                to=incoming_client_id,
            )

    # Broadcast to all connected clients (spectators, non-queued) so their counter stays in sync
    r.publish("queue_events", json.dumps({"queue_length": queue_length}))


def emit_queue_positions(r):
    dynamic_items = r.lrange(current_app.config["PLAYLIST_DYNAMIC_KEY"], 0, -1)
    processing_items = r.lrange(current_app.config["PLAYLIST_PROCESSING_KEY"], 0, -1)
    queue_length = len(dynamic_items)

    for idx, raw in enumerate(dynamic_items):
        try:
            item = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            continue

        client_id = item.get("client_id")
        conversion_id = item.get("conversion_id")

        if not client_id or not conversion_id:
            continue

        socketio.emit(
            "queue_position_update",
            {
                "conversion_id": conversion_id,
                "queue_position": idx + 1,
                "queue_length": queue_length,
                "in_queue": True,
            },
            to=client_id,
        )

    # Also notify clients whose songs are still being generated
    for raw in processing_items:
        try:
            item = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            continue

        client_id = item.get("client_id")
        if not client_id:
            continue

        socketio.emit(
            "queue_position_update",
            {
                "in_queue": False,
                "has_active_job": True,
                "queue_length": queue_length,
            },
            to=client_id,
        )

    return queue_length
