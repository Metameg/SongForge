
import { initializeRadioPlayer } from "./api/radio.js";

document.addEventListener("DOMContentLoaded", () => {
    const modal = document.getElementById("requestModal");
    const requestBtn = document.getElementById("requestBtn"); 
    const playBtn = document.getElementById("playBtn");
    const lyricsBtn = document.querySelector(".lyrics-btn");
    const wrapper = document.querySelector(".request-box-wrapper");


    const radio = initializeRadioPlayer({
      audioEl: document.getElementById("radioAudio"),
      playButton: playBtn,
      nowPlayingEl: document.getElementById("nowPlaying"),
      queueStatusEl: document.getElementById("queueStatus"),
      thumbnailEl: document.getElementById("thumbnailImg")
    });



    requestBtn.addEventListener("click", async () => {
      modal.classList.add("active");

      const res = await fetch("/api/my-queue-position");
      const data = await res.json();
      if (data.in_queue) {
        submitBtn.disabled = true;
        document.getElementById("submitError").textContent = "⚠ You currently have a song being created or in queue. Please wait for your song to start playing before submitting another request.";
        return
      }

    });

    modal.addEventListener("click", e => {
      if (e.target === modal) {
        modal.classList.remove("active");
      }
    });

    lyricsBtn.addEventListener("click", () => {
      wrapper.classList.toggle("show-lyrics");
      lyricsBtn.classList.toggle("active");
    });



    document.querySelector(".modal-close-btn").onclick = () => {
      document.getElementById("prompt").value = "";
      document.getElementById("lyrics").value = "";
      document.getElementById("requestModal").classList.remove("active");
    };

    

    document.getElementById("closeModalBtn").onclick = () => {
      document.getElementById("prompt").value = "";
      document.getElementById("lyrics").value = "";
      document.getElementById("requestModal").classList.remove("active");
    };
});




