import { describe, expect, it } from "vitest";
import { INITIAL_FEED, liveIndicator, reduceFeed, type RadioFeed } from "./radio";

const playingJson = (o: Record<string, unknown> = {}): string =>
  JSON.stringify({
    status: "playing", song_id: "s1", title: "T", source: "generated", object_key: "audio/s1.mp3",
    audio_url: "https://x/a.mp3", started_at: "2026-10-04T00:00:00Z", ends_at: "2026-10-04T00:02:00Z",
    duration: 120, playback_id: "p1", version: 1, server_time: "2026-10-04T00:00:01Z",
    album_cover_path: "https://cdn.test/c.svg", ...o,
  });

describe("reduceFeed", () => {
  it("starts empty and disconnected", () => {
    expect(INITIAL_FEED).toEqual({ state: null, connected: false });
  });
  it("open marks connected", () => {
    expect(reduceFeed(INITIAL_FEED, { type: "open" }).connected).toBe(true);
  });
  it("reconnect preserves playback state", () => {
    let f = reduceFeed(INITIAL_FEED, { type: "open" });
    f = reduceFeed(f, { type: "song-change", data: playingJson() });
    const held = f.state;
    f = reduceFeed(f, { type: "error" });
    expect(f.connected).toBe(false);
    expect(f.state).toEqual(held);
    f = reduceFeed(f, { type: "open" });
    f = reduceFeed(f, { type: "song-change", data: playingJson() });
    expect(f.state).toMatchObject({ status: "playing", playback_id: "p1" });
  });
  it("idle event sets the idle state", () => {
    const f = reduceFeed({ state: null, connected: true }, { type: "idle" });
    expect(f.state).toEqual({ status: "idle" });
    expect(liveIndicator(f).message).toBe("The station is quiet right now.");
  });
  it("ignores malformed song-change data", () => {
    const f: RadioFeed = { state: { status: "idle" }, connected: true };
    expect(reduceFeed(f, { type: "song-change", data: "{not json" })).toBe(f);
  });
  it("heartbeat re-emit yields deep-equal states", () => {
    const a = reduceFeed(INITIAL_FEED, { type: "song-change", data: playingJson() });
    const b = reduceFeed(a, { type: "song-change", data: playingJson() });
    expect(b.state).toEqual(a.state);
  });
  it("refetched replaces state", () => {
    const f = reduceFeed(INITIAL_FEED, { type: "refetched", state: { status: "idle" } });
    expect(f.state).toEqual({ status: "idle" });
  });
});

describe("liveIndicator", () => {
  const playing = reduceFeed(INITIAL_FEED, { type: "song-change", data: playingJson() }).state;
  it("playing + connected is LIVE", () => {
    expect(liveIndicator({ state: playing, connected: true })).toEqual({ live: true, label: "LIVE", message: null });
  });
  it("playing + disconnected is reconnecting", () => {
    expect(liveIndicator({ state: playing, connected: false })).toEqual({ live: false, label: "OFFLINE", message: "Reconnecting..." });
  });
  it("null + disconnected has no message", () => {
    expect(liveIndicator(INITIAL_FEED)).toEqual({ live: false, label: "OFFLINE", message: null });
  });
});
