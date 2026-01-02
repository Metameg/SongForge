
const audio = document.getElementById('radioAudio');
const playBtn = document.getElementById('playBtn');
const nowPlaying = document.getElementById('nowPlaying');

let playlist = [];
let currentIndex = 0;

// Fetch playlist from Flask
fetch('/api/audio-list')
  .then(res => res.json())
  .then(files => {
    console.log(files);
    playlist = files.map(
      f => `/static/audios/${f}`
    );
  });

playBtn.addEventListener('click', () => {
  if (!playlist.length) return;
  if (!audio.src) {
    console.log("loading");
  
    loadTrack(currentIndex);
  }
  console.log(audio.src);
  audio.play();
  
});

audio.addEventListener('ended', () => {
  currentIndex = (currentIndex + 1) % playlist.length;
  loadTrack(currentIndex);
  audio.play();
});

function loadTrack(index) {
  audio.src = playlist[index];
  nowPlaying.textContent = playlist[index]
    .split('/')
    .pop();
}
