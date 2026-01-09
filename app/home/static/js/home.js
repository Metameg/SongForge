
document.addEventListener("DOMContentLoaded", () => {
    const requestBtn = document.getElementById("requestBtn");    const modal = document.getElementById("requestModal");
    const playBtn = document.getElementById("playBtn");
    const lyricsBtn = document.querySelector(".lyrics-btn");
    const wrapper = document.querySelector(".request-box-wrapper");

    requestBtn.addEventListener("click", () => {
      resetRequestModal();
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




    document.getElementById("newRequestBtn").onclick = () => {
      resetRequestModal();
    };

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
