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

GENESIS_FOLDER = "Genesis"
os.makedirs(GENESIS_FOLDER, exist_ok=True)

app = Flask(__name__)

# simple in-memory state (fine for dev)
JOBS = {}
JOB_MAPPING = {}
TOTAL_COST = 0.0
COST_LOCK = threading.Lock()
CONVERSIONS_PER_CALL = 2
MAX_CONCURRENT_JOBS = 5
job_semaphore = Semaphore(MAX_CONCURRENT_JOBS)


def process_job(job_id, text, webhook_url):
    global TOTAL_COST
    with job_semaphore:  # ⬅️ blocks if 5 jobs already running
        JOBS[job_id] = {
            "status": "creating_music",
            "urls": [],
            "album_cover": None,
        }
        sw = SongWriter()
        lyrics = sw.create_lyrics(text)
        conversion_ids, cost = sw.create_music(lyrics, webhook_url)
        for conv_id in conversion_ids:
            JOB_MAPPING[conv_id] = job_id

        with COST_LOCK:
            TOTAL_COST += cost * CONVERSIONS_PER_CALL

        # global TOTAL_COST
        # JOBS[job_id] = "creating_music"  # status update
        # time.sleep(random.choice(range(5, 10)))  # simulate work
        # TOTAL_COST += 1.00
        # JOBS[job_id] = "complete"


def build_job_data(job_id, conversion_path, album_cover_path):
    # headers = {"Authorization": SongWriter().musicgpt_key}
    # print("made it to fetch")

    # retrieve first part
    # url = f"https://api.musicgpt.com/api/public/v1/byId?conversionType=MUSIC_AI&conversion_id={data['conversion_id']}&task_id={data['task_id']}"
    # response = requests.get(url, headers=headers)
    # result = response.json()

    # retrieve second part
    # url2 = f"https://api.musicgpt.com/api/public/v1/byId?conversionType=MUSIC_AI&conversion_id={data['conversion_id_2']}&task_id={data['task_id']}"
    # response2 = requests.get(url2, headers=headers)
    # result2 = response2.json()

    # aggregate results
    if job_id not in JOBS:
        JOBS[job_id] = {
            "status": "creating_music",
            "urls": [conversion_path],
            "album_cover": album_cover_path,
        }
    else:
        JOBS[job_id]["urls"].append(conversion_path)

        JOBS[job_id]["album_cover"] = (
            album_cover_path  # mark complete only when you actually have both results
        )
        if len(JOBS[job_id]["urls"]) == CONVERSIONS_PER_CALL:
            JOBS[job_id]["status"] = "complete"

            print("RESULTS MUSIX:")
            pprint(JOBS[job_id])


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    JOBS.clear()
    webhook_url = request.host_url.rstrip("/") + "/webhook"

    data = BibleDataset()
    job_id = 0
    job_ids = []
    for chapter, text in data.books[1].items():
        if (
            chapter == 2
            or chapter == 1
            or chapter == 3
            or chapter == 4
            or chapter == 5
            or chapter == 6
            or chapter == 7
            or chapter == 8
        ):
            continue
        job_id += 1

        JOBS[job_id] = {"status": "pending", "urls": [], "album_cover": None}

        job_ids.append(job_id)

        threading.Thread(
            target=process_job, args=(job_id, text, webhook_url), daemon=True
        ).start()
        break

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

    print("WEBHOOK_DATA:")
    pprint(data)

    # Step 1: mark job as complete in JOBS dict (or "processing_music")
    if "conversion_path" in data:
        conversion_id = data.get("conversion_id")
        album_cover_path = data.get("album_cover_path")
        conversion_id_path = data.get("conversion_path")
        job_id = JOB_MAPPING.get(conversion_id)
        build_job_data(job_id, conversion_id_path, album_cover_path)
        # Step 2: fetch MusicGPT results
        # threading.Thread(
        #     target=build_job_data,
        #     args=(job_id, conversion_id_path, album_cover_path),
        #     daemon=True,
        # ).start()

    return {"ok": True}


@app.route("/download/<int:job_id>")
def download_job(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "complete":
        return {"error": "Job not complete or not found"}, 404

    # urls should have two items per job
    urls = job.get("urls", [])
    album_cover_url = job.get("album_cover")
    if not urls:
        return {"error": "Music results not ready"}, 400

    print("JOB", job)
    # create a zip in memory
    zip_buffer = BytesIO()
    with ZipFile(zip_buffer, "w") as zip_file:
        for idx, url in enumerate(urls, start=1):
            r = requests.get(url)
            filename = f"chapter{job_id}_track{idx}.mp3"
            zip_file.writestr(filename, r.content)

        zip_file.writestr("album_cover.jpg", requests.get(album_cover_url).content)

    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype="application/zip",
        download_name=f"Genesis_job{job_id}.zip",
        as_attachment=True,
    )


@app.route("/download_all")
def download_all():
    # collect all completed jobs
    completed_jobs = {
        job_id: job for job_id, job in JOBS.items() if job.get("status") == "complete"
    }
    if not completed_jobs:
        return {"error": "No completed jobs"}, 404

    zip_buffer = BytesIO()
    with ZipFile(zip_buffer, "w") as zip_file:
        for job_id, job in completed_jobs.items():
            urls = job.get("urls", [])
            for idx, data in enumerate(urls, start=1):
                conversion_url = data.get("audio_url")  # replace with correct field
                if not conversion_url:
                    continue
                r = requests.get(conversion_url)
                # store in folder per job inside zip
                filename = f"Genesis/job{job_id}/track{idx}.mp3"
                zip_file.writestr(filename, r.content)

    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype="application/zip",
        download_name="Genesis_All_Jobs.zip",
        as_attachment=True,
    )


if __name__ == "__main__":
    app.run(debug=True)
