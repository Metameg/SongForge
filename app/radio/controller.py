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
    print("changing song")
    r = current_app.extensions["redis"]

    cid, source = select_next_track(r)
    if not cid:
        return

    if isinstance(cid, bytes):
        cid = cid.decode()

    job_key = f"job:{cid}"
    path = r.hget(job_key, "conversion_path")
    duration = float(r.hget(job_key, "duration") or 0)

    now = int(time.time() * 1000)

    r.hset(
        "radio:now_playing",
        mapping={
            "conversion_id": cid,
            "conversion_path": path,
            "source": source,
            "duration": duration,
        },
    )
    r.set("radio:started_at", now)

    # Notify clients
    r.publish(
        "radio_events",
        json.dumps(
            {
                "event": "track_changed",
                "cid": cid,
                "conversion_path": path,
                "started_at": now,
                "duration": duration,
                "index_key": r.get("playlist:static:index"),
            }
        ),
    )

    # 🔁 Update remaining queue positions (per-user)
    emit_queue_positions(r)


def emit_queue_positions(r):
    print("emit_queue_positions")
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
        print("queue client id", client_id)
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
            },
            to=client_id,  # use client_id from job
        )
