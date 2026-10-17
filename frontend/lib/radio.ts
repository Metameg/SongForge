/** Radio feed reducer (SSE events -> state) and LIVE indicator derivation (issue #36). Pure. */
import type { NowPlaying, NowPlayingState } from "./nowPlaying";

export interface RadioFeed {
  state: NowPlayingState | null;
  connected: boolean;
}

export type RadioEvent =
  | { type: "open" }
  | { type: "error" }
  | { type: "song-change"; data: string }
  | { type: "idle" }
  | { type: "refetched"; state: NowPlayingState };

export const INITIAL_FEED: RadioFeed = { state: null, connected: false };

export function reduceFeed(feed: RadioFeed, event: RadioEvent): RadioFeed {
  switch (event.type) {
    case "open":
      return { ...feed, connected: true };
    case "error":
      // Keep the last state: EventSource auto-reconnects and re-sends the current truth.
      return { ...feed, connected: false };
    case "idle":
      return { ...feed, state: { status: "idle" } };
    case "refetched":
      return { ...feed, state: event.state };
    case "song-change": {
      try {
        const parsed = JSON.parse(event.data) as NowPlaying;
        if (parsed && parsed.status === "playing") return { ...feed, state: parsed };
      } catch {
        /* malformed frame: ignore */
      }
      return feed;
    }
  }
}

export interface LiveIndicatorView {
  live: boolean;
  label: string;
  message: string | null;
}

export function liveIndicator(feed: RadioFeed): LiveIndicatorView {
  const playing = feed.state?.status === "playing";
  const live = feed.connected && playing;
  let message: string | null = null;
  if (feed.connected && feed.state?.status === "idle") message = "The station is quiet right now.";
  else if (!feed.connected && feed.state) message = "Reconnecting...";
  return { live, label: live ? "LIVE" : "OFFLINE", message };
}
