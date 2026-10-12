"use client";

import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import {
  fetchQueue,
  makeLatestGuard,
  myPosition,
  wireQueueRefetch,
  type QueueResponse,
} from "@/lib/queue";
import { fetchQuota, type QuotaResponse } from "@/lib/quota";
import { parseJobEvent, type JobEventName } from "@/lib/events";
import { addMySong, hasMySong, loadMySongs } from "@/lib/mySongs";
import { emptyHistory, reduceHistory } from "@/lib/download";
import { INITIAL_PROGRESS, progressTimeoutMs, reduceProgress } from "@/lib/progress";
import { INITIAL_FEED, reduceFeed } from "@/lib/radio";
import type { NowPlayingState } from "@/lib/nowPlaying";
import Composer from "./Composer";
import DownloadButton from "./DownloadButton";
import GenerationProgressView from "./GenerationProgress";
import LiveIndicator from "./LiveIndicator";
import Player from "./Player";
import QueueStatus from "./QueueStatus";
import RadioStage from "./RadioStage";

/**
 * Three-zone radio page (issue #36): stage (top) / status (middle) / pinned composer
 * (bottom). Owns the single `EventSource("/events")` connection and the quota read;
 * `Player` consumes the feed state as a prop.
 */
export default function RadioApp() {
  const [feed, dispatch] = useReducer(reduceFeed, INITIAL_FEED);
  const [progress, dispatchProgress] = useReducer(reduceProgress, INITIAL_PROGRESS);
  const [history, dispatchHistory] = useReducer(reduceHistory, emptyHistory);
  const [mySongIds, setMySongIds] = useState<Set<string>>(() => new Set());
  const [quota, setQuota] = useState<QuotaResponse | null>(null);

  const [queue, setQueue] = useState<QueueResponse | null>(null);
  // Mirror of `queue` for the SSE handlers: the `song-change` gate reads the LATEST
  // queue (do I currently hold a position?) without re-wiring the stream each render.
  const queueRef = useRef<QueueResponse | null>(null);
  useEffect(() => {
    queueRef.current = queue;
  }, [queue]);

  const refreshQuota = useCallback(async () => {
    try {
      setQuota(await fetchQuota(""));
    } catch {
      /* keep last known */
    }
  }, []);

  // Only the newest refresh applies: rapid SSE events fire concurrent fetches, and an
  // older-but-slower response must not overwrite a newer one with a stale position.
  const queueGuard = useRef(makeLatestGuard());
  const refreshQueue = useCallback(async () => {
    const token = queueGuard.current.begin();
    try {
      const next = await fetchQueue("");
      if (queueGuard.current.isCurrent(token)) setQueue(next);
    } catch {
      /* keep last known */
    }
  }, []);

  useEffect(() => {
    let unwireQueue: (() => void) | null = null;
    let source: EventSource | null = null;
    let cancelled = false;
    // Establish the signed identity cookie BEFORE opening the stream. The backend
    // `GET /events` only READS identity (it never Set-Cookies one), while `GET /quota`
    // mints and Set-Cookies the identity on a first visit. Opening the stream first
    // would register it under a throwaway identity the backend discards, so this
    // viewer's own job-progress/job-ready/job-failed -- published to their real cookie
    // identity once they create -- would never reach them until a reconnect/reload.
    // Awaiting the quota read first means the EventSource request carries that cookie.
    void (async () => {
      await refreshQuota();
      if (cancelled) return;
      source = new EventSource("/events");
      // depth-in-SSE: the waiting-queue depth rides every `song-change` frame (and the
      // connect-time frame), so a plain listener gets "N waiting" from the push with NO
      // `/queue` -> Postgres read. `/queue` is refetched for the viewer's own POSITION
      // only when it can have changed for them: on their own job-progress/job-ready, and
      // on song-change ONLY while they currently hold a position. No initial blanket
      // fetch -- the first song-change frame on connect seeds the depth.
      unwireQueue = wireQueueRefetch(source, {
        onDepth: (depth) => {
          if (depth === null) return;
          setQueue((prev) => (prev === null ? { depth, positions: [] } : { ...prev, depth }));
        },
        refetchPositions: () => void refreshQueue(),
        hasPosition: () => {
          const cur = queueRef.current;
          return cur !== null && myPosition(cur) !== null;
        },
      });
      source.onopen = () => dispatch({ type: "open" });
      source.onerror = () => dispatch({ type: "error" }); // browser auto-reconnects; state kept
      source.addEventListener("song-change", (e) =>
        dispatch({ type: "song-change", data: (e as MessageEvent<string>).data }),
      );
      source.addEventListener("idle", () => dispatch({ type: "idle" }));
      const onJob = (name: JobEventName) => (e: Event) => {
        const ev = parseJobEvent(name, (e as MessageEvent<string>).data);
        if (!ev) return;
        dispatchProgress(ev);
        if (ev.type === "ready") setMySongIds(addMySong(ev.song_id));
      };
      source.addEventListener("job-progress", onJob("job-progress"));
      source.addEventListener("job-ready", onJob("job-ready"));
      source.addEventListener("job-failed", onJob("job-failed"));
    })();
    return () => {
      cancelled = true;
      unwireQueue?.();
      source?.close();
    };
  }, [refreshQuota, refreshQueue]);
  useEffect(() => {
    setMySongIds(loadMySongs()); // post-mount: avoids an SSR/hydration mismatch
  }, []);
  useEffect(() => {
    if (progress.status === "failed") void refreshQuota(); // credit was refunded
  }, [progress.status, refreshQuota]);
  // Safety net so the bar never hangs on "Generating" forever if the terminal
  // job-ready/job-failed SSE frame is dropped (bounded, no-replay per-user channel)
  // or missed during a brief reconnect. Armed on entering `generating`, re-armed on
  // each eta update (an observed job-progress), cleared on any terminal transition.
  // No polling -- see `lib/progress.ts::progressTimeoutMs`.
  useEffect(() => {
    if (progress.status !== "generating" || progress.jobId === null) return;
    const jobId = progress.jobId;
    const timer = setTimeout(
      () => dispatchProgress({ type: "timeout", jobId }),
      progressTimeoutMs(progress.eta),
    );
    return () => clearTimeout(timer);
  }, [progress.status, progress.jobId, progress.eta]);
  const handleCreated = useCallback(
    (jobId: string) => {
      dispatchProgress({ type: "started", jobId });
      void refreshQuota();
    },
    [refreshQuota],
  );
  // Client-observed download history, derived from the feed (no second SSE listener).
  const playingId = feed.state?.status === "playing" ? feed.state.song_id : null;
  const playingTitle = feed.state?.status === "playing" ? feed.state.title : "";
  useEffect(() => {
    if (playingId) dispatchHistory({ song_id: playingId, title: playingTitle });
  }, [playingId, playingTitle]);
  const isMine = feed.state?.status === "playing" && hasMySong(mySongIds, feed.state.song_id);

  const onRefetched = useCallback(
    (state: NowPlayingState) => dispatch({ type: "refetched", state }),
    [],
  );

  return (
    <div
      style={{
        minHeight: "100dvh",
        display: "flex",
        flexDirection: "column",
        overflowX: "hidden",
        boxSizing: "border-box",
      }}
    >
      <div
        style={{
          width: "100%",
          maxWidth: 720,
          margin: "0 auto",
          padding: 16,
          boxSizing: "border-box",
          flex: 1,
        }}
      >
        <header style={{ display: "flex", justifyContent: "center", padding: "4px 0 18px" }}>
          <h1
            className="sf-wordmark"
            style={{ fontSize: 24, margin: 0, display: "inline-flex", alignItems: "center", gap: 9 }}
          >
            <span
              aria-hidden="true"
              style={{
                width: 9,
                height: 9,
                borderRadius: "50%",
                background: "var(--sf-accent-grad)",
                boxShadow: "0 0 12px 0 var(--sf-accent-glow)",
              }}
            />
            Song<span style={{ color: "var(--sf-accent)" }}>Forge</span>
          </h1>
        </header>
        <RadioStage state={feed.state} isMine={isMine} />
        <section aria-label="Status" style={{ textAlign: "center", marginTop: 18 }}>
          <LiveIndicator feed={feed} />
          {feed.state?.status === "playing" && (
            <p
              className="sf-wordmark"
              style={{ fontSize: 17, fontWeight: 600, color: "var(--sf-text)", margin: "10px 0 2px" }}
            >
              {feed.state.title}
            </p>
          )}
          <DownloadButton history={history} />
          <QueueStatus queue={queue} />
          <Player nowPlaying={feed.state} onRefetched={onRefetched} />
        </section>
      </div>
      <div style={{ width: "100%", maxWidth: 720, margin: "0 auto", position: "sticky", bottom: 0 }}>
        <GenerationProgressView progress={progress} />
        <Composer
          quota={quota}
          generationActive={progress.status === "generating"}
          onCreated={handleCreated}
        />
      </div>
    </div>
  );
}
