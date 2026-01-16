from flask import current_app
import time
from .controller import advance_radio


def radio_watchdog():
    r = current_app.extensions["redis"]

    if not r.setnx("radio:watchdog_lock", "1"):
        return
    r.expire("radio:watchdog_lock", 5)

    try:
        started_at = r.get("radio:started_at")
        duration = r.hget("radio:now_playing", "duration")

        if not started_at or not duration:
            advance_radio()
            return

        elapsed = (time.time() * 1000 - int(started_at)) / 1000
        if elapsed >= float(duration):
            advance_radio()
    finally:
        r.delete("radio:watchdog_lock")


def start_radio_watchdog(app):
    with app.app_context():
        while True:
            radio_watchdog()
            time.sleep(1)
