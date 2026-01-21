from flask import Flask, request, jsonify, current_app
import requests
import os
import json
import threading
from pprint import pprint
import time
from app.extensions import socketio
import random
from .services import MusicAPIClient
from io import BytesIO
from zipfile import ZipFile
from pathlib import Path
from . import home_bp
import redis

app = Flask(__name__)
# r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)


@home_bp.route("/api/radio/now-playing", methods=["GET"])
def now_playing():
    r = current_app.extensions["redis"]

    song = r.hgetall("radio:now_playing")
    started_at = r.get("radio:started_at")
    print(f"SONG: {song}, STARTEDAT: {started_at}")
    if not song or not started_at:
        return jsonify({"playing": False})

    return jsonify(
        {
            "playing": True,
            "conversion_path": song["conversion_path"],
            "started_at": int(started_at),
            "duration": float(song.get("duration", 0)),
        }
    )


# @home_bp.route("/api/audio-list", methods=["GET"])
# def audio_list():
#     audio_dir = os.path.join(current_app.static_folder, "audios")
#     files = [
#         f for f in os.listdir(audio_dir) if f.lower().endswith((".mp3", ".ogg", ".wav"))
#     ]
#
#     # Shuffle the playlist
#     random.shuffle(files)
#     return jsonify(files)


def simulate_webhook(webhook_url, payload):
    requests.post(webhook_url, json=payload, timeout=10)


@home_bp.route("/api/create-song", methods=["POST"])
def create_song():
    r = current_app.extensions["redis"]
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

    return jsonify({"success": True})

    # try:
    #     if not lyrics:
    #         lyrics = client.create_lyrics(prompt)
    #
    #     conversion_ids, cost, error = client.create_music(prompt, lyrics)
    #
    #     if error:
    #         return jsonify({"status": "failed", "message": error}), 500
    #
    #     # Assume one song per request (simplify radio logic)
    #     conversion_id = conversion_ids[0]
    #     job_key = f"job:{conversion_id}"
    #
    #     # Create job record (NO QUEUE YET)
    #     r.hset(
    #         job_key,
    #         mapping={
    #             "status": "processing",
    #             "prompt": prompt,
    #             "lyrics": lyrics,
    #             "created_at": int(time.time() * 1000),
    #         },
    #     )
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


@home_bp.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    r = current_app.extensions["redis"]

    # Only proceed if conversion_path exists
    # conversion_path = data.get("conversion_path")
    # conversion_id = data.get("conversion_id")
    # duration = data.get("duration")
    conversion_path = "https://lalals.s3.amazonaws.com/conversions/standard/4fea5fd7-a903-4930-a711-16ad8bf2c436/4fea5fd7-a903-4930-a711-16ad8bf2c436.mp3"
    conversion_id = "4fea5fd7-a903-4930-a711-16ad8bf2c436"
    duration = 120

    print("hooked")
    if not conversion_id or not conversion_path or not duration:
        return {"ok": False, "error": "Missing required fields"}, 400

    job_key = f"job:{conversion_id}"

    # Atomic: only queue once
    if r.exists(job_key):
        print("alread   queued")
        return {"ok": True, "already_queued": True}

    # Persist job metadata
    r.hset(
        job_key,
        mapping={
            "conversion_path": conversion_path,
            "duration": float(duration),
            "status": "queued",
            "source": "dynamic",
        },
    )

    # Push ONLY the ID into the dynamic playlist
    queue_position = r.rpush(
        current_app.config["PLAYLIST_DYNAMIC_KEY"],
        conversion_id,
    )

    # Notify listeners
    r.publish(
        "radio_events",
        json.dumps(
            {
                "event": "queue_updated",
                "conversion_id": conversion_id,
                "queue_position": queue_position,
            }
        ),
    )

    return {"ok": True}


# @home_bp.route("/api/next-audio", methods=["GET"])
# def latest_conversion():
#     r = current_app.extensions["redis"]
#
#     # Get all job keys
#     keys = r.keys("job:*")
#     if not keys:
#         return jsonify({"conversion_path": None})
#
#     # Find the newest completed job
#     for key in reversed(sorted(keys)):
#         data = r.hgetall(key)
#         if data.get("status") == "complete" and data.get("conversion_path"):
#             r.hset(key, "status", "queued")
#
#             return jsonify(
#                 {
#                     "conversion_path": data["conversion_path"],
#                     "conversion_id": key.split(":")[1],
#                 }
#             )
#
#     return jsonify({"conversion_path": None})
#


# @home_bp.route("/api/mark-played", methods=["POST"])
# def mark_played():
#     r = current_app.extensions["redis"]
#     data = request.get_json()
#     path = data.get("conversion_path")
#     keys = r.keys("job:*")
#
#     for key in keys:
#         if r.hget(key, "conversion_path") == path:
#             r.hset(key, "status", "played")
#             break
#
#     return {"ok": True}
