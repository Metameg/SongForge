from flask import current_app
import time
import json
from app.extensions import socketio


def select_next_track(r):
    raw = r.lpop(current_app.config["PLAYLIST_DYNAMIC_KEY"])
    if raw:
        item = json.loads(raw)
        return item["conversion_id"], "dynamic"

    # 2️⃣ Static fallback (circular)
    static_key = current_app.config["PLAYLIST_STATIC_KEY"]
    index_key = "playlist:static:index"

    static_len = r.llen(static_key)
    if static_len == 0:
        return None, None

    index = int(r.get(index_key) or 0)
    cid = r.lindex(static_key, index % static_len)
    r.set(index_key, index + 1)

    return cid, "static"


def advance_radio():
    r = current_app.extensions["redis"]

    # If the outgoing track was a dynamic song, clean up the owner's active_job
    # server-side so they're not locked out if they closed their browser during playback
    prev = r.hgetall("radio:now_playing")
    if prev.get("source") == "dynamic":
        prev_job = r.hgetall(prev.get("job_key", "")) if prev.get("job_key") else {}
        if prev_job.get("status") != "played":
            r.hset(prev["job_key"], "status", "played")
            client_id = prev_job.get("client_id")
            if client_id:
                r.delete(f"client:{client_id}:active_job")
                socketio.emit("queue_position_update", {"in_queue": False}, to=client_id)

    cid, source = select_next_track(r)
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

    # 🔁 Update remaining queue positions (per-user)
    emit_queue_positions(r)
    current_app.logger.debug(f"advance_radio: source={source}, cid={cid}")
    if source == "dynamic":
        client_id = r.hget(job_key, "client_id")
        if client_id:
            if isinstance(client_id, bytes):
                client_id = client_id.decode()
            socketio.emit(
                "queue_position_update",
                {
                    "conversion_id": cid,
                    "now_playing": True,
                },
                to=client_id,
            )


def emit_queue_positions(r):
    queue_key = current_app.config["PLAYLIST_DYNAMIC_KEY"]

    raw_items = r.lrange(queue_key, 0, -1)
    queue_length = len(raw_items)

    for idx, raw in enumerate(raw_items):
        try:
            # Decode bytes if needed
            item = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            continue

        # Use client_id stored in the job, not Flask session
        client_id = item.get("client_id")  # 🔑 previously session_id
        conversion_id = item.get("conversion_id")

        if not client_id or not conversion_id:
            continue

        # Emit only to the specific client who owns this job
        socketio.emit(
            "queue_position_update",
            {
                "conversion_id": conversion_id,
                "queue_position": idx + 1,
                "queue_length": queue_length,
                "in_queue": True,
            },
            to=client_id,  # use client_id from job
        )
