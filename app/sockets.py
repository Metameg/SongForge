from flask import session
from flask_socketio import join_room


def register_socket_handlers(socketio):
    @socketio.on("connect")
    def handle_connect():
        import uuid

        if "client_id" not in session:
            print("creating client id")
            session["client_id"] = str(uuid.uuid4())

        join_room(f"session:{session['client_id']}")

        print(f"Socket joined room: session:{session['client_id']}")
