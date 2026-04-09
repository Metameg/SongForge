from flask import current_app
import time
import json
from .controller import advance_radio
from app.home.utilities import emit_job_status, remove_from_processing

JOB_TIMEOUT_SECONDS = 600  # 10 minutes


def radio_watchdog():
    r = current_app.extensions["redis"]
    if not r.set("radio:watchdog_lock", "1", nx=True, ex=5):
        return

    started_at = r.get("radio:started_at")
    duration = r.hget("radio:now_playing", "duration")
    if duration is None or started_at is None:
        advance_radio()
        return

    started_at = int(started_at)
    duration = float(duration)

    elapsed = (time.time() * 1000 - int(started_at)) / 1000

    if elapsed >= duration:
        advance_radio()


def check_processing_timeouts(app):
    from app.extensions import socketio
    r = app.extensions["redis"]
    processing_key = app.config["PLAYLIST_PROCESSING_KEY"]
    now_ms = time.time() * 1000

    for raw in r.lrange(processing_key, 0, -1):
        try:
            item = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            continue

        created_at = int(item.get("created_at") or 0)
        if created_at and (now_ms - created_at) / 1000 >= JOB_TIMEOUT_SECONDS:
            job_key = item.get("job_key")
            client_id = item.get("client_id")

            remove_from_processing(r, processing_key, job_key)
            r.decr("musicgpt:active_generations")
            if client_id:
                r.delete(f"client:{client_id}:active_job")
                emit_job_status(
                    socketio,
                    client_id,
                    status="failed",
                    message="Song generation timed out. Please try again.",
                )
            if job_key:
                r.delete(job_key)


def start_radio_watchdog(app):
    tick = 0
    with app.app_context():
        while True:
            radio_watchdog()
            if tick % 30 == 0:
                check_processing_timeouts(app)
            tick += 1
            time.sleep(1)
