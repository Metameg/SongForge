from flask import session, current_app, request
from flask_socketio import join_room
from .home.utilities import emit_queue_position_to_client


def register_socket_handlers(socketio):
    @socketio.on("connect")
    def handle_connect():
        import uuid

        if "client_id" not in session:
            session["client_id"] = str(uuid.uuid4())

        client_id = session.get("client_id")
        join_room(client_id)

        # Check if this client's dynamic song is currently on air
        r = current_app.extensions["redis"]
        now_playing = r.hgetall("radio:now_playing")
        if now_playing.get("source") == "dynamic":
            job_key = now_playing.get("job_key")
            if job_key:
                job = r.hgetall(job_key)
                if job.get("client_id") == client_id:
                    socketio.emit(
                        "queue_position_update",
                        {"now_playing": True, "conversion_id": now_playing.get("conversion_id")},
                        to=request.sid,
                    )
                    return

        emit_queue_position_to_client(socketio, client_id, to=request.sid)
