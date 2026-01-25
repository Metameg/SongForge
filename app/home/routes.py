from . import home_bp
from flask import session, render_template


@home_bp.route("/", methods=["GET"])
def index():
    if "client_id" not in session:
        import uuid

        print("client_id wasnt in session so routes is making another one")
        session["client_id"] = str(uuid.uuid4())
    print(session["client_id"])
    return render_template("home.html")
