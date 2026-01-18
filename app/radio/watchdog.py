from flask import current_app
import time
from .controller import advance_radio


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


def start_radio_watchdog(app):
    with app.app_context():
        while True:
            radio_watchdog()
            time.sleep(1)
