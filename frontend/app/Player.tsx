"use client";

/**
 * Minimal static-radio player (issue #8, criteria #4, #5; issue #10, criteria #1-#4).
 *
 * Loading the page opens an SSE connection (`new EventSource("/events")`) and shows a
 * Play button once the current pointer arrives — browsers block autoplay, so the button
 * press is the anchor point: it computes clock skew + the elapsed offset into the
 * current song via `frontend/lib/sync.ts` and seeks `<audio>` there before calling
 * `.play()` (criterion #4).
 *
 * Issue #10 replaces the old self-rescheduling `/now-playing` poll with server push: the
 * backend's `/events` endpoint sends the current pointer immediately on connect
 * (sync-on-arrival) and again on every genuine song change (a Redis pub/sub-fed push,
 * arriving the instant the station advances — no up-to-1s poll lag) or ~30s heartbeat
 * (a dropped push self-corrects). `shouldReanchorOnPointer` (`lib/sync.ts`) tells the
 * handler whether an incoming frame is a real song change (`playback_id` differs) or
 * just a heartbeat re-send of the same one — re-anchoring on every frame would audibly
 * restart playback each heartbeat. The `<audio>` `ended` event (PRD story #11 —
 * playback reaching the end of the current track with no next-song push having arrived
 * yet) still triggers an immediate `/now-playing` re-fetch as a safeguard.
 *
 * Gapless transitions (criterion #3) use two `<audio preload="auto">` buffers
 * (`audioARef`/`audioBRef`): a real song change loads the *inactive* buffer with the
 * new track, seeks + plays it, pauses the previously-active one, then flips which
 * buffer is "active" (`nextPreloadSlot`, `lib/sync.ts`) — avoiding the single-element
 * `src`-reassignment reset/jank a listener would otherwise hear at every boundary. This
 * does not (and, per the static-radio pointer model, cannot) eliminate the new track's
 * own fetch+decode latency — there is no next-song id known ahead of the boundary to
 * pre-warm a buffer with; see `.orchestrator/CONTEXT.md`'s explicit scope note.
 *
 * Issue #9 (design D6) continuous-sync gap: the `<audio>` `timeupdate` event (which
 * fires several times a second during playback) runs a free local drift check (deadband
 * < 1s, hard-seek ≥ 1s via `decideDrift`) against the server timeline. Clock skew is
 * measured at each pointer's *receipt* and kept in a ref, so the per-tick drift math
 * uses a skew sampled at a known moment rather than recomputing it from an increasingly
 * stale `server_time`. The client-side heartbeat re-fetch timer from issue #9 is
 * removed — the server now drives re-sync via the ~30s SSE heartbeat re-emit.
 *
 * Issue #14, criterion #3 (interrupt crossfade): a fresh user song can arrive as an
 * ordinary `song-change` push mid-static-song (an interrupt) rather than at the
 * natural boundary. No wire-contract change (design D2) — `applyPlaying` infers an
 * interrupt purely from data it already has: `prevPointerRef` (the outgoing pointer)
 * plus the usual clock-skew math tells it whether the outgoing song still had
 * meaningful time left (`isInterruptArrival`, `lib/sync.ts`). An interrupt
 * volume-ramps both buffers over `CROSSFADE_DURATION_MS` (`rampVolume` below) instead
 * of the ordinary hard pause/play swap; an ordinary boundary (the outgoing song
 * actually finished) or the `ended`-safeguard path both naturally read as "not an
 * interrupt" since the outgoing song's remaining time is ~0 by then.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchNowPlaying, type NowPlaying, type NowPlayingState } from "../lib/nowPlaying";
import { fetchQuota, formatSongsLeft, type QuotaResponse } from "../lib/quota";
import {
  computeExpectedOffsetSeconds,
  computeOffsetSeconds,
  computeSkewMs,
  correctedServerNowMs,
  crossfadeGains,
  CROSSFADE_DURATION_MS,
  decideDrift,
  isInterruptArrival,
  nextPreloadSlot,
  shouldReanchorOnPointer,
  type PreloadSlot,
} from "../lib/sync";

// Same-origin base: the browser fetches "/now-playing" on its own origin and the Next
// server proxies it to the backend. This avoids CORS and keeps the internal backend
// hostname out of the browser. `/events` (the SSE stream) is reached the same way.
const NOW_PLAYING_BASE = "";

/** Seek `audio` to where the server timeline says this song should be right now. */
function seekToLiveOffset(audio: HTMLAudioElement, playing: NowPlaying): void {
  const clientReceiptMs = Date.now();
  const skewMs = computeSkewMs(Date.parse(playing.server_time), clientReceiptMs);
  const serverNowMs = correctedServerNowMs(Date.now(), skewMs);
  const offsetSeconds = computeOffsetSeconds(serverNowMs, Date.parse(playing.started_at));
  audio.currentTime = Math.max(offsetSeconds, 0);
}

