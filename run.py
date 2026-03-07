import eventlet
eventlet.monkey_patch()

import os
import re
import subprocess
import threading
from dotenv import load_dotenv
load_dotenv(override=True)
from app import create_app

from app.extensions import socketio  # ⚡ import the same instance
import json
from mutagen._file import File as MutagenFile


def init_static_playlist(app, r):
    audio_dir = os.path.join(app.static_folder, "audios")
    files = [f for f in os.listdir(audio_dir) if f.lower().endswith((".mp3", ".ogg", ".wav"))]

    # Rebuild the list if it doesn't match the files on disk
    if r.llen(app.config["PLAYLIST_STATIC_KEY"]) != len(files):
        r.delete(app.config["PLAYLIST_STATIC_KEY"])
        r.delete("playlist:static:index")

        for f in sorted(files):
            cid = f"static-{f}"
            r.rpush(app.config["PLAYLIST_STATIC_KEY"], cid)

    # Always write job hashes so they survive Redis restarts or partial init failures
    for f in files:
        cid = f"static-{f}"
        job_key = f"job:{cid}"

        audio = MutagenFile(os.path.join(audio_dir, f))
        if audio is None or not hasattr(audio, "info") or audio.info is None:
            app.logger.warning(f"Skipping unreadable audio file: {f}")
            continue

        r.hset(job_key, "conversion_path", f"/static/audios/{f}")
        r.hset(job_key, "status", "static")
        r.hset(job_key, "duration", audio.info.length)


def redis_listener(r):
    pubsub = r.pubsub()
    pubsub.subscribe("radio_events", "queue_events")

    for message in pubsub.listen():
        if message["type"] != "message":
            continue
        channel = message["channel"]
        if isinstance(channel, bytes):
            channel = channel.decode()
        data = json.loads(message["data"])
        if channel == "radio_events":
            socketio.emit("radio_events", data)
        elif channel == "queue_events":
            socketio.emit("queue_changed", data)


def _persist_cloudflared_url(app, public_url):
    """Update app config and Redis with the live tunnel URL."""
    webhook_url = public_url + "/webhook"
    with app.app_context():
        app.config["PUBLIC_BASE_URL"] = public_url
        app.config["WEBHOOK_URL"] = webhook_url
        app.extensions["redis"].set("config:webhook_url", webhook_url)
    print(f"Cloudflare tunnel active: {public_url}", flush=True)
    print(f"Webhook URL:              {webhook_url}", flush=True)


def start_cloudflared(app):
    """Start cloudflared tunnel, update app config, and persist URL in Redis."""
    import time as _time
    import urllib.request as _req

    proc = subprocess.Popen(
        [r"C:\Program Files (x86)\cloudflared\cloudflared.exe", "tunnel", "--url", "http://localhost:5000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    url_pattern = re.compile(rb"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")
    found = threading.Event()

    def read_stream(stream):
        for line in stream:
            match = url_pattern.search(line)
            if match and not found.is_set():
                found.set()
                _persist_cloudflared_url(app, match.group(0).decode())

    def poll_metrics():
        """Fallback: poll cloudflared metrics API for the tunnel URL."""
        for _ in range(30):
            _time.sleep(2)
            if found.is_set():
                return
            try:
                metrics_url = "http://127.0.0.1:8080/metrics"
                with _req.urlopen(metrics_url, timeout=2) as resp:
                    content = resp.read().decode()
                match = re.search(r'userHostname="(https://[a-zA-Z0-9\-]+\.trycloudflare\.com)"', content)
                if match and not found.is_set():
                    found.set()
                    _persist_cloudflared_url(app, match.group(1))
                    return
            except Exception:
                pass

    threading.Thread(target=read_stream, args=(proc.stdout,), daemon=True).start()
    threading.Thread(target=read_stream, args=(proc.stderr,), daemon=True).start()
    threading.Thread(target=poll_metrics, daemon=True).start()
    threading.Thread(target=proc.wait, daemon=True).start()


app = create_app()
redis_client = app.extensions["redis"]
init_static_playlist(app, redis_client)
socketio.start_background_task(redis_listener, redis_client)

if __name__ == "__main__":
    # Only start cloudflared in the parent process, not the reloader child
    if not os.environ.get("WERKZEUG_RUN_MAIN"):
        threading.Thread(target=start_cloudflared, args=(app,), daemon=True).start()
    socketio.run(app, debug=True)
