from flask import Flask, request, jsonify, render_template, send_file
import requests
import os
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

MAX_CONCURRENT_JOBS = 5
job_semaphore = Semaphore(MAX_CONCURRENT_JOBS)


def process_job(job_id, text, webhook_url):
    with job_semaphore:  # ⬅️ blocks if 5 jobs already running
        #     JOBS[job_id] = "creating_music"
        #     sw = SongWriter()
        #     lyrics = sw.create_lyrics(text)
        #     conversion_ids = sw.create_music(lyrics, webhook_url)
        #     for conv_id in conversion_ids:
        #         JOB_MAPPING[conv_id] = job_id

        JOBS[job_id] = "creating_music"  # status update
        time.sleep(random.choice(range(5, 10)))  # simulate work
        JOBS[job_id] = "complete"


def fetch_music_results(job_id, data):
    headers = {"Authorization": SongWriter().musicgpt_key}

    # retrieve first part
    url1 = f"https://api.musicgpt.com/api/public/v1/byId?conversionType=MUSIC_AI&conversion_id={data['conversion_id_1']}&task_id={data['task_id']}"
    response1 = requests.get(url1, headers=headers)
    result1 = response1.json()

    # retrieve second part
    url2 = f"https://api.musicgpt.com/api/public/v1/byId?conversionType=MUSIC_AI&conversion_id={data['conversion_id_2']}&task_id={data['task_id']}"
    response2 = requests.get(url2, headers=headers)
    result2 = response2.json()

    # aggregate results
    JOBS[job_id] = {"status": "complete", "music_results": [result1, result2]}


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
        job_id += 1

        JOBS[job_id] = "pending"
        job_ids.append(job_id)

        threading.Thread(
            target=process_job, args=(job_id, text, webhook_url), daemon=True
        ).start()

    return jsonify({"job_ids": job_ids})


@app.route("/status", methods=["GET"])
def status():
    return jsonify(JOBS)


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    # Step 1: mark job as complete in JOBS dict (or "processing_music")
    conversion_id = data.get("conversion_id_1") or data.get("conversion_id_2")
    job_id = JOB_MAPPING.get(conversion_id)

    # Step 2: fetch MusicGPT results
    threading.Thread(
        target=fetch_music_results, args=(job_id, data), daemon=True
    ).start()

    return {"ok": True}


@app.route("/download/<int:job_id>")
def download_job(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "complete":
        return {"error": "Job not complete or not found"}, 404

    # music_results should have two items per job
    music_results = job.get("music_results", [])
    if not music_results:
        return {"error": "Music results not ready"}, 400

    # create a zip in memory
    zip_buffer = BytesIO()
    with ZipFile(zip_buffer, "w") as zip_file:
        for idx, data in enumerate(music_results, start=1):
            conversion_url = data.get("audio_url")  # replace with actual link field
            if not conversion_url:
                continue
            r = requests.get(conversion_url)
            filename = f"chapter{job_id}_track{idx}.mp3"
            zip_file.writestr(filename, r.content)

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
            music_results = job.get("music_results", [])
            for idx, data in enumerate(music_results, start=1):
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