/**
 * Resolve the "live" (or "preloading") `<audio>` element from the two buffer refs and
 * the current slot. A module-level helper (rather than a component-scoped closure) so
 * it carries no per-render identity of its own and needs no `useCallback` dependency
 * bookkeeping at any of its call sites.
 */
function bufferElement(
  slot: PreloadSlot,
  audioARef: React.RefObject<HTMLAudioElement | null>,
  audioBRef: React.RefObject<HTMLAudioElement | null>,
): HTMLAudioElement | null {
  return (slot === "a" ? audioARef : audioBRef).current;
}

/**
 * Volume-ramp `audio` over `durationMs` using {@link crossfadeGains} (issue #14,
 * criterion #3's interrupt crossfade). DOM-side (touches `<audio>.volume` +
 * `requestAnimationFrame`), so it lives here rather than in the pure `lib/sync.ts`
 * seam, which only supplies the gain-at-elapsed-time math.
 */
function rampVolume(
  audio: HTMLAudioElement,
  buffer: "outgoing" | "incoming",
  durationMs: number,
  onDone?: () => void,
): void {
  const startMs = performance.now();
  const step = (nowMs: number) => {
    const elapsedMs = nowMs - startMs;
    const { outgoingGain, incomingGain } = crossfadeGains(elapsedMs, durationMs);
    audio.volume = buffer === "outgoing" ? outgoingGain : incomingGain;
    if (elapsedMs < durationMs) {
      requestAnimationFrame(step);
    } else {
      onDone?.();
    }
  };
  requestAnimationFrame(step);
}

