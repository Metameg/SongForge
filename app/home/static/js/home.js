
// import { RadioPlayer } from "./api/radio.js";
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
      queueStatusEl: document.getElementById("queueStatus")
    });



    requestBtn.addEventListener("click", async () => {
      modal.classList.add("active");

      const res = await fetch("/api/my-queue-position");
      const data = await res.json();
      console.log(data);
      if (data.in_queue) {
        console.log("has song");
        submitBtn.disabled = true;
        document.getElementById("submitError").textContent = "Only 1 song allowed in queue. Please wait for your song to play before generating a new song request.";
        return
      }

      // resetRequestModal();
    });

    modal.addEventListener("click", e => {
      if (e.target === modal) {
        modal.classList.remove("active");
      }
    });

    lyricsBtn.addEventListener("click", () => {
      wrapper.classList.toggle("show-lyrics");
    });



    document.querySelector(".modal-close-btn").onclick = () => {
      document.getElementById("requestModal").classList.remove("active");
    };

    // document.getElementById("newRequestBtn").onclick = async () => {
    //
    //   const res = await fetch("/api/my-queue-position");
    //   const data = await res.json();
    //   if (data.in_queue) {
    //     console.log("has song");
    //     submitBtn.disabled = true;
    //     document.getElementById("submitError").textContent = "Only 1 song allowed in queue. Please wait for your song to play before generating a new song request.";
    //     return
    //   }
    //
    //   resetRequestModal();
    // };

    document.getElementById("closeModalBtn").onclick = () => {
      document.getElementById("requestModal").classList.remove("active");
    };
});




function resetRequestModal() {
  document.getElementById("prompt").value = "";
  document.getElementById("lyrics").value = "";
  document.getElementById("errorMsg").textContent = "";

  const submitBtn = document.getElementById("submitBtn");
  submitBtn.disabled = false;
  submitBtn.classList.remove("loading", "success");
  submitBtn.textContent = "Submit";

  document.getElementById("statusTimeline").innerHTML = "";
  document.getElementById("postSubmitActions").classList.add("hidden");
}
