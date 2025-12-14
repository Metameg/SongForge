from flask import Flask, request, jsonify, render_template
import requests
import time
import threading

app = Flask(__name__)

# simple in-memory state (fine for dev)
STATE = {
    "status": "idle"  # idle | processing | complete
}


def process_and_callback(webhook_url):
    # simulate long-running work
    time.sleep(15)

    # send webhook callback
    requests.post(
        webhook_url,
        json={"status": "complete", "message": "Processing finished"},
        timeout=5,
    )


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    STATE["status"] = "processing"

    # 🔁 Simulate API call that triggers webhook later
    # Replace this with your real API call
    requests.post(
        "http://127.0.0.1:5000/api",
        json={
            "webhook_url": "https://schedules-transactions-began-merely.trycloudflare.com/webhook"
        },
        timeout=5,
    )

    return jsonify({"ok": True})


@app.route("/status", methods=["GET"])
def status():
    return jsonify(STATE)


@app.route("/api", methods=["POST"])
def api():
    data = request.json
    webhook_url = data.get("webhook_url")

    # start background task
    threading.Thread(
        target=process_and_callback, args=(webhook_url,), daemon=True
    ).start()

    # return immediately (important)
    return jsonify({"accepted": True}), 202


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("Webhook received:", data)

    STATE["status"] = "complete"
    return {"status": "ok"}, 200


if __name__ == "__main__":
    app.run(debug=True)
