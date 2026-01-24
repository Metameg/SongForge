import { socket } from "./socket.js";

const audio = document.getElementById('radioAudio');
const playBtn = document.getElementById('playBtn');
const nowPlaying = document.getElementById('nowPlaying');

let cid = "";



async function syncRadio() {
  const res = await fetch("/api/radio/now-playing");
  const data = await res.json();
  if (!data.playing) return;

  audio.src = data.conversion_path;

  const now = Date.now();
  const offset = (now - data.started_at) / 1000;

  audio.currentTime = Math.max(0, offset);
  audio.play();
}


playBtn.addEventListener('click', () => {
  syncRadio();
  
});


socket.on("radio_events", data => {
  if (data.event === "track_changed") {
    audio.src = data.conversion_path;
    cid = data.cid;
    const now = Date.now();
    const offset = (now - data.started_at) / 1000;

    audio.currentTime = Math.max(0, offset);
    audio.play();
  }
});



audio.addEventListener("ended", async () => { 
  syncRadio(); 
  markPlayed(audio);
});

async function markPlayed() {
  await fetch("/api/mark-played", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      cid
    }),
  });

}
