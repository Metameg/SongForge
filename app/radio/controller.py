from flask import current_app
import time
import json


def select_next_track(r):
    cid = r.lpop(current_app.config["PLAYLIST_DYNAMIC_KEY"])
    if cid:
        return cid, "dynamic"

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

    cid, source = select_next_track(r)
    if not cid:
        return

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
                "conversion_path": path,
                "started_at": now,
                "duration": duration,
                "index_key": r.get("playlist:static:index"),
            }
        ),
    )