export default function Player() {
  const [state, setState] = useState<NowPlayingState | null>(null);
  const [hasStarted, setHasStarted] = useState(false);
  // Issue #15, criterion #5: remaining daily "songs left", read once on mount from the
  // same-origin `/quota` proxy. Null until it loads (the indicator stays hidden), so a
  // slow/unreachable backend never blocks the player.
  const [quota, setQuota] = useState<QuotaResponse | null>(null);
  const audioARef = useRef<HTMLAudioElement | null>(null);
  const audioBRef = useRef<HTMLAudioElement | null>(null);
  // Which buffer is currently "live" (the other is preloading/idle). A ref, not state:
  // nothing in the render depends on it (both `<audio>` elements are rendered
  // unconditionally; their `src` is set imperatively), so it need not trigger a re-render.
  const activeBufferRef = useRef<PreloadSlot>("a");
  // Mirrors `hasStarted` for use inside `applyPlaying`, which is created once (stable
  // callback identity) and so cannot close over a fresh `hasStarted` from each render.
  const hasStartedRef = useRef(false);
  // The last pointer's `playback_id` applied to the buffers — lets `shouldReanchorOnPointer`
  // tell a genuine song change apart from a heartbeat re-send of the same one.
  const prevPlaybackIdRef = useRef<string | null>(null);
  // The last "playing" pointer itself (issue #14, criterion #3) — `applyPlaying` reads
  // this to tell whether the OUTGOING song still had meaningful time left when the new
  // pointer arrived (an interrupt) vs. having actually finished (an ordinary boundary).
  const prevPointerRef = useRef<NowPlaying | null>(null);
  // Clock skew (client − server, ms) sampled at each pointer's receipt. The `timeupdate`
  // drift check reads this rather than recomputing it from `server_time`, which grows
  // stale between events (issue #9, design D6).
  const skewMsRef = useRef<number>(0);

  // Apply an incoming "playing" pointer, from either the SSE stream or the `ended`
  // safeguard's direct fetch. Always updates `state` + resamples clock skew; only
  // touches the `<audio>` buffers when `shouldReanchorOnPointer` says this is a real
  // song change (criterion #3) rather than a heartbeat re-send of the current one.
  // Stable identity (only refs + setState in its closure) so the mount-once SSE effect
  // below can depend on it safely.
  const applyPlaying = useCallback((next: NowPlaying) => {
    const isChange = shouldReanchorOnPointer(prevPlaybackIdRef.current, next.playback_id);
    const outgoingPointer = prevPointerRef.current;
    prevPlaybackIdRef.current = next.playback_id;
    prevPointerRef.current = next;
    skewMsRef.current = computeSkewMs(Date.parse(next.server_time), Date.now());
    setState(next);
    if (!isChange) return;

    // Issue #14, criterion #3: crossfade an interrupt instead of hard-cutting. Inferred
    // from the OUTGOING pointer's own remaining time on the corrected server clock — a
    // genuine boundary (or the `ended` safeguard, which only fires once the outgoing
    // song has actually played out) has ~0 remaining and reads as "cut" for free.
    let transition: "crossfade" | "cut" = "cut";
    if (outgoingPointer && hasStartedRef.current) {
      const serverNowMs = correctedServerNowMs(Date.now(), skewMsRef.current);
      const offsetIntoOutgoingSeconds = computeOffsetSeconds(
        serverNowMs,
        Date.parse(outgoingPointer.started_at),
      );
      const outgoingDurationSeconds =
        outgoingPointer.duration ??
        (Date.parse(outgoingPointer.ends_at) - Date.parse(outgoingPointer.started_at)) / 1000;
      if (isInterruptArrival(offsetIntoOutgoingSeconds, outgoingDurationSeconds)) {
        transition = "crossfade";
      }
    }

    const outgoing = bufferElement(activeBufferRef.current, audioARef, audioBRef);
    const incoming = bufferElement(nextPreloadSlot(activeBufferRef.current), audioARef, audioBRef);
    if (incoming) {
      incoming.src = next.audio_url;
      if (hasStartedRef.current) {
        seekToLiveOffset(incoming, next);
        incoming.volume = transition === "crossfade" ? 0 : 1;
        void incoming.play();
      }
    }
    if (hasStartedRef.current) {
      if (transition === "crossfade" && incoming && outgoing) {
        rampVolume(outgoing, "outgoing", CROSSFADE_DURATION_MS, () => {
          outgoing.pause();
          outgoing.volume = 1;
        });
        rampVolume(incoming, "incoming", CROSSFADE_DURATION_MS);
      } else {
        outgoing?.pause();
        if (outgoing) outgoing.volume = 1;
      }
    }
    activeBufferRef.current = nextPreloadSlot(activeBufferRef.current);
  }, []);

  // Open the SSE connection once. The server sends the current pointer immediately on
  // connect (sync-on-arrival) and on every push/heartbeat thereafter (criterion #4); a
  // dropped connection is handled by the browser's native `EventSource` auto-reconnect,
  // whose first frame on the new connection is again the current truth.
  useEffect(() => {
    const source = new EventSource("/events");

    const handleSongChange = (event: MessageEvent<string>) => {
      applyPlaying(JSON.parse(event.data) as NowPlaying);
    };
    const handleIdle = () => {
      prevPlaybackIdRef.current = null;
      prevPointerRef.current = null;
      setState({ status: "idle" });
    };

    source.addEventListener("song-change", handleSongChange);
    source.addEventListener("idle", handleIdle);

    return () => {
      source.close();
    };
  }, [applyPlaying]);

  const handlePlay = () => {
    if (!state || state.status !== "playing") return;
    const audio = bufferElement(activeBufferRef.current, audioARef, audioBRef);
    if (!audio) return;
    seekToLiveOffset(audio, state);
    void audio.play();
    hasStartedRef.current = true;
    setHasStarted(true);
  };

  // The current track finished with no next-song push having arrived yet — re-fetch
  // immediately (PRD story #11) rather than waiting for the next SSE frame. Routed
  // through `applyPlaying` so a genuinely new pointer also gets the gapless swap.
  const handleEnded = async (event: React.SyntheticEvent<HTMLAudioElement>) => {
    if (event.currentTarget !== bufferElement(activeBufferRef.current, audioARef, audioBRef)) {
      return;
    }
    const next = await fetchNowPlaying(NOW_PLAYING_BASE);
    if (next.status === "playing") {
      applyPlaying(next);
    } else {
      prevPlaybackIdRef.current = null;
      prevPointerRef.current = null;
      setState(next);
    }
  };

  // Continuous drift correction (issue #9, criterion #1 / design D6): on every
  // `timeupdate` of whichever buffer is currently active, compare where we ARE
  // (`audio.currentTime`) to where the server timeline says we should be, and hard-seek
  // only when the drift crosses the 1s deadband (`decideDrift`) — a sub-second gap is
  // left alone so imperceptible jitter never nudges playback.
  const handleTimeUpdate = (event: React.SyntheticEvent<HTMLAudioElement>) => {
    const audio = bufferElement(activeBufferRef.current, audioARef, audioBRef);
    if (event.currentTarget !== audio) return;
    if (!hasStarted || !audio || !state || state.status !== "playing") return;
    const expected = computeExpectedOffsetSeconds(
      Date.parse(state.started_at),
      Date.now(),
      skewMsRef.current,
    );
    if (decideDrift(audio.currentTime - expected) === "seek") {
      audio.currentTime = Math.max(expected, 0);
    }
  };

  // Issue #15, criterion #5: load "songs left" once on mount. Best-effort — a failure
  // leaves the indicator hidden and never disrupts playback.
  useEffect(() => {
    let cancelled = false;
    fetchQuota(NOW_PLAYING_BASE)
      .then((q) => {
        if (!cancelled) setQuota(q);
      })
      .catch(() => {
        /* backend unreachable — leave the indicator hidden */
      });
    return () => {
      cancelled = true;
    };
  }, []);

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
      {quota !== null && (
        <p style={{ opacity: 0.5, margin: 0, fontSize: "0.85rem" }}>
          {formatSongsLeft(quota)}
        </p>
      )}
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
      <audio ref={audioARef} preload="auto" onEnded={handleEnded} onTimeUpdate={handleTimeUpdate} />
      <audio ref={audioBRef} preload="auto" onEnded={handleEnded} onTimeUpdate={handleTimeUpdate} />
    </main>
  );
}
