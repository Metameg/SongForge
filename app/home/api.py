from flask import session, request, jsonify, current_app
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
from mutagen._file import File as MutagenFile
from .utilities import emit_job_status, emit_queue_position_to_client
# app = Flask(__name__)


@home_bp.route("/api/radio/now-playing", methods=["GET"])
def now_playing():
    r = current_app.extensions["redis"]
    song = r.hgetall("radio:now_playing")
    started_at = r.get("radio:started_at")
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


def simulate_webhook(webhook_url, payload):
    requests.post(webhook_url, json=payload, timeout=10)


@home_bp.route("/api/create-song", methods=["POST"])
def create_song():
    r = current_app.extensions["redis"]

    client_id = session.get("client_id")
    if not client_id:
        return {"ok": False, "error": "No client session"}, 401

    data = request.get_json(silent=True)

    if not data:
        return jsonify({"status": "failed", "message": "Invalid JSON payload"}), 400

    lyrics = data.get("lyrics", "").strip()
    prompt = data.get("prompt", "").strip()

    if not prompt:
        return jsonify({"status": "failed", "message": "Prompt is required"}), 400

    try:
        emit_job_status(
            socketio,
            client_id,
            status="processing",
            message="Creating Song",
        )
        client = MusicAPIClient(
            open_ai_key=current_app.config["OPEN_AI_KEY"],
            musicgpt_key=current_app.config["MUSICGPT_KEY"],
            webhook_url=current_app.config["WEBHOOK_URL"],
        )

        # MUSICGPT CALL
        conversion_ids, cost, error = client.create_music(prompt, lyrics)

        # payload = {
        #     "conversion_id": "test-conversion-123",
        #     "conversion_path": "/audio/song_1.mp3",
        # }
        #
        # threading.Thread(
        #     target=simulate_webhook,
        #     args=(current_app.config["WEBHOOK_URL"], payload),
        #     daemon=True,
        # ).start()
        conversion_id = conversion_ids[0]
        job_key = f"job:{conversion_id}"
        # job_key = "job:4fea5fd7-a903-4930-a711-16ad8bf2c43"
        r.hset(
            job_key,
            mapping={
                "client_id": client_id,
                "status": "processing",
                "prompt": prompt,
                "lyrics": lyrics,
                "created_at": int(time.time() * 1000),
            },
        )

        return jsonify({"status": "success"})

    except Exception as e:
        current_app.logger.exception(e)
        return jsonify({"status": "failed", "message": "Unexpected server error"}), 500

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
    conversion_path = data.get("conversion_path")
    conversion_id = data.get("conversion_id")

    job_key = f"job:{conversion_id}"

    job = r.hgetall(job_key)

    if not conversion_id or not conversion_path or not job:
        print("job still doesn't exist")
        return {"ok": False, "error": "Missing required fields"}, 400

    # Check duplicate queues
    playlist_entries = r.lrange(current_app.config["PLAYLIST_DYNAMIC_KEY"], 0, -1)
    already_queued = False
    for entry in playlist_entries:
        data = json.loads(entry)
        if f"job:{data.get('conversion_id')}" == job_key:
            already_queued = True
            break

    if already_queued:
        print("already queued")
        return {"ok": True, "already_queued": True}
    print("DATA:")
    pprint(data)
    #
    # conversion_path = "https://lalals.s3.amazonaws.com/conversions/standard/3f2f4043-a87b-4544-8316-3c3655e4109e/3f2f4043-a87b-4544-8316-3c3655e4109e.mp3"
    # conversion_id = "4fea5fd7-a903-4930-a711-16ad8bf2c43"
    # duration = 226
    # try:

    client_id = job["client_id"]

    if data.get("subtype") == "music_ai":
        r.hset(
            job_key,
            mapping={
                "album_cover": data.get("album_cover_path"),
                "conversion_path": conversion_path,
                "duration": float(data.get("conversion_duration")),
                "title": data.get("title"),
                "status": "queued",
                "source": "dynamic",
            },
        )

        emit_job_status(
            socketio,
            client_id,
            status="queued",
            message="Song Finished",
        )

        payload = {
            "conversion_id": conversion_id,
            "client_id": client_id,
        }

        r.rpush(
            current_app.config["PLAYLIST_DYNAMIC_KEY"],
            json.dumps(payload),
        )

        # emit_job_status(
        #     socketio,
        #     client_id,
        #     conversion_id,
        #     status="queued",
        #     message="Song added to queue",
        # )

        emit_queue_position_to_client(socketio, client_id)

        return {
            "ok": True,
            "message": "Song Finished",
        }

    return {"ok": True}

    # duration = 0
    # if data.get("subtype") == "lyrics_timestamped":
    #     emit_job_status(
    #         socketio,
    #         client_id,
    #         conversion_id,
    #         status="lyrics_timestamped",
    #         message="Lyrics synchronized",
    #     )

    # lyrics = json.loads(data.get("lyrics_timestamped"))
    # duration_ms = max((line.get("end", 0) for line in lyrics), default=0)
    # duration = duration_ms / 1000.0
    # except (json.JSONDecodeError, TypeError):
    #     duration = None

    # Persist job metadata
    # r.hset(
    #     job_key,
    #     mapping={
    #         "conversion_path": conversion_path,
    #         "duration": float(duration),
    #         "status": "queued",
    #         "source": "dynamic",
    #     },
    # )

    # Push to queue

    # return {
    #     "ok": True,
    #     "subtype": data.get("subtype"),
    #     "message": "successfully created song",
    # }


@home_bp.route("/api/my-queue-position", methods=["GET"])
def my_queue_position():
    r = current_app.extensions["redis"]

    # get the client_id from the session
    client_id = session.get("client_id")
    print("checking queue", client_id)
    if not client_id:
        return {"in_queue": False, "message": "No client ID found."}

    raw_items = r.lrange(current_app.config["PLAYLIST_DYNAMIC_KEY"], 0, -1)

    for idx, raw in enumerate(raw_items):
        try:
            item = json.loads(raw.decode())
        except Exception:
            continue

        if item.get("session_id") == client_id:  # <- match against client_id only
            return {
                "in_queue": True,
                "queue_position": idx + 1,
                "queue_length": len(raw_items),
            }

    return {"in_queue": False, "message": "You currently have no songs in queue."}


@home_bp.route("/api/mark-played", methods=["POST"])
def mark_played():
    r = current_app.extensions["redis"]
    data = request.get_json()
    cid = data.get("cid")
    keys = r.keys("job:*")

    if (
        cid.startswith("static")
        or r.lpos(current_app.config["HISTORY_KEY"], cid) is not None
    ):
        print("not adding to history")
        return {"ok": True}

    for key in keys:
        if key == f"job:{cid}":
            r.hset(key, "status", "played")

            r.rpush(current_app.config["HISTORY_KEY"], key)

            # check length
            length = r.llen(current_app.config["HISTORY_KEY"])

            if length > current_app.config["MAX_HISTORY_JOBS"]:
                # get the oldest job in history
                evicted = r.lrange(current_app.config["HISTORY_KEY"], 0, 0)

                # trim history to keep newest job
                r.ltrim(current_app.config["HISTORY_KEY"], 1, -1)

                # delete evicted job hashes
                for cid in evicted:
                    r.delete(f"job:{cid}")

            break

    return {"ok": True}
