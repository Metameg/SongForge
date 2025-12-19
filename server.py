from flask import Flask, request, jsonify, render_template, send_file
import requests
import os
from pprint import pprint
import time
import threading
from threading import Semaphore
import random
from songwriter import SongWriter
from dataset import BibleDataset
from io import BytesIO
from zipfile import ZipFile
from pathlib import Path

BOOK_DIR = "Genesis"
os.makedirs(BOOK_DIR, exist_ok=True)

app = Flask(__name__)

# simple in-memory state (fine for dev)
JOBS = {}
JOB_MAPPING = {}
TOTAL_COST = 0.0
COST_LOCK = threading.Lock()
CONVERSIONS_PER_CALL = 2
MAX_CONCURRENT_JOBS = 5
STARTING_CHAPTER = 10
job_semaphore = Semaphore(MAX_CONCURRENT_JOBS)


def process_job(job_id, text, webhook_url):
    global TOTAL_COST
    job_semaphore.acquire()  # with job_semaphore:  # ⬅️ blocks if 5 jobs already running

    JOBS[job_id]["status"] = "creating_music"

    try:
        sw = SongWriter()
        lyrics = sw.create_lyrics(text)
        conversion_ids, cost, error = sw.create_music(lyrics, webhook_url)

        if error:
            fail_job(job_id, error)
            return

        for conv_id in conversion_ids:
            JOB_MAPPING[conv_id] = job_id

        with COST_LOCK:
            TOTAL_COST += cost * CONVERSIONS_PER_CALL

    except Exception as e:
        fail_job(job_id, f"Unhandled exception: {e}")


# def build_job_data(job_id, conversion_path, album_cover_path):
#     # aggregate results
#     if job_id not in JOBS:
#         JOBS[job_id] = {
#             "status": "creating_music",
#             "urls": [conversion_path],
#             "album_cover": album_cover_path,
#         }
#     else:
#         JOBS[job_id]["urls"].append(conversion_path)
#
#         JOBS[job_id]["album_cover"] = (
#             album_cover_path  # mark complete only when you actually have both results
#         )
#         if len(JOBS[job_id]["urls"]) == CONVERSIONS_PER_CALL:
#             JOBS[job_id]["status"] = "complete"
#             save_job(job_id)
#             job_semaphore.release()


def fail_job(job_id, reason):
    job = JOBS.get(job_id)
    if not job:
        return

    if job["status"] in ("complete", "failed"):
        return  # prevent double release

    job["status"] = "failed"
    job["error"] = reason

    print(f"[JOB FAILED] {job_id}: {reason}")

    # 🔓 VERY IMPORTANT
    job_semaphore.release()


def save_job(job_id):
    job = JOBS[job_id]
    chapter_dir = Path(BOOK_DIR) / f"chapter{job_id - 1}"
    chapter_dir.mkdir(parents=True, exist_ok=True)

    # save tracks
    for idx, url in enumerate(job.get("urls", []), start=1):
        r = requests.get(url, timeout=30)
        r.raise_for_status()

        track_path = chapter_dir / f"track{idx}.mp3"
        track_path.write_bytes(r.content)

    # save album cover
    album_cover_url = job.get("album_cover")
    if album_cover_url:
        r = requests.get(album_cover_url, timeout=30)
        r.raise_for_status()
        (chapter_dir / "album_cover.jpg").write_bytes(r.content)


# ---- ENDPOINTS ------
@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    JOBS.clear()
    webhook_url = request.host_url.rstrip("/") + "/webhook"

    data = BibleDataset()
    job_id = STARTING_CHAPTER
    job_ids = []
    for chapter, text in data.books[1].items():
        if chapter in range(1, STARTING_CHAPTER):
            continue
        job_id += 1

        JOBS[job_id] = {
            "status": "pending",
            "urls": [],
            "album_cover": None,
            "error": None,
        }

        job_ids.append(job_id)

        threading.Thread(
            target=process_job, args=(job_id, text, webhook_url), daemon=True
        ).start()

    return jsonify({"job_ids": job_ids})


@app.route("/status", methods=["GET"])
def status():
    return jsonify(JOBS)


@app.route("/cost", methods=["GET"])
def cost():
    return jsonify({"total_cost": round(TOTAL_COST, 4)})


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    # Step 1: mark job as complete in JOBS dict (or "processing_music")
    if "conversion_path" in data:
        conversion_id = data.get("conversion_id")
        album_cover_path = data.get("album_cover_path")
        conversion_path = data.get("conversion_path")
        job_id = JOB_MAPPING.get(conversion_id)
        job = JOBS[job_id]

        job["urls"].append(conversion_path)

        if len(job["urls"]) == CONVERSIONS_PER_CALL:
            job["album_cover"] = album_cover_path
            job["status"] = "complete"
            save_job(job_id)
            job_semaphore.release()
        # build_job_data(job_id, conversion_id_path, album_cover_path)

    return {"ok": True}


@app.route("/download/<int:job_id>")
def download(job_id):
    chapter_dir = Path(BOOK_DIR) / f"chapter{job_id - 1}"

    if not chapter_dir.exists():
        return {"error": "Files not found"}, 404

    zip_buffer = BytesIO()
    with ZipFile(zip_buffer, "w") as zip_file:
        for file in chapter_dir.iterdir():
            zip_file.write(file, arcname=file.name)

    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype="application/zip",
        download_name=f"Genesis_chapter{job_id - 1}.zip",
        as_attachment=True,
    )


@app.route("/download_all")
def download_all():
    genesis_dir = Path("Genesis")

    if not genesis_dir.exists():
        return {"error": "No Genesis folder"}, 404

    zip_buffer = BytesIO()
    with ZipFile(zip_buffer, "w") as zip_file:
        for file in genesis_dir.rglob("*"):
            if file.is_file():
                zip_file.write(file, arcname=file.relative_to(genesis_dir))

    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype="application/zip",
        download_name="Genesis_All_Jobs.zip",
        as_attachment=True,
    )


if __name__ == "__main__":
    app.run(debug=True)
