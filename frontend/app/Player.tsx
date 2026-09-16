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
 * change at a boundary (criterion #5) is driven by a self-rescheduling poll of
 * `/now-playing` (armed to each pointer's `ends_at`) plus the `<audio>` element's `ended`
 * event (PRD story #11 — playback reaching the end of the current track with no next-song
 * anchor yet). When the pointer's `playback_id` changes, a `useEffect` re-anchors the
 * (by-then freshly-`src`'d) audio element to the live offset — see the effect below for
 * why re-anchoring must happen after React commits the new `src`, not inside a setState
 * updater.
 */

import { useEffect, useRef, useState } from "react";
import { fetchNowPlaying, type NowPlaying, type NowPlayingState } from "../lib/nowPlaying";
import { computeOffsetSeconds, computeSkewMs, correctedServerNowMs } from "../lib/sync";

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";

// A stale/past `ends_at` (or an idle station) must not spin a per-client `setTimeout(0)`
// tight refetch loop against `/now-playing` — floor every reschedule to this.
const MIN_REFETCH_DELAY_MS = 1000;
// After a transient fetch failure (or while idle), retry on this cadence so one failed
// poll never permanently stops the song-change updates.
const RETRY_DELAY_MS = 5000;

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
  // Set by the poll effect; lets the `<audio>` `ended` handler force an immediate
  // re-fetch (PRD story #11) without waiting for the scheduled boundary tick.
  const pollNowRef = useRef<() => void>(() => {});

  // Poll `/now-playing` and reschedule to the next boundary. Resilient (a transient
  // failure retries rather than killing the reschedule chain) and floored (a stale
  // `ends_at` can't tight-loop). Owns its own timer + cancellation.
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const schedule = (delayMs: number): void => {
      if (cancelled) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(poll, Math.max(delayMs, MIN_REFETCH_DELAY_MS));
    };

    const poll = (): void => {
      fetchNowPlaying(BACKEND_URL)
        .then((next) => {
          if (cancelled) return;
          setState(next);
          schedule(
            next.status === "playing"
              ? Date.parse(next.ends_at) - Date.now()
              : RETRY_DELAY_MS,
          );
        })
        .catch(() => {
          // Network blip / non-JSON body — keep the reschedule chain alive.
          schedule(RETRY_DELAY_MS);
        });
    };

    pollNowRef.current = poll;
    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      pollNowRef.current = () => {};
    };
  }, []);

  // Re-anchor on song change: once the listener has pressed Play, every new pointer
  // (`playback_id` change) seeks the freshly-loaded `<audio>` to the live server-timeline
  // offset and resumes. Keyed on `playback_id` so it runs AFTER React commits the new
  // `src` — changing an `<audio>` element's `src` reloads it and resets `currentTime` to
  // 0, so a seek performed before that commit is silently discarded (criterion #5). The
  // FIRST play stays in the click handler: browsers only allow `play()` from the user
  // gesture, and the element is unlocked for subsequent programmatic seeks once that
  // gesture has played it.
  const playbackId = state?.status === "playing" ? state.playback_id : null;
  useEffect(() => {
    if (!hasStarted || playbackId === null) return;
    const audio = audioRef.current;
    if (!audio || !state || state.status !== "playing") return;
    seekToLiveOffset(audio, state);
    void audio.play();
    // Fires on playback_id / hasStarted change; `state` is read fresh from that render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playbackId, hasStarted]);

  const handlePlay = () => {
    const audio = audioRef.current;
    if (!audio || !state || state.status !== "playing") return;
    seekToLiveOffset(audio, state);
    void audio.play();
    setHasStarted(true);
  };

  const handleEnded = () => {
    // The current track finished with no next-song anchor yet — re-fetch immediately
    // (PRD story #11) rather than waiting for the scheduled boundary tick.
    pollNowRef.current();
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
