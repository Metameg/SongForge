from flask import Flask


def create_app():
    app = Flask(__name__)

    from .home import home_bp
    from .bulktool import bulktool_bp

    app.register_blueprint(home_bp)
    app.register_blueprint(bulktool_bp)

    return app
