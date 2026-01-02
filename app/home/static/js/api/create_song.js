
document.getElementById("submitBtn").onclick = async () => {
  const lyrics = document.getElementById("lyrics").value;
  const prompt = document.getElementById("prompt").value;
  const errorMsg = document.getElementById("errorMsg");

  errorMsg.textContent = ""; // clear previous error

  if (!prompt) {
    errorMsg.textContent = "Prompt is required.";
    return;
  }

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

    console.log("Song created:", data);

  } catch (err) {
    errorMsg.textContent = "Network error. Please try again.";
    console.error(err);
  }
};
;
