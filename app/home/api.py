from flask import Flask, request, jsonify, current_app
import requests
import os
from pprint import pprint
import time
import threading
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
    try:
        if not lyrics:
            lyrics = client.create_lyrics(prompt)

        conversion_ids, cost, error = client.create_music(prompt, lyrics)

        if error:
            return jsonify({"status": "failed", "message": error}), 500

        return jsonify(
            {
                "status": "success",
                "conversion_ids": conversion_ids,
                "cost": cost,
                "lyrics": lyrics,
            }
        )

    except Exception as e:
        current_app.logger.exception(e)
        return jsonify({"status": "failed", "message": "Unexpected server error"}), 500


@home_bp.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    # r = current_app.extensions["redis"]
    print(data)

    # Only proceed if conversion_path exists
    conversion_path = data.get("conversion_path")
    if not conversion_path:
        return {"ok": False, "error": "No conversion_path in webhook"}, 400

    # Lookup job_id via conversion_id or another unique identifier
    conversion_id = data.get("conversion_id")
    job_id = f"job:{conversion_id}"  # or some mapping
    job_key = f"job:{job_id}"
    print()
    print(job_id)
    print(job_key)
    print()
    # Atomic check: only set if not already set
    # if not r.hexists(job_key, "conversion_path"):
    #     r.hset(job_key, "conversion_path", conversion_path)
    #     r.hset(job_key, "status", "complete")  # mark job as complete

    return {"ok": True}
