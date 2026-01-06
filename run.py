import eventlet

eventlet.monkey_patch()

from app import create_app

from app.extensions import socketio  # ⚡ import the same instance
import json


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
socketio.start_background_task(redis_listener, redis_client)

if __name__ == "__main__":
    socketio.run(app, debug=True)
