from flask import session
from flask_socketio import join_room
from .home.utilities import emit_queue_position_to_client


def register_socket_handlers(socketio):
    @socketio.on("connect")
    def handle_connect():
        import uuid

        if "client_id" not in session:
            print("creating client id")
            session["client_id"] = str(uuid.uuid4())

        join_room(session.get("client_id"))

        print(f"Socket joined room: session:{session.get('client_id')}")
        emit_queue_position_to_client(socketio, session.get("client_id"))
