import os
import re
import subprocess
import threading
from app import create_app

from app.extensions import socketio  # ⚡ import the same instance
import json
from mutagen._file import File as MutagenFile


def init_static_playlist(app, r):
    list_exists = r.llen(app.config["PLAYLIST_STATIC_KEY"]) > 0
    audio_dir = os.path.join(app.static_folder, "audios")

    for f in os.listdir(audio_dir):
        if f.lower().endswith((".mp3", ".ogg", ".wav")):
            cid = f"static-{f}"
            job_key = f"job:{cid}"

            audio = MutagenFile(os.path.join(audio_dir, f))
            if audio is None or not hasattr(audio, "info") or audio.info is None:
                app.logger.warning(f"Skipping unreadable audio file: {f}")
                continue
            duration = audio.info.length  # seconds

            # Always write job hashes so they survive Redis restarts or partial failures
            r.hset(job_key, "conversion_path", f"/static/audios/{f}")
            r.hset(job_key, "status", "static")
            r.hset(job_key, "duration", duration)

            if not list_exists:
                r.rpush(app.config["PLAYLIST_STATIC_KEY"], cid)


def redis_listener(r):
    pubsub = r.pubsub()
    pubsub.subscribe("radio_events")

    for message in pubsub.listen():
        if message["type"] == "message":
            data = json.loads(message["data"])
            # print("Emitting new_audio:", data)  # debug
            socketio.emit("radio_events", data)  # ⚡ broadcast=True


def start_cloudflared(app):
    """Start cloudflared tunnel and update app config with the public URL."""
    proc = subprocess.Popen(
        [r"C:\Program Files (x86)\cloudflared\cloudflared.exe", "tunnel", "--url", "http://localhost:5000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    url_pattern = re.compile(rb"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")

    def read_stream(stream):
        for line in stream:
            match = url_pattern.search(line)
            if match:
                public_url = match.group(0).decode()
                with app.app_context():
                    app.config["PUBLIC_BASE_URL"] = public_url
                    app.config["WEBHOOK_URL"] = public_url + "/webhook"
                print(f"Cloudflare tunnel active: {public_url}", flush=True)

    threading.Thread(target=read_stream, args=(proc.stdout,), daemon=True).start()
    threading.Thread(target=read_stream, args=(proc.stderr,), daemon=True).start()
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
