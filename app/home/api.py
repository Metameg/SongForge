from flask import Flask, request, jsonify, current_app
import requests
import os
import json
import threading
from pprint import pprint
import time
from app.extensions import socketio
from threading import Semaphore
import random
from .services import MusicAPIClient
from dataset import BibleDataset
from io import BytesIO
from zipfile import ZipFile
from pathlib import Path
from . import home_bp
import redis

app = Flask(__name__)
r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)


@home_bp.route("/api/audio-list", methods=["GET"])
def audio_list():
    audio_dir = os.path.join(current_app.static_folder, "audios")
    files = [
        f for f in os.listdir(audio_dir) if f.lower().endswith((".mp3", ".ogg", ".wav"))
    ]

    # Shuffle the playlist
    random.shuffle(files)
    return jsonify(files)


def simulate_webhook(webhook_url, payload):
    requests.post(webhook_url, json=payload, timeout=10)


@home_bp.route("/api/create-song", methods=["POST"])
def create_song():
    data = request.get_json(silent=True)

    if not data:
        return jsonify({"status": "failed", "message": "Invalid JSON payload"}), 400

    lyrics = data.get("lyrics", "").strip()
    prompt = data.get("prompt", "").strip()

    if not prompt:
        return jsonify({"status": "failed", "message": "Prompt is required"}), 400

    client = MusicAPIClient(
        open_ai_key=current_app.config["OPEN_AI_KEY"],
        musicgpt_key=current_app.config["MUSICGPT_KEY"],
        webhook_url=f"{current_app.config['PUBLIC_BASE_URL']}/webhook",
    )

    # conversion_ids, cost, error = client.create_music(prompt, lyrics)
    payload = {
        "conversion_id": "test-conversion-123",
        "conversion_path": "/audio/song_1.mp3",
    }

    threading.Thread(
        target=simulate_webhook,
        args=(current_app.config["WEBHOOK_URL"], payload),
        daemon=True,
    ).start()

    return jsonify(
        {
            "status": "success",
            "lyrics": lyrics,
        }
    )

    # try:
    #     if not lyrics:
    #         lyrics = client.create_lyrics(prompt)
    #
    #     conversion_ids, cost, error = client.create_music(prompt, lyrics)
    #
    #     if error:
    #         return jsonify({"status": "failed", "message": error}), 500
    #
    #     return jsonify(
    #         {
    #             "status": "success",
    #             "conversion_ids": conversion_ids,
    #             "cost": cost,
    #             "lyrics": lyrics,
    #         }
    #     )
    #
    # except Exception as e:
    #     current_app.logger.exception(e)
    #     return jsonify({"status": "failed", "message": "Unexpected server error"}), 500
    #


@home_bp.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    r = current_app.extensions["redis"]

    # Only proceed if conversion_path exists
    # conversion_path = data.get("conversion_path")
    # conversion_path = data.get("conversion_id")
    conversion_path = "https://lalals.s3.amazonaws.com/conversions/standard/4fea5fd7-a903-4930-a711-16ad8bf2c436/4fea5fd7-a903-4930-a711-16ad8bf2c436.mp3"
    conversion_id = "4fea5fd7-a903-4930-a711-16ad8bf2c436"

    if not conversion_path:
        return {"ok": False, "error": "No conversion_path in webhook"}, 400

    job_key = f"job:{conversion_id}"

    r.publish(
        "audio_events",
        json.dumps(
            {
                "conversion_id": conversion_id,
                "conversion_path": conversion_path,
            }
        ),
    )
    # Atomic check: only set if not already set
    if not r.hexists(job_key, "conversion_path"):
        r.hset(job_key, "conversion_path", conversion_path)
        r.hset(job_key, "status", "complete")  # mark job as complete

    return {"ok": True}


@home_bp.route("/api/next-audio", methods=["GET"])
def latest_conversion():
    r = current_app.extensions["redis"]

    # Get all job keys
    keys = r.keys("job:*")
    if not keys:
        return jsonify({"conversion_path": None})

    # Find the newest completed job
    for key in reversed(sorted(keys)):
        data = r.hgetall(key)
        if data.get("status") == "complete" and data.get("conversion_path"):
            r.hset(key, "status", "queued")

            return jsonify(
                {
                    "conversion_path": data["conversion_path"],
                    "conversion_id": key.split(":")[1],
                }
            )

    return jsonify({"conversion_path": None})


@home_bp.route("/api/mark-played", methods=["POST"])
def mark_played():
    r = current_app.extensions["redis"]
    data = request.get_json()
    path = data.get("conversion_path")
    keys = r.keys("job:*")

    for key in keys:
        if r.hget(key, "conversion_path") == path:
            r.hset(key, "status", "played")
            break

    return {"ok": True}
