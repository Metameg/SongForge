from flask import Blueprint

bulktool_bp = Blueprint(
    "bulktool",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/bulktool/static",
)

from . import routes, api
