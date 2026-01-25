from flask import Flask
from .config import DevelopmentConfig
import redis
from .extensions import socketio
import threading
from flask_session import Session


def create_app():
    app = Flask(__name__, static_folder="static")
    app.config.from_object(DevelopmentConfig)

    # Redis client
    app.extensions["redis"] = redis.Redis(
        host=app.config["REDIS_HOST"],
        port=app.config["REDIS_PORT"],
        db=app.config["REDIS_DB"],
        decode_responses=True,
    )

    app.config.update(
        SECRET_KEY="supersecret",
        SESSION_TYPE="redis",
        SESSION_REDIS=redis.from_url(
            f"redis://{app.config['REDIS_HOST']}:{app.config['REDIS_PORT']}"
        ),
        SESSION_PERMANENT=False,
    )
    Session(app)

    socketio.init_app(app)
    from .sockets import register_socket_handlers

    register_socket_handlers(socketio)

    from app.radio.watchdog import start_radio_watchdog

    threading.Thread(target=start_radio_watchdog, args=(app,), daemon=True).start()

    from .home import home_bp
    from .bulktool import bulktool_bp

    app.register_blueprint(home_bp)
    app.register_blueprint(bulktool_bp)

    return app
