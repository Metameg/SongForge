
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
      document.getElementById("submitError").textContent = "";

      const res = await fetch("/api/my-queue-position");
      const data = await res.json();
      const submitBtn = document.getElementById("submitBtn");

      if (data.has_active_job) {
        submitBtn.disabled = true;
        const msg = data.in_queue
          ? "You already have a song in the queue. Wait for it to play before submitting another."
          : "Your song is currently playing. You can create another once it finishes.";
        document.getElementById("submitError").textContent = msg;
      } else {
        submitBtn.disabled = false;
        submitBtn.textContent = "Submit";
        submitBtn.classList.remove("loading", "success");
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




