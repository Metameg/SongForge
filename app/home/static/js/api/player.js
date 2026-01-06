import { socket } from "./socket.js";

const audio = document.getElementById('radioAudio');
const playBtn = document.getElementById('playBtn');
const nowPlaying = document.getElementById('nowPlaying');

let playlist = [];
let currentIndex = 0;


// Fetch playlist from Flask
fetch('/api/audio-list')
  .then(res => res.json())
  .then(files => {
    playlist = files.map(
      f => `/static/audios/${f}`
    );
  });

playBtn.addEventListener('click', () => {
  if (!playlist.length) return;
  if (!audio.src) {
  
    loadTrack(currentIndex);
  }
  audio.play();
  
});


function loadTrack(index) {
  audio.src = playlist[index];
  nowPlaying.textContent = playlist[index]
    .split('/')
    .pop();
}



socket.on("new_audio", data => {
  console.log("new audio received");
  const insertIndex = audio.src ? currentIndex + 1 : 0;
  playlist.splice(insertIndex, 0, data.conversion_path);

  if (!audio.src) {
    loadTrack(0);
  }
});

// async function pollForNewAudio() {
//   try {
//     const res = await fetch("/api/next-audio");
//     const data = await res.json();
//
//     if (!data.conversion_path) return;
//
//     console.log("New song:", data.conversion_path);
//
//     // CASE 1: nothing has played yet
//     if (!audio.src) {
//       playlist.splice(0, 0, data.conversion_path);
//       currentIndex = 0;
//
//       loadTrack(currentIndex);
//       return;
//     }
//
//     // CASE 2: something is playing → play next
//     const insertIndex = currentIndex + 1;
//     playlist.splice(insertIndex, 0, data.conversion_path);
//
//
//   } catch (err) {
//     console.error("Polling error:", err);
//   }
// }
//
// // poll every 3 seconds
// setInterval(pollForNewAudio, 3000);




audio.addEventListener("ended", async () => {
  const src = audio.src;
  currentIndex++;

  loadTrack(currentIndex);
  audio.play();

  // notify backend
  fetch("/api/mark-played", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ conversion_path: src }),
  });
});
