"use client";

import { useCallback, useEffect, useReducer, useState } from "react";
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

  useEffect(() => {
    const source = new EventSource("/events");
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
    return () => source.close();
  }, []);

  const refreshQuota = useCallback(async () => {
    try {
      setQuota(await fetchQuota(""));
    } catch {
      /* keep last known */
    }
  }, []);
  useEffect(() => {
    void refreshQuota();
  }, [refreshQuota]);
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
        <header>
          <h1 style={{ fontSize: 20, margin: "0 0 12px" }}>SongForge</h1>
        </header>
        <RadioStage state={feed.state} isMine={isMine} />
        <section aria-label="Status" style={{ textAlign: "center", marginTop: 16 }}>
          <LiveIndicator feed={feed} />
          {feed.state?.status === "playing" && (
            <p style={{ opacity: 0.7, margin: "8px 0" }}>{feed.state.title}</p>
          )}
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
