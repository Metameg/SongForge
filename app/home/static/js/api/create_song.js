
import { socket } from "./socket.js";

const statusTimeline = document.getElementById("statusTimeline");
let submittingStatus;
let completedStatus;
let finishedStatus;

document.getElementById("submitBtn").onclick = async () => {
  const submitBtn = document.getElementById("submitBtn");
  const lyrics = document.getElementById("lyrics").value;
  const prompt = document.getElementById("prompt").value;
  const errorMsg = document.getElementById("errorMsg");
  errorMsg.textContent = ""; // clear previous error
  
  if (!prompt) {
    errorMsg.textContent = "Prompt is required.";
    return;
  }

  // 🔒 Lock button + show loading
  submitBtn.disabled = true;
  submitBtn.classList.add("loading");
  submitBtn.textContent = "Submitting…";
  submittingStatus = addStatus("Submitting song...");

  try {
    const res = await fetch("/api/create-song", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ lyrics, prompt }),
    });

    const data = await res.json();

    if (!res.ok) {
      errorMsg.textContent = data.message || "Something went wrong.";
      failStatus(submittingStatus, data.message || "Something went wrong. Please try again.");
      submitBtn.disabled = false;
      submitBtn.classList.remove("loading");
      submitBtn.textContent = "Submit";
      return;
    }

    
    // ✅ Success state
    submitBtn.classList.remove("loading");
    submitBtn.classList.add("success");
    submitBtn.textContent = "✓ Submitted";
 

  } catch (err) {
    console.error(err);

    // ❌ Error state (re-enable)
    errorMsg.textContent = err.message || "Network error.";
    submitBtn.disabled = false;
    submitBtn.classList.remove("loading");
    submitBtn.textContent = "Submit";
  }
};


socket.on("job_status_update", (payload) => {
  const { status, message } = payload;

  switch (status) {
    case "processing":
      completeStatus(submittingStatus);
      completedStatus = addStatus("Creating song audio. This may take a few minutes…");
      break;

    case "queued":
      completeStatus(completedStatus);
      document.getElementById("postSubmitActions").classList.remove("hidden");
      finishedStatus = addStatus("Song created and added to the queue!");
      completeStatus(finishedStatus);
      break;

    case "played":
      // Song finished playing — reset form so user can create another
      document.getElementById("statusTimeline").innerHTML = "";
      document.getElementById("postSubmitActions").classList.add("hidden");
      document.getElementById("submitError").textContent = "";
      document.getElementById("errorMsg").textContent = "";
      const submitBtn = document.getElementById("submitBtn");
      submitBtn.disabled = false;
      submitBtn.classList.remove("loading", "success");
      submitBtn.textContent = "Submit";
      break;
  }
});


function addStatus(message) {
  const item = document.createElement("div");
  item.className = "status-item";

  item.innerHTML = `
    <div class="status-icon">
      <div class="spinner"></div>
    </div>
    <div class="status-text">${message}</div>
  `;

  statusTimeline.appendChild(item);
  return item;
}

function completeStatus(item) {
  item.classList.add("completed");
  item.querySelector(".status-icon").innerHTML =
    `<div class="checkmark">✓</div>`;
}


function failStatus(item, message) {
  item.classList.add("failed");

  if (message) {
    const text = item.querySelector(".status-text");
    if (text) text.textContent = message;
  }

  item.querySelector(".status-icon").innerHTML =
    `<div class="error-mark">✕</div>`;
}

