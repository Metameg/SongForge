from flask import session, request, jsonify, current_app
import requests
import os
import json
import time
import threading
from datetime import datetime, timedelta, timezone
from app.extensions import socketio
from .services import MusicAPIClient
from . import home_bp
import redis
from mutagen._file import File as MutagenFile
from .utilities import (
    emit_job_status,
    emit_queue_position_to_client,
    remove_from_processing,
)
from app.radio.controller import emit_queue_positions

DAILY_SONG_LIMIT = 5
PARALLEL_GENERATION_LIMIT = 5
SIM_DELAY_SECONDS = 5  # how long to wait before firing the simulated webhook


def _run_simulated_webhook(app, job_key, conversion_id, client_id):
    """Dev-mode only: simulate MusicGPT webhook delivery after a short delay."""
    time.sleep(SIM_DELAY_SECONDS)
    with app.app_context():
        r = app.extensions["redis"]

        audio_path = "https://lalals.s3.amazonaws.com/conversions/standard/c3b77b63-4f91-44b1-8e75-da8a6cc7fad7/c3b77b63-4f91-44b1-8e75-da8a6cc7fad7.mp3"
        duration = 120

        prompt = r.hget(job_key, "prompt") or "unknown"
        title = f"[SIM] {prompt[:50]}"

        r.decr("musicgpt:active_generations")

        r.hset(job_key, "conversion_path", audio_path)
        r.hset(job_key, "duration", duration)
        r.hset(job_key, "title", title)
        r.hset(job_key, "album_cover", "")
        r.hset(job_key, "status", "queued")
        r.hset(job_key, "source", "dynamic")

        emit_job_status(socketio, client_id, status="queued", message="Song created and added to queue!")

        remove_from_processing(r, app.config["PLAYLIST_PROCESSING_KEY"], job_key)
        queue_payload = {"conversion_id": conversion_id, "client_id": client_id}
        r.rpush(app.config["PLAYLIST_DYNAMIC_KEY"], json.dumps(queue_payload))

        queue_length = r.llen(app.config["PLAYLIST_DYNAMIC_KEY"])
        emit_queue_positions(r)
        r.publish("queue_events", json.dumps({"queue_length": queue_length}))


@home_bp.route("/api/debug/status", methods=["GET"])
def debug_status():
    """Dev-only: check tunnel URL, active generations, and Redis health."""
    r = current_app.extensions["redis"]
    return jsonify({
        "webhook_url": r.get("config:webhook_url") or current_app.config.get("WEBHOOK_URL"),
        "active_generations": int(r.get("musicgpt:active_generations") or 0),
        "dynamic_queue_length": r.llen(current_app.config["PLAYLIST_DYNAMIC_KEY"]),
        "processing_queue_length": r.llen(current_app.config["PLAYLIST_PROCESSING_KEY"]),
    })


@home_bp.route("/api/radio/now-playing", methods=["GET"])
def now_playing():
    r = current_app.extensions["redis"]
    song = r.hgetall("radio:now_playing")
    started_at = r.get("radio:started_at")
    if not song or not started_at:
        return jsonify({"playing": False})

    cid = song.get("conversion_id", "")
    job_key = song.get("job_key", f"job:{cid}")
    job = r.hgetall(job_key) if job_key else {}

    return jsonify(
        {
            "playing": True,
            "cid": cid,
            "conversion_path": song["conversion_path"],
            "started_at": int(started_at),
            "duration": float(song.get("duration", 0)),
            "source": song.get("source", "static"),
            "title": job.get("title", ""),
            "album_cover": job.get("album_cover", ""),
        }
    )


