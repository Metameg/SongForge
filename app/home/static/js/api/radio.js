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
    this._queueFetchSeq = 0; // sequence counter to discard stale updateQueuePosition results
    
    this._bindUI();
    this._bindSocket();
    this._bindAudio();
    this.updateQueuePosition();
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

  _setNowPlayingTitle(title, source) {
    if (!this.nowPlaying) return;
    if (source === "static") {
      this.nowPlaying.textContent = "Curated Selection";
      this.nowPlaying.className = "source-static";
    } else {
      this.nowPlaying.textContent = title || "Now Playing";
      this.nowPlaying.className = "source-dynamic";
    }
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

    socket.on("queue_changed", (data) => {
      if (data?.queue_length !== undefined) this._updateQueueCount(data.queue_length);
      this.updateQueuePosition();
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
    this._setNowPlayingTitle(data.title, data.source);
    if (data.source !== "static" && data.album_cover) {
      this._setAlbumCover(data.album_cover);
    } else {
      this._setAlbumCover(null);
    }

    // If this track matches the user's queued song, flip queue widget immediately (fast path).
    // Also always fetch a server-confirmed queue state for dynamic tracks so both the
    // now-playing client and queued clients update reliably, regardless of whether
    // userQueueEntry.conversion_id was populated.
    if (data.source !== "static") {
      if (this.userQueueEntry?.conversion_id === data.cid) {
        this.userQueueEntry = { ...this.userQueueEntry, now_playing: true, in_queue: false };
        this._renderQueueStatus();
      }
      this.updateQueuePosition();
    }
  }

  _onQueuePositionUpdate(data) {
    console.log("queue status update", data);
    this.userQueueEntry = data;
    this._renderQueueStatus();
    if (data.queue_length !== undefined) this._updateQueueCount(data.queue_length);
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
        const submitBtn = document.getElementById("submitBtn");
        submitBtn.disabled = true;
        submitBtn.textContent = "Playing...";
        submitBtn.classList.remove("loading", "success");
        document.getElementById("submitError").textContent = "Your song is currently playing. You can create another once it finishes.";
        return;
    }

    // if (!this.queueStatusEl) return;
    console.log(this.userQueueEntry);
    if (!this.userQueueEntry || !this.userQueueEntry['in_queue']) {
      if (this.userQueueEntry?.has_active_job) {
        // Song is still being created — don't reset the UI
        this.queueStatusEl.innerHTML = `
          <div class="queue-empty">
            <div class="queue-empty-icon">♪</div>
            <div class="queue-empty-text">
              <p class="queue-empty-title">Song is being created…</p>
              <p class="queue-empty-subtitle">Your song will appear in the queue shortly</p>
            </div>
          </div>
        `;
        return;
      }

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
    
    const { queue_position } = this.userQueueEntry;

    this.queueStatusEl.innerHTML = `
      <div class="queue-active">
        <div class="queue-position-badge">
          <span class="queue-number">#${queue_position}</span>
        </div>
        <div class="queue-info">
          <p class="queue-info-title">Your song is in the queue</p>
          <p class="queue-info-detail">Position #${queue_position}</p>
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
    const seq = ++this._queueFetchSeq;
    try {
      const res = await fetch("/api/my-queue-position");
      const data = await res.json();

      // Discard if a newer fetch has already completed
      if (seq !== this._queueFetchSeq) return;

      this.userQueueEntry = data;
      this._renderQueueStatus();
      if (data.queue_length !== undefined) this._updateQueueCount(data.queue_length);
    } catch (error) {
      console.error("Error fetching queue position:", error);
    }
  }

  _updateQueueCount(count) {
    const el = document.getElementById("queueCount");
    if (!el) return;
    el.textContent = count;
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
      this.audio.src = conversionPath;

      // Wait for metadata before seeking — setting currentTime before this is silently ignored
      await new Promise((resolve, reject) => {
        this.audio.addEventListener("loadedmetadata", resolve, { once: true });
        this.audio.addEventListener("error", reject, { once: true });
      });

      // Recalculate offset after metadata loads since time has passed
      const offset = Math.max(0, (Date.now() - startedAt) / 1000);
      this.audio.currentTime = offset;

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

        this._setNowPlayingTitle(data.title, data.source);
        if (data.source !== "static" && data.album_cover) {
          this._setAlbumCover(data.album_cover);
        } else {
          this._setAlbumCover(null);
        }
      } else if (this.userWantsPlaying && this.audio.paused && !this.waitingForNextTrack) {
        // Re-sync to current global position before resuming — the audio may have
        // been seeked to a stale position from when track_changed first fired
        const offset = Math.max(0, (Date.now() - data.started_at) / 1000);
        this.audio.currentTime = offset;
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


