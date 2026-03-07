from flask import Flask
from .config import DevelopmentConfig
import redis
from .extensions import socketio
import threading
import logging
import os
from logging.handlers import RotatingFileHandler
from flask_session import Session


def create_app():
    app = Flask(__name__, static_folder="static")
    app.config.from_object(DevelopmentConfig)

    # File logging — always written regardless of how the server was started
    log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    handler = RotatingFileHandler(
        os.path.join(log_dir, "app.log"),
        maxBytes=2 * 1024 * 1024,  # 2 MB per file
        backupCount=3,
    )
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    handler.setLevel(logging.INFO)
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)

    # Also write Werkzeug request logs to the same file
    logging.getLogger("werkzeug").addHandler(handler)

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