@home_bp.route("/api/create-song", methods=["POST"])
def create_song():
    r = current_app.extensions["redis"]

    client_id = session.get("client_id")
    if not client_id:
        return jsonify({"status": "failed", "message": "No client session. Please refresh the page."}), 401

    # Gate 1: one song at a time — must wait until current song finishes playing
    active_job_key = f"client:{client_id}:active_job"
    if r.exists(active_job_key):
        return jsonify({
            "status": "failed",
            "message": "You already have a song in progress. Please wait for it to finish playing before creating another.",
        }), 409

    # Gate 2: daily limit of 5 songs per user (resets at midnight UTC)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_key = f"client:{client_id}:daily:{today}"
    daily_count = int(r.get(daily_key) or 0)
    if daily_count >= DAILY_SONG_LIMIT:
        now = datetime.now(timezone.utc)
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        reset_seconds = int((midnight - now).total_seconds())
        hours = reset_seconds // 3600
        minutes = (reset_seconds % 3600) // 60
        return jsonify({
            "status": "failed",
            "message": (
                f"You've reached your daily limit of {DAILY_SONG_LIMIT} songs. "
                f"You can create more songs in {hours}h {minutes}m."
            ),
        }), 429

    # Gate 3: platform-wide parallel generation cap (API subscription limit)
    active_generations = int(r.get("musicgpt:active_generations") or 0)
    if active_generations >= PARALLEL_GENERATION_LIMIT:
        return jsonify({
            "status": "failed",
            "message": "There are too many songs being generated right now. Please try again later.",
        }), 503

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "failed", "message": "Invalid request payload."}), 400

    # Turnstile verification
    # turnstile_secret = current_app.config.get("TURNSTILE_SECRET_KEY")
    # if turnstile_secret:
    #     token = data.get("cf-turnstile-response", "")
    #     verify = requests.post(
    #         "https://challenges.cloudflare.com/turnstile/v0/siteverify",
    #         data={"secret": turnstile_secret, "response": token},
    #         timeout=5,
    #     )
    #     if not verify.json().get("success"):
    #         return jsonify({"status": "failed", "message": "Human verification failed. Please try again."}), 403

    lyrics = data.get("lyrics", "").strip()
    prompt = data.get("prompt", "").strip()

    if not prompt:
        return jsonify({"status": "failed", "message": "A prompt is required to create a song."}), 400

    if len(prompt) > 280:
        return jsonify({"status": "failed", "message": "Prompt must be 280 characters or fewer."}), 400

    job_key = None
    try:
        emit_job_status(socketio, client_id, status="processing", message="Creating Song")

        r.incr("musicgpt:active_generations")
        r.incr(daily_key)
        r.expire(daily_key, 90000)

        if current_app.config.get("DEV_SIMULATE_WEBHOOK"):
            import uuid
            conversion_id = str(uuid.uuid4())
        else:
            webhook_url = r.get("config:webhook_url") or current_app.config["WEBHOOK_URL"]
            api_client = MusicAPIClient(
                open_ai_key=current_app.config["OPEN_AI_KEY"],
                musicgpt_key=current_app.config["MUSICGPT_KEY"],
                webhook_url=webhook_url,
            )
            if lyrics:
                lyrics = api_client.create_lyrics(lyrics)
            conversion_ids, cost, error = api_client.create_music(prompt, lyrics)
            if error:
                r.decr("musicgpt:active_generations")
                r.decr(daily_key)
                if "402" in error:
                    msg = "The platform has run out of credits. Please contact the administrator."
                elif "500" in error:
                    msg = "The music generation service is temporarily unavailable. Please try again later."
                else:
                    msg = "Song generation failed. Please try again later."
                current_app.logger.error(f"MusicGPT error for client {client_id}: {error}")
                return jsonify({"status": "failed", "message": msg}), 503
            conversion_id = conversion_ids[0]

        job_key = f"job:{conversion_id}"

        payload = {
            "job_key": job_key,
            "conversion_id": conversion_id,
            "client_id": client_id,
            "status": "processing",
            "prompt": prompt,
            "lyrics": lyrics,
            "created_at": int(time.time() * 1000),
        }

        r.rpush(current_app.config["PLAYLIST_PROCESSING_KEY"], json.dumps(payload))
        for k, v in payload.items():
            r.hset(job_key, k, v)

        r.set(active_job_key, job_key, ex=14400)

        if current_app.config.get("DEV_SIMULATE_WEBHOOK"):
            app = current_app._get_current_object()
            threading.Thread(
                target=_run_simulated_webhook,
                args=(app, job_key, conversion_id, client_id),
                daemon=True,
            ).start()

        return jsonify({"status": "success"})

    except Exception as e:
        r.decr("musicgpt:active_generations")
        r.delete(active_job_key)
        r.decr(daily_key)
        if job_key:
            remove_from_processing(r, current_app.config["PLAYLIST_PROCESSING_KEY"], job_key)
        current_app.logger.exception(e)
        return jsonify({"status": "failed", "message": "An unexpected error occurred. Please try again."}), 500


