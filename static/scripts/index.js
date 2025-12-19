const jobsDiv = document.getElementById("jobs");
let poller;
const downloadAllBtn = document.getElementById("download-all");
const notifiedFailures = new Set();
downloadAllBtn.disabled = true;

document.addEventListener("DOMContentLoaded", () => {
    const openBtn = document.querySelector(".request-btn");
    const modal = document.getElementById("requestModal");

    const lyricsBtn = document.querySelector(".lyrics-btn");
    const wrapper = document.querySelector(".request-box-wrapper");

    openBtn.addEventListener("click", () => {
      modal.classList.add("active");
    });

    modal.addEventListener("click", e => {
      if (e.target === modal) {
        modal.classList.remove("active");
      }
    });

    lyricsBtn.addEventListener("click", () => {
      wrapper.classList.toggle("show-lyrics");
    });
});


document.getElementById("start").onclick = async () => {
    jobsDiv.innerHTML = "";

    const res = await fetch("/start", { method: "POST" });
    const data = await res.json();

    data.job_ids.forEach((jobId, idx) => {
        const el = document.createElement("div");
        el.className = "job";
        el.innerHTML = `
            <div class="label">Job ${idx+1} of ${data.job_ids.length}</div>
            <div class="bar-container">
                <div class="bar" id="bar-${jobId}"></div>
            </div>
            <span class="status-text" id="status-${jobId}">pending</span>
            <span class="done" id="done-${jobId}">✔ Success</span>
            <div class="job-error" id="error-${jobId}" style="display:none;"></div>
            <button class="download-btn" id="download-${jobId}" style="display:none;">Download</button>
        `;
        jobsDiv.appendChild(el); });

    // start polling
    poller = setInterval(checkStatus, 5000);
};


async function checkStatus() {
    const res = await fetch("/status");
    const jobs = await res.json();

    let completedCount = 0;

    for (const [jobId, info] of Object.entries(jobs)) {
        const bar = document.getElementById(`bar-${jobId}`);
        const statusText = document.getElementById(`status-${jobId}`);
        const done = document.getElementById(`done-${jobId}`);

        let status = "pending";
        if (typeof info === "string") {
            // for old simple statuses
            status = info;
        } else if (info.status) {
            status = info.status;
        }

        // Update status text
        statusText.innerText = status;

        // Update progress bar
        if (status === "pending") {
            bar.style.width = "0%";
        } else if (status === "creating_music") {
            bar.style.width = "50%";
        } 
        else if (status === "complete") {
            bar.style.width = "100%";
            done.style.display = "inline";
            const downloadBtn = document.getElementById(`download-${jobId}`);
            downloadBtn.style.display = "inline";
            downloadBtn.onclick = () => {
                window.location.href = `/download/${jobId}`;
            };
          completedCount+=1
        }
        else if (status === "failed") {
            bar.style.width = "100%";
            bar.style.backgroundColor = "#e53935"; // red
            statusText.innerText = "failed ❌";

            if (errorEl && errorMsg) {
                errorEl.innerText = errorMsg;
                errorEl.style.display = "block";
            }

            // Notify only once
            if (!notifiedFailures.has(jobId)) {
                notifiedFailures.add(jobId);
                console.error(`Job ${jobId} failed:`, errorMsg);
            }
        }
    }

    // fetch running cost
    const costRes = await fetch("/cost");
    const costData = await costRes.json();
    document.getElementById("cost-display").innerText =
        `Total cost: $${costData.total_cost.toFixed(2)}`;

    if (completedCount === Object.keys(jobs).length && completedCount > 0) {
        downloadAllBtn.disabled = false;
    }

   // Enable "Download All" only if all jobs are complete
    if (completedCount === Object.keys(jobs).length && completedCount > 0) {
        downloadAllBtn.disabled = false;
        clearInterval(poller)
    } else {
        downloadAllBtn.disabled = true;
    }
}
