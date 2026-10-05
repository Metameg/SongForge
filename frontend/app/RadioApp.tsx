"use client";

import { useCallback, useEffect, useReducer, useState } from "react";
import { fetchQueue, wireQueueRefetch, type QueueResponse } from "@/lib/queue";
import { fetchQuota, type QuotaResponse } from "@/lib/quota";
import { parseJobEvent, type JobEventName } from "@/lib/events";
import { addMySong, hasMySong, loadMySongs } from "@/lib/mySongs";
import { INITIAL_PROGRESS, reduceProgress } from "@/lib/progress";
import { INITIAL_FEED, reduceFeed } from "@/lib/radio";
import type { NowPlayingState } from "@/lib/nowPlaying";
import Composer from "./Composer";
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
  const [mySongIds, setMySongIds] = useState<Set<string>>(() => new Set());
  const [quota, setQuota] = useState<QuotaResponse | null>(null);

  const [queue, setQueue] = useState<QueueResponse | null>(null);

  const refreshQuota = useCallback(async () => {
    try {
      setQuota(await fetchQuota(""));
    } catch {
      /* keep last known */
    }
  }, []);

  const refreshQueue = useCallback(async () => {
    try {
      setQueue(await fetchQueue(""));
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
      // Queue is read once now (cookie established above), then refetched reactively on
      // song-change / the viewer's own job-progress / job-ready -- no timer polling.
      unwireQueue = wireQueueRefetch(source, () => void refreshQueue());
      void refreshQueue();
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
  const handleCreated = useCallback(
    (jobId: string) => {
      dispatchProgress({ type: "started", jobId });
      void refreshQuota();
    },
    [refreshQuota],
  );
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
