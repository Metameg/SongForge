
import { socket } from "./socket.js";

export class RadioPlayer {
  constructor({
    audioEl,
    playButton,
    nowPlayingEl,
  }) {
    this.audio = audioEl;
    this.playBtn = playButton;
    this.nowPlaying = nowPlayingEl;

    // 🔁 state
    this.cid = null;
    this.isPlaying = false;
    this._bindUI();
    this._bindSocket();
    this._bindAudio();
  }

  /* ---------------- UI ---------------- */

  _bindUI() {
    this.playBtn.addEventListener("click", () => {
      this.isPlaying = !this.isPlaying;
      this.syncRadio();
      this._updatePlayButton();
    });
  }

  _updatePlayButton() {
    this.playBtn.textContent = this.isPlaying ? "⏸" : "▶";
  }
  /* ---------------- Socket ---------------- */

  _bindSocket() {
    socket.on("radio_events", (data) => {
      if (data.event === "track_changed") {
        this._onTrackChanged(data);
      }
    });
  }

  _onTrackChanged(data) {
    this.cid = data.cid;

    this.audio.src = data.conversion_path;

    const offset = (Date.now() - data.started_at) / 1000;
    this.audio.currentTime = Math.max(0, offset);

    this.audio.play();

    if (this.nowPlaying) {
      this.nowPlaying.textContent = data.cid;
    }
  }

  /* ---------------- Audio ---------------- */

  _bindAudio() {
    this.audio.addEventListener("ended", async () => {
      await this.markPlayed();
      await this.syncRadio();
    });

    this.audio.addEventListener("play", () => {
      this.isPlaying = true;
    });

    this.audio.addEventListener("pause", () => {
      this.isPlaying = false;
    });
  }

  /* ---------------- API ---------------- */

  async syncRadio() {
    const res = await fetch("/api/radio/now-playing");
    const data = await res.json();
    if (!this.isPlaying) {
      this.audio.pause();
      return
    }
    if (!data.playing) return;

    this.cid = data.cid;

    this.audio.src = data.conversion_path;

    const offset = (Date.now() - data.started_at) / 1000;
    this.audio.currentTime = Math.max(0, offset);

    this.audio.play();
  }

  async markPlayed() {
    if (!this.cid) return;

    await fetch("/api/mark-played", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ cid: this.cid }),
    });
  }
}
