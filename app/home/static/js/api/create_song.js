
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

  const creatingStatus = addStatus(
    "Creating song. This may take a moment. Do not refresh your browser."
  );

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
      return;
    }

    // ✅ Mark first step complete
    completeStatus(creatingStatus);

    // 🟢 Status: Added to queue
    const queuedStatus = addStatus(
      "Song created successfully. It has been added to the queue."
    );
    completeStatus(queuedStatus);

    // ✅ Success state
    submitBtn.classList.remove("loading");
    submitBtn.classList.add("success");
    submitBtn.textContent = "✓ Submitted";

    document.getElementById("postSubmitActions").classList.remove("hidden");

  } catch (err) {
    console.error(err);

    // ❌ Error state (re-enable)
    errorMsg.textContent = err.message || "Network error.";
    submitBtn.disabled = false;
    submitBtn.classList.remove("loading");
    submitBtn.textContent = "Submit";
  }
};



const statusTimeline = document.getElementById("statusTimeline");

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
