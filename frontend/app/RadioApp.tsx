"use client";

import { useCallback, useEffect, useReducer, useState } from "react";
import { fetchQuota, type QuotaResponse } from "@/lib/quota";
import { INITIAL_FEED, reduceFeed } from "@/lib/radio";
import type { NowPlayingState } from "@/lib/nowPlaying";
import Composer from "./Composer";
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
  const [quota, setQuota] = useState<QuotaResponse | null>(null);

  useEffect(() => {
    const source = new EventSource("/events");
    source.onopen = () => dispatch({ type: "open" });
    source.onerror = () => dispatch({ type: "error" }); // browser auto-reconnects; state kept
    source.addEventListener("song-change", (e) =>
      dispatch({ type: "song-change", data: (e as MessageEvent<string>).data }),
    );
    source.addEventListener("idle", () => dispatch({ type: "idle" }));
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
        <RadioStage state={feed.state} />
        <section aria-label="Status" style={{ textAlign: "center", marginTop: 16 }}>
          <LiveIndicator feed={feed} />
          {feed.state?.status === "playing" && (
            <p style={{ opacity: 0.7, margin: "8px 0" }}>{feed.state.title}</p>
          )}
          <Player nowPlaying={feed.state} onRefetched={onRefetched} />
        </section>
      </div>
      <div style={{ width: "100%", maxWidth: 720, margin: "0 auto", position: "sticky", bottom: 0 }}>
        {/* generationActive stays false in #36: per-user job progress is a later slice. */}
        <Composer quota={quota} generationActive={false} onCreated={refreshQuota} />
      </div>
    </div>
  );
}
