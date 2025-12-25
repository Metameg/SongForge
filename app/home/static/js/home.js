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


