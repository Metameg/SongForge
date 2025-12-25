from . import bulktool_bp
from flask import render_template


@bulktool_bp.route("/bulktool", methods=["GET"])
def index():
    return render_template("bulktool.html")
