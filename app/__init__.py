from flask import Flask
from .config import DevelopmentConfig
import redis
from .extensions import socketio


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

    socketio.init_app(app)

    from .home import home_bp
    from .bulktool import bulktool_bp

    app.register_blueprint(home_bp)
    app.register_blueprint(bulktool_bp)

    return app
