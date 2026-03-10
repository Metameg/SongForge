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

    # Redis client — prefers REDIS_URL (Railway/production), falls back to localhost
    redis_url = os.getenv(
        "REDIS_URL",
        f"redis://{app.config['REDIS_HOST']}:{app.config['REDIS_PORT']}/{app.config['REDIS_DB']}",
    )
    app.extensions["redis"] = redis.from_url(redis_url, decode_responses=True)

    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY", "supersecret"),
        SESSION_TYPE="redis",
        SESSION_REDIS=redis.from_url(redis_url),
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
