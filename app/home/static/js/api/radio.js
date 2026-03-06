import { socket } from "./socket.js";

export class RadioPlayer {
  constructor({
    audioEl,
    playButton,
    nowPlayingEl,
    queueStatusEl,
    thumbnailEl
  }) {
    this.audio = audioEl;
    this.playBtn = playButton;
    this.nowPlaying = nowPlayingEl;
    this.queueStatusEl = queueStatusEl;
    this.thumbnailEl = thumbnailEl;
    
    // State
    this.cid = null;
    this.userWantsPlaying = false;
    this.userQueueEntry = null;
    this.isTransitioning = false;
    this.waitingForNextTrack = false; // NEW: Waiting for track_changed
    
    this._bindUI();
    this._bindSocket();
    this._bindAudio();
    
  }

  /* ---------------- UI ---------------- */
  _bindUI() {
    this.playBtn.addEventListener("click", () => {
      this.userWantsPlaying = !this.userWantsPlaying;
      this._updatePlayButton();
      
      if (this.userWantsPlaying) {
        this.syncRadio();
      } else {
        this.audio.pause();
      }
    });
  }

  _updatePlayButton() {
    this.playBtn.textContent = this.userWantsPlaying ? "⏸" : "▶";
  }

  _setAlbumCover(url) {
    if (url) {
      this.thumbnailEl.style.backgroundImage = `url("${url}")`;
      this.thumbnailEl.classList.add("has-cover");
    } else {
      this.thumbnailEl.style.backgroundImage = "";
      this.thumbnailEl.classList.remove("has-cover");
    }
  }

  /* ---------------- Socket ---------------- */
  _bindSocket() {
    socket.on("radio_events", (data) => {
      if (data.event === "track_changed") {
        this._onTrackChanged(data);
      }
    });

    socket.on("queue_position_update", (data) => {
      this._onQueuePositionUpdate(data);
    });
  }

  async _onTrackChanged(data) {
    
    // Clear waiting state
    this.waitingForNextTrack = false;
    
    // Mark previous track as played
    if (this.cid && this.cid !== data.cid) {
      await this.markPlayed();
    }
    
    // Update to new track
    this.cid = data.cid;
    await this._loadTrack(data.conversion_path, data.started_at);
    
    // Update UI
    if (this.nowPlaying) {
      this.nowPlaying.textContent = data.title || "Now Playing";
    }
    if (data.source !== "static" && data.album_cover) {
      this._setAlbumCover(data.album_cover);
    } else {
      this._setAlbumCover(null);
    }

    // If this track matches the user's queued song, flip queue widget to "now playing"
    if (data.source !== "static" && this.userQueueEntry?.conversion_id === data.cid) {
      this.userQueueEntry = { ...this.userQueueEntry, now_playing: true, in_queue: false };
      this._renderQueueStatus();
    }
  }

  _onQueuePositionUpdate(data) {
    console.log("queue status update", data);
    this.userQueueEntry = data;
    this._renderQueueStatus();
  }

  // Enhanced queue status rendering
  _renderQueueStatus() {
    if (this.userQueueEntry?.now_playing) {
        this.queueStatusEl.innerHTML = `
            <div class="queue-now-playing">
                <div class="queue-now-playing-icon">🎵</div>
                <div class="queue-now-playing-text">
                    <p class="queue-now-playing-title">Your song is now playing!</p>
                    <p class="queue-now-playing-subtitle">Sit back and enjoy 🎶</p>
                </div>
            </div>
        `;
        document.getElementById("submitBtn").textContent = "Playing...";
        return;
    }

    // if (!this.queueStatusEl) return;
    console.log(this.userQueueEntry);
    if (!this.userQueueEntry || !this.userQueueEntry['in_queue']) {
      this.queueStatusEl.innerHTML = `
        <div class="queue-empty">
          <div class="queue-empty-icon">♪</div>
          <div class="queue-empty-text">
            <p class="queue-empty-title">No songs in queue</p>
            <p class="queue-empty-subtitle">Create a song request to get started</p>
          </div>
        </div>
      `;


      document.getElementById("submitBtn").disabled = false;
      document.getElementById("submitBtn").textContent = "Submit";
      document.getElementById("submitBtn").classList.remove("success");
      document.getElementById("submitError").textContent = "";

      this._resetRequestModal();

      return;
    }
    
    const { queue_position, queue_length } = this.userQueueEntry;
    const percentage = ((queue_length - queue_position + 1) / queue_length) * 100;
    
    this.queueStatusEl.innerHTML = `
      <div class="queue-active">
        <div class="queue-position-badge">
          <span class="queue-number">#${queue_position}</span>
        </div>
        <div class="queue-info">
          <p class="queue-info-title">Your song is in the queue</p>
          <p class="queue-info-detail">Position ${queue_position} of ${queue_length}</p>
          <div class="queue-progress-bar">
            <div class="queue-progress-fill" style="width: ${percentage}%"></div>
          </div>
        </div>
      </div>
    `;
  }

