from flask import Flask
from .config import DevelopmentConfig, ProductionConfig
import redis
from .extensions import socketio
import threading
import logging
import os
from logging.handlers import RotatingFileHandler
from flask_session import Session


def create_app():
    app = Flask(__name__, static_folder="static")

    config = ProductionConfig if os.getenv("FLASK_ENV") == "production" else DevelopmentConfig
    app.config.from_object(config)

    log_fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # stdout — visible in Railway console
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(log_fmt)
    stream_handler.setLevel(logging.INFO)
    app.logger.addHandler(stream_handler)
    logging.getLogger("werkzeug").addHandler(stream_handler)

    # File logging — for local dev
    log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "app.log"),
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
    )
    file_handler.setFormatter(log_fmt)
    file_handler.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)
    logging.getLogger("werkzeug").addHandler(file_handler)

    app.logger.setLevel(logging.INFO)

    # Redis client — prefers REDIS_URL (Railway/production), falls back to localhost
    redis_url = os.getenv(
        "REDIS_URL",
        f"redis://{app.config['REDIS_HOST']}:{app.config['REDIS_PORT']}/{app.config['REDIS_DB']}",
    )
    app.extensions["redis"] = redis.from_url(redis_url, decode_responses=True, max_connections=10)

    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY", "supersecret"),
        SESSION_TYPE="redis",
        SESSION_REDIS=redis.from_url(redis_url, max_connections=5),
        SESSION_PERMANENT=False,
    )
    Session(app)

    socketio.init_app(app)
    from .sockets import register_socket_handlers

    register_socket_handlers(socketio)

    from app.radio.watchdog import start_radio_watchdog

    threading.Thread(target=start_radio_watchdog, args=(app,), daemon=True).start()

    from .home import home_bp

    app.register_blueprint(home_bp)

    return app