@home_bp.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if not data:
        return {"ok": False, "error": "No payload"}, 400

    r = current_app.extensions["redis"]

    subtype = data.get("subtype", "")
    conversion_id = data.get("conversion_id")

    import json as _json
    current_app.logger.info(f"WEBHOOK subtype={subtype!r} conversion_id={conversion_id!r} payload={_json.dumps(data, indent=2)}")

    # Album cover arrives in a separate webhook before music_ai — attach it to the job early
    if subtype == "album_cover_generation":
        cover_url = data.get("image_path") or ""
        # The cover payload has conversion_id_1/2 but no single conversion_id
        for cid_field in ("conversion_id_1", "conversion_id_2"):
            cid = data.get(cid_field)
            if cid and r.exists(f"job:{cid}"):
                r.hset(f"job:{cid}", "album_cover", cover_url)
        return {"ok": True}

    # Only continue processing the main music generation event
    if subtype != "music_ai":
        return {"ok": True, "skipped": True}

    if not conversion_id:
        return {"ok": False, "error": "Missing conversion_id"}, 400

    job_key = f"job:{conversion_id}"
    job = r.hgetall(job_key)

    if not job:
        # Not a job we're tracking (e.g., conversion_id_2 which we ignore)
        return {"ok": True, "skipped": True}

    # Deduplicate: MusicGPT retries webhook delivery — ignore if already queued/played
    if job.get("status") in ("queued", "played"):
        return {"ok": True, "skipped": True}

    conversion_path = data.get("conversion_path")
    if not conversion_path:
        return {"ok": False, "error": "Missing conversion_path"}, 400

    duration = float(data.get("conversion_duration") or 0)
    title = data.get("title") or "Untitled"
    # Use album_cover already set by the album_cover_generation webhook, fallback to music_ai payload
    existing_cover = r.hget(job_key, "album_cover") or ""
    album_cover = existing_cover or data.get("album_cover_path") or ""

    client_id = job.get("client_id")

    # Release generation slot
    r.decr("musicgpt:active_generations")

    # Persist final metadata on the job
    r.hset(job_key, "job_key", job_key)
    r.hset(job_key, "conversion_path", conversion_path)
    r.hset(job_key, "duration", duration)
    r.hset(job_key, "title", title)
    r.hset(job_key, "album_cover", album_cover)
    r.hset(job_key, "status", "queued")
    r.hset(job_key, "source", "dynamic")

    if client_id:
        emit_job_status(socketio, client_id, status="queued", message="Song created and added to queue!")

    # Move from processing → dynamic playback queue
    remove_from_processing(r, current_app.config["PLAYLIST_PROCESSING_KEY"], job_key)

    queue_payload = {"conversion_id": conversion_id, "client_id": client_id}
    r.rpush(current_app.config["PLAYLIST_DYNAMIC_KEY"], json.dumps(queue_payload))

    # Notify all queued clients of their updated positions (includes the new client)
    emit_queue_positions(r)

    # Notify all connected clients that the queue changed so they can refresh their own position
    queue_length = r.llen(current_app.config["PLAYLIST_DYNAMIC_KEY"])
    r.publish("queue_events", json.dumps({"queue_length": queue_length}))

    # active_job intentionally NOT deleted here —
    # user stays locked until their song finishes playing (handled in mark_played)

    return {"ok": True, "message": "Song queued successfully"}


@home_bp.route("/api/my-queue-position", methods=["GET"])
def my_queue_position():
    r = current_app.extensions["redis"]

    raw_items = r.lrange(current_app.config["PLAYLIST_DYNAMIC_KEY"], 0, -1)
    queue_length = len(raw_items)

    client_id = session.get("client_id")
    if not client_id:
        return {"in_queue": False, "has_active_job": False, "queue_length": queue_length}

    has_active_job = bool(r.exists(f"client:{client_id}:active_job"))

    # Check if this client's song is currently on air
    now_playing = r.hgetall("radio:now_playing")
    if now_playing.get("source") == "dynamic":
        job_key = now_playing.get("job_key")
        if job_key:
            job = r.hgetall(job_key)
            if job.get("client_id") == client_id:
                return jsonify({"now_playing": True, "conversion_id": now_playing.get("conversion_id"), "queue_length": queue_length})

    for idx, item in enumerate(raw_items):
        item = json.loads(item)
        if item.get("client_id") == client_id:
            return {
                "in_queue": True,
                "has_active_job": has_active_job,
                "queue_position": idx + 1,
                "queue_length": queue_length,
                "conversion_id": item.get("conversion_id"),
            }

    return {"in_queue": False, "has_active_job": has_active_job, "queue_length": queue_length}


@home_bp.route("/api/mark-played", methods=["POST"])
def mark_played():
    r = current_app.extensions["redis"]

    data = request.get_json()
    cid = data.get("cid")

    if not cid or cid.startswith("static"):
        return {"ok": True}

    history = r.lrange(current_app.config["HISTORY_KEY"], 0, -1)
    if f"job:{cid}" in history:
        return {"ok": True}

    job_key = f"job:{cid}"
    job = r.hgetall(job_key)

    if job:
        r.hset(job_key, "status", "played")
        r.rpush(current_app.config["HISTORY_KEY"], job_key)

        # Unlock the client so they can create their next song
        client_id = job.get("client_id")
        if client_id:
            r.delete(f"client:{client_id}:active_job")
            emit_job_status(
                socketio,
                client_id,
                status="played",
                message="Your song has finished playing! You can now create another song.",
            )
            # Push an empty queue state so the frontend resets immediately
            emit_queue_position_to_client(socketio, client_id)

        # Trim history to cap
        length = r.llen(current_app.config["HISTORY_KEY"])
        if length > current_app.config["MAX_HISTORY_JOBS"]:
            evicted = r.lrange(current_app.config["HISTORY_KEY"], 0, 0)
            r.ltrim(current_app.config["HISTORY_KEY"], 1, -1)
            for evicted_key in evicted:
                r.delete(evicted_key)

    return {"ok": True}
