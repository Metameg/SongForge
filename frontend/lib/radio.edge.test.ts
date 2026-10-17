/** Edge coverage for `lib/radio.ts` reducer + LIVE indicator (issue #36 Review Focus #5). */
import { describe, expect, it } from "vitest";
import { INITIAL_FEED, liveIndicator, reduceFeed, type RadioFeed } from "./radio";

const playingJson = (o: Record<string, unknown> = {}): string =>
  JSON.stringify({
    status: "playing", song_id: "s1", title: "T", source: "generated", object_key: "audio/s1.mp3",
    audio_url: "https://x/a.mp3", started_at: "2026-10-04T00:00:00Z", ends_at: "2026-10-04T00:02:00Z",
    duration: 120, playback_id: "p1", version: 1, server_time: "2026-10-04T00:00:01Z",
    album_cover_path: "https://cdn.test/c.svg", ...o,
  });

describe("reduceFeed edge cases", () => {
  it("error on an empty feed stays disconnected with no state", () => {
    expect(reduceFeed(INITIAL_FEED, { type: "error" })).toEqual({ state: null, connected: false });
  });

  it("repeated errors are idempotent", () => {
    const f = reduceFeed({ state: { status: "idle" }, connected: true }, { type: "error" });
    expect(reduceFeed(f, { type: "error" })).toEqual(f);
  });

  it("idle after playing clears now-playing but keeps the connection flag", () => {
    let f = reduceFeed(INITIAL_FEED, { type: "open" });
    f = reduceFeed(f, { type: "song-change", data: playingJson() });
    f = reduceFeed(f, { type: "idle" });
    expect(f).toEqual({ state: { status: "idle" }, connected: true });
    expect(liveIndicator(f).live).toBe(false);
  });

  it("song-change replaces the previous song, including a now-null cover", () => {
    let f = reduceFeed(INITIAL_FEED, { type: "song-change", data: playingJson() });
    f = reduceFeed(f, {
      type: "song-change",
      data: playingJson({ song_id: "s2", playback_id: "p2", album_cover_path: null }),
    });
    expect(f.state).toMatchObject({ status: "playing", song_id: "s2", playback_id: "p2", album_cover_path: null });
  });

  it("song-change does not alter the connected flag", () => {
    const f = reduceFeed({ state: null, connected: false }, { type: "song-change", data: playingJson() });
    expect(f.connected).toBe(false);
  });

  it.each(["null", "42", '"str"', "{}", '{"status":"idle"}', ""])(
    "ignores non-playing song-change frame %j and returns the same feed object",
    (data) => {
      const f: RadioFeed = { state: JSON.parse(playingJson()), connected: true };
      expect(reduceFeed(f, { type: "song-change", data })).toBe(f);
    },
  );
});

describe("liveIndicator edge cases", () => {
  const playing = JSON.parse(playingJson());
  const idle = { status: "idle" as const };

  it.each([
    [playing, true, true],
    [playing, false, false],
    [idle, true, false],
    [idle, false, false],
    [null, true, false],
    [null, false, false],
  ])("state=%j connected=%s -> live=%s (label matches)", (state, connected, live) => {
    const view = liveIndicator({ state, connected } as RadioFeed);
    expect(view.live).toBe(live);
    expect(view.label).toBe(live ? "LIVE" : "OFFLINE");
  });

  it("idle + connected says quiet; idle + disconnected says reconnecting", () => {
    expect(liveIndicator({ state: idle, connected: true }).message).toBe("The station is quiet right now.");
    expect(liveIndicator({ state: idle, connected: false }).message).toBe("Reconnecting...");
  });
});
