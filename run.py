import os
from app import create_app

from app.extensions import socketio  # ⚡ import the same instance
import json


def init_static_playlist(app, r):
    if r.llen(app.config["PLAYLIST_STATIC_KEY"]) > 0:
        return  # already initialized

    audio_dir = os.path.join(app.static_folder, "audios")

    for f in os.listdir(audio_dir):
        if f.lower().endswith((".mp3", ".ogg", ".wav")):
            cid = f"static-{f}"
            job_key = f"job:{cid}"

            r.rpush(app.config["PLAYLIST_STATIC_KEY"], cid)
            r.hset(
                job_key,
                mapping={"conversion_path": f"/static/audios/{f}", "status": "static"},
            )


def redis_listener(r):
    pubsub = r.pubsub()
    pubsub.subscribe("audio_events")
    for message in pubsub.listen():
        if message["type"] == "message":
            data = json.loads(message["data"])
            print("Emitting new_audio:", data)  # debug
            socketio.emit("new_audio", data)  # ⚡ broadcast=True


app = create_app()
redis_client = app.extensions["redis"]
init_static_playlist(app, redis_client)
socketio.start_background_task(redis_listener, redis_client)

if __name__ == "__main__":
    socketio.run(app, debug=True)
