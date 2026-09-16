"use client";

/**
 * Minimal static-radio player (issue #8, criteria #4, #5).
 *
 * Loading the page fetches the current pointer and shows a Play button — browsers block
 * autoplay, so the button press is the anchor point: it computes clock skew + the elapsed
 * offset into the current song via `frontend/lib/sync.ts` and seeks `<audio>` there before
 * calling `.play()` (criterion #4).
 *
 * Without SSE (a later issue — see `.orchestrator/CONTEXT.md` DEFERRED list), the song
 * change at a boundary (criterion #5) is driven by two triggers that both re-fetch
 * `/now-playing` and re-anchor when `playback_id` changes: a `setTimeout` scheduled to the
 * pointer's `ends_at`, and the `<audio>` element's `ended` event (PRD story #11 — playback
 * reaching the end of the current track with no next-song anchor yet).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchNowPlaying, type NowPlaying, type NowPlayingState } from "../lib/nowPlaying";
import { computeOffsetSeconds, computeSkewMs, correctedServerNowMs } from "../lib/sync";

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";

/** Seek `audio` to where the server timeline says this song should be right now. */
function seekToLiveOffset(audio: HTMLAudioElement, playing: NowPlaying): void {
  const clientReceiptMs = Date.now();
  const skewMs = computeSkewMs(Date.parse(playing.server_time), clientReceiptMs);
  const serverNowMs = correctedServerNowMs(Date.now(), skewMs);
  const offsetSeconds = computeOffsetSeconds(serverNowMs, Date.parse(playing.started_at));
  audio.currentTime = Math.max(offsetSeconds, 0);
}

export default function Player() {
  const [state, setState] = useState<NowPlayingState | null>(null);
  const [hasStarted, setHasStarted] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const boundaryTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refresh = useCallback(async () => {
    const next = await fetchNowPlaying(BACKEND_URL);
    setState((previous) => {
      const previousPlaybackId = previous && previous.status === "playing" ? previous.playback_id : null;
      const nextPlaybackId = next.status === "playing" ? next.playback_id : null;
      // A new song started (playback_id changed) while we were already playing —
      // re-anchor without waiting for another button press (criterion #5).
      if (hasStarted && next.status === "playing" && nextPlaybackId !== previousPlaybackId) {
        const audio = audioRef.current;
        if (audio) {
          seekToLiveOffset(audio, next);
          void audio.play();
        }
      }
      return next;
    });
    return next;
  }, [hasStarted]);

  const scheduleBoundaryRefresh = useCallback(
    (playing: NowPlaying) => {
      if (boundaryTimer.current) clearTimeout(boundaryTimer.current);
      const delayMs = Math.max(Date.parse(playing.ends_at) - Date.now(), 0);
      boundaryTimer.current = setTimeout(() => {
        void refresh().then((next) => {
          if (next.status === "playing") scheduleBoundaryRefresh(next);
        });
      }, delayMs);
    },
    [refresh],
  );

  useEffect(() => {
    void refresh().then((next) => {
      if (next.status === "playing") scheduleBoundaryRefresh(next);
    });
    return () => {
      if (boundaryTimer.current) clearTimeout(boundaryTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handlePlay = () => {
    const audio = audioRef.current;
    if (!audio || !state || state.status !== "playing") return;
    seekToLiveOffset(audio, state);
    void audio.play();
    setHasStarted(true);
  };

  const handleEnded = () => {
    // The current track finished with no next-song anchor yet — re-fetch immediately
    // (PRD story #11) rather than waiting for the scheduled boundary timer.
    void refresh().then((next) => {
      if (next.status === "playing") scheduleBoundaryRefresh(next);
    });
  };

  const isPlaying = state?.status === "playing";

  return (
    <main
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: "1rem",
        padding: "1rem",
      }}
    >
      <h1 style={{ margin: 0, fontSize: "2rem" }}>SongForge</h1>
      <p style={{ opacity: 0.7, margin: 0 }}>
        {isPlaying ? (state as NowPlaying).title : "The station is quiet right now."}
      </p>
      <button
        onClick={handlePlay}
        disabled={!isPlaying}
        style={{
          padding: "0.75rem 2rem",
          fontSize: "1rem",
          borderRadius: "999px",
          border: "none",
          cursor: isPlaying ? "pointer" : "not-allowed",
          background: "#f2f2f2",
          color: "#0b0b0f",
          opacity: isPlaying ? 1 : 0.5,
        }}
      >
        Play
      </button>
      <audio
        ref={audioRef}
        src={isPlaying ? (state as NowPlaying).audio_url : undefined}
        onEnded={handleEnded}
      />
    </main>
  );
}
