import { socket } from "./socket.js";

export class RadioPlayer {
  constructor({
    audioEl,
    playButton,
    nowPlayingEl,
    queueStatusEl
  }) {
    this.audio = audioEl;
    this.playBtn = playButton;
    this.nowPlaying = nowPlayingEl;
    this.queueStatusEl = queueStatusEl;
    
    // State
    this.cid = null;
    this.userWantsPlaying = false;
    this.userQueueEntry = null;
    this.isTransitioning = false;
    this.waitingForNextTrack = false; // NEW: Waiting for track_changed
    
    this._bindUI();
    this._bindSocket();
    this._bindAudio();
    
    console.log("RadioPlayer initialized");
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
    console.log("Track changed:", data.cid);
    
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
      this.nowPlaying.textContent = data.cid;
    }
  }

  _onQueuePositionUpdate(data) {
    console.log("Queue position updated:", data);
    this.userQueueEntry = data;
    this._renderQueueStatus();
  }

  _renderQueueStatus() {
    if (!this.queueStatusEl) return;
    
    if (!this.userQueueEntry) {
      this.queueStatusEl.textContent =
        "You currently have no songs in queue. Create a song request now to place song in queue.";
      return;
    }
    
    const { queue_position, queue_length } = this.userQueueEntry;
    this.queueStatusEl.textContent =
      `Your song is #${queue_position} out of ${queue_length} in the queue.`;
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
    this.audio.addEventListener("ended", () => {
      console.log("Track ended - pausing until next track arrives");
      
      // Pause and wait for track_changed event
      this.waitingForNextTrack = true;
      this.audio.pause();
      
      // Safety fallback: if track_changed doesn't arrive, try syncing
      setTimeout(() => {
        if (this.waitingForNextTrack && this.userWantsPlaying) {
          console.log("No track_changed received, forcing sync");
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
      // Only attempt resume if:
      // 1. User wants to be playing
      // 2. We're not transitioning
      // 3. We're not waiting for the next track
      if (this.userWantsPlaying && !this.isTransitioning && !this.waitingForNextTrack) {
        console.log("Unexpected pause - attempting resume");
        this.audio.play().catch(err => {
          console.log("Autoplay blocked:", err.message);
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
    console.log("Syncing radio...");
    
    try {
      const res = await fetch("/api/radio/now-playing");
      const data = await res.json();
      
      if (!data.playing) {
        console.log("No track currently playing");
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
          this.nowPlaying.textContent = data.cid;
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
      console.log("Marked as played:", this.cid);
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


