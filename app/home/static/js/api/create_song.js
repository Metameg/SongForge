
import { socket } from "./socket.js";

const statusTimeline = document.getElementById("statusTimeline");
let submittingStatus;
let completedStatus;

document.getElementById("submitBtn").onclick = async () => {
  const submitBtn = document.getElementById("submitBtn");
  const lyrics = document.getElementById("lyrics").value;
  const prompt = document.getElementById("prompt").value;
  const errorMsg = document.getElementById("errorMsg");
  errorMsg.textContent = ""; // clear previous error
  
  const res = await fetch("/api/my-queue-position");
    const data = await res.json();
    if (data.in_queue) {
      submitBtn.disabled = true;
      document.getElementById("submitError").textContent = "⚠ Only 1 song allowed in queue. Please wait for your song to play before generating a new song request.";
      return
  }

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
      failStatus(submittingStatus, "Something went wrong. Please try again.");
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
      completedStatus = addStatus("Creating song audio. This may take a few minutes.");
      break;

    case "queued":
      completeStatus(completedStatus);
      document.getElementById("postSubmitActions").classList.remove("hidden");
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