  _resetRequestModal() {
    
    document.getElementById("errorMsg").textContent = "";

    const submitBtn = document.getElementById("submitBtn");
    submitBtn.disabled = false;
    submitBtn.classList.remove("loading", "success");
    submitBtn.textContent = "Submit";

    document.getElementById("statusTimeline").innerHTML = "";
    document.getElementById("postSubmitActions").classList.add("hidden");
  }
    

   async updateQueuePosition() {

    try {
      const res = await fetch("/api/my-queue-position");
      const data = await res.json();
      
      this.userQueueEntry = data;
      this._renderQueueStatus();
    } catch (error) {
      console.error("Error fetching queue position:", error);
    }
  }





  /* ---------------- Audio ---------------- */
  _bindAudio() {
    this.audio.addEventListener("play", () => {
      this.thumbnailEl.classList.add("playing");
    });

    this.audio.addEventListener("pause", () => {
      this.thumbnailEl.classList.remove("playing");
    });

    this.audio.addEventListener("ended", () => {
      
      // Pause and wait for track_changed event
      this.waitingForNextTrack = true;
      this.audio.pause();
      
      // Safety fallback: if track_changed doesn't arrive, try syncing
      setTimeout(() => {
        if (this.waitingForNextTrack && this.userWantsPlaying) {
          this.waitingForNextTrack = false;
          this.syncRadio();
        }
      }, 5000);
    });

    this.audio.addEventListener("error", (e) => {
      console.error("Audio error:", e);
      // Retry syncing after a short delay
      setTimeout(() => {
        if (this.userWantsPlaying) {
          this.syncRadio();
        }
      }, 1000);
    });

    // Handle cases where browser blocks autoplay
    this.audio.addEventListener("pause", (e) => {
      if (this.userWantsPlaying && !this.isTransitioning && !this.waitingForNextTrack) {
        this.audio.play().catch(err => {
        });
      }
    });
  }

  /* ---------------- Track Loading ---------------- */
  async _loadTrack(conversionPath, startedAt) {
    this.isTransitioning = true;
    
    try {
      // Calculate offset
      const offset = Math.max(0, (Date.now() - startedAt) / 1000);
      
      // Load new track
      this.audio.src = conversionPath;
      this.audio.currentTime = offset;
      
      // Play if user wants it playing
      if (this.userWantsPlaying) {
        await this.audio.play();
      }
    } catch (error) {
      console.error("Error loading track:", error);
    } finally {
      this.isTransitioning = false;
    }
  }

  /* ---------------- API ---------------- */
  async syncRadio() {
    
    try {
      const res = await fetch("/api/radio/now-playing");
      const data = await res.json();
      
      if (!data.playing) {
        this.audio.pause();
        return;
      }
      
      // Only update if it's a different track or we're not playing
      const isDifferentTrack = this.cid !== data.cid;
      const needsSync = isDifferentTrack || !this.audio.src;
      
      if (needsSync) {
        this.cid = data.cid;
        await this._loadTrack(data.conversion_path, data.started_at);

        if (this.nowPlaying) {
          this.nowPlaying.textContent = data.title || "Now Playing";
        }
        if (data.source !== "static" && data.album_cover) {
          this._setAlbumCover(data.album_cover);
        } else {
          this._setAlbumCover(null);
        }
      } else if (this.userWantsPlaying && this.audio.paused && !this.waitingForNextTrack) {
        // Just resume if we have the right track but it's paused
        await this.audio.play();
      }
    } catch (error) {
      console.error("Error syncing radio:", error);
    }
  }

  async markPlayed() {
    if (!this.cid) return;
    
    try {
      await fetch("/api/mark-played", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ cid: this.cid }),
      });
    } catch (error) {
      console.error("Error marking track as played:", error);
    }
  }
}



export let radioPlayerInstance = null;

export function initializeRadioPlayer(options) {
  radioPlayerInstance = new RadioPlayer(options);
  return radioPlayerInstance;
}


